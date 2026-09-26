# -*- coding: utf-8 -*-
"""
arcmap_mcp_server.py  ──  Servidor MCP EXTERNO (Python 3, 64 bits) + FastMCP.

Es la pieza que registras en Claude Code / OpenCode. Habla por stdio con el
cliente IA y reenvía cada herramienta, por socket TCP local, al puente que
corre dentro de ArcMap (el add-in .NET, barra de herramientas arcmap-mcp).

    Claude Code ──stdio──► este servidor ──socket 127.0.0.1:27179──► ArcMap vivo

Arranque manual de prueba:
    python arcmap_mcp_server.py        (requiere ArcMap abierto con el puente)
"""

import os
import json
import re
import base64
import socket
import struct

from mcp.server.fastmcp import FastMCP, Image

# El add-in escucha SOLO en el loopback de su máquina, y eso no se puede cambiar
# (decisión de seguridad, ADR-006): apuntar ARCMAP_BRIDGE_HOST a la IP de la otra
# máquina NO funciona, porque allí no hay nada escuchando en esa interfaz. Para un
# ArcMap remoto se monta un túnel (SSH `-L`, o Tailscale con reenvío de puerto), que
# termina en el 127.0.0.1 del destino; entonces este servidor se conecta a su propio
# extremo local del túnel, y ARCMAP_BRIDGE_HOST solo hace falta si ese extremo no
# es 127.0.0.1.
HOST = os.environ.get("ARCMAP_BRIDGE_HOST", "127.0.0.1")
# Mismo nombre de variable que lee el add-in (McpServer.cs): una sola variable mueve
# los dos extremos. Es además la vía de escape cuando un ArcMap zombi deja cogido el
# 27179 y ningún ArcMap nuevo puede levantar el puente.
PORT = int(os.environ.get("ARCMAP_BRIDGE_PORT", "27179"))

# --------------------------------------------------------------------------- #
# Presupuestos de espera.
#
# REGLA ÚNICA: el timeout de ESTE lado va SIEMPRE por encima del tope que el
# add-in aplica a ese comando, con margen. El add-in, cuando se le acaba el
# tiempo, devuelve un error que dice en qué FASE se quedó (importando arcpy,
# abriendo documento, ejecutando código) y suelta el candado del puente; el
# socket, cuando se le acaba a él, solo corta, deja el trabajo huérfano vivo
# dentro de ArcMap y obliga a adivinar. Si los dos números EMPATAN gana el corte
# mudo, que es el peor de los dos. Por eso ninguno es redondo: el redondo es el
# del add-in, y aquí se le suma el margen.
# --------------------------------------------------------------------------- #


class _Espera(int):
    """Segundos de espera que RECUERDAN de qué variable de entorno salen.

    El mensaje de 'puente ocupado' tiene que decir qué variable subir, y no
    siempre es ARCMAP_GP_TIMEOUT: nombrar la que no gobierna esa llamada manda a
    tocar un número que no cambiará nada. Atando el nombre al valor no se pueden
    separar. Es un `int` de verdad, así que `socket.settimeout` lo traga igual.
    """

    def __new__(cls, variable, defecto):
        espera = super().__new__(cls, int(os.environ.get(variable, str(defecto))))
        espera.variable = variable
        return espera


TIMEOUT = _Espera("ARCMAP_BRIDGE_TIMEOUT", 60)  # tools rápidas
# Comandos LARGOS que el add-in atiende en el hilo de ArcMap (exports de layout,
# run_geoprocessing, calculate_geometry): allí su tope es LongHandlerTimeout, 1800 s.
# 1860 = 1800 + un minuto de margen. Antes valía 1800 clavados y el empate lo ganaba
# el socket.
GP_TIMEOUT = _Espera("ARCMAP_GP_TIMEOUT", 1860)  # 30 min + margen
# Comandos que el add-in despacha a un handler de FONDO (las 3 de Data Driven Pages
# y las 5 ambientales): ahí el peor caso legítimo no es un solo tope, es una suma.
# DDP paga snapshot del documento (hasta 600 s) + subprocess del runner (1800 s) =
# 2400 s; las ambientales, subprocess (1800 s) + el paso STA de añadir-al-mapa (60 s).
# Por encima de todo eso el add-in tiene un techo de seguridad de 2700 s
# (FondoTimeout) que SIEMPRE responde algo, así que el margen se cuenta sobre él.
FONDO_TIMEOUT = _Espera("ARCMAP_FONDO_TIMEOUT", 2760)  # 2700 del add-in + margen
# Guardar el documento: el add-in le da 600 s (un .mxd con decenas de capas y ráster
# pesado no se escribe en 60).
SAVE_TIMEOUT = _Espera("ARCMAP_SAVE_TIMEOUT", 630)
# execute_arcpy es interactivo y NO debe esperar 30 min: a los 120 s el cliente MCP
# ya ha dado la llamada por colgada, así que un techo alto solo sirve para que el
# fallo no tenga forma de error (nos costó las sesiones del 27-jul y del 29-jul).
# Va por encima del ARCMAP_EXEC_TIMEOUT del add-in (900 s) a propósito: así vence
# primero el timeout del add-in, que mata el subproceso y devuelve un error que dice
# en qué FASE se quedó el runner, en vez de un corte mudo de socket que además deja
# el runner huérfano vivo (y un runner huérfano impide cerrar ArcMap).
EXEC_TIMEOUT = _Espera("ARCMAP_EXEC_TIMEOUT_CLIENTE", 930)
# El par 930/900 solo cuadra cuando la copia del documento es instantánea (copia del
# .mxd del disco, el caso normal). Con `serializar_sesion=True` el add-in gasta hasta
# 600 s (SnapshotTimeout) en SaveAsDocument ANTES de que empiecen a contar sus 900 s:
# peor caso 1500 s, y con 930 aquí volvía a ganar el corte mudo de socket. Se le da
# presupuesto propio en vez de subir el de todas las llamadas, que son interactivas.
EXEC_SESION_TIMEOUT = _Espera("ARCMAP_EXEC_SESION_TIMEOUT_CLIENTE", 1560)  # 600 + 900 + margen
# Cuánto se deja dibujar a ArcMap cuando un export llega con el mapa aún pintando
# (E_PENDING). La espera va AQUÍ, fuera de ArcMap, a propósito: ver _exportar.
ESPERA_DIBUJO = _Espera("ARCMAP_ESPERA_DIBUJO", 120)
_PAUSA_DIBUJO = 3


class ArcMapClient:
    """Cliente socket hacia el puente dentro de ArcMap. Reconecta por comando."""

    def __init__(self, host=HOST, port=PORT, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, ctype, params=None, timeout=None):
        """Envía un comando al puente. `timeout` (s) sobrescribe el del cliente para
        esta llamada: cada tool pasa el presupuesto (`_Espera`) que le corresponde
        según el tope que el add-in aplique a ESE comando."""
        eff = timeout or self.timeout
        msg = json.dumps({"type": ctype, "params": params or {}}).encode("utf-8")
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(eff)
        try:
            s.connect((self.host, self.port))
        except (ConnectionRefusedError, socket.timeout, OSError):
            # El `finally: s.close()` de más abajo cuelga del OTRO try, así que en
            # esta rama el socket se filtraba hasta que lo recogía el GC. Con el
            # puente caído se reintenta en bucle, y ahí los descriptores se acumulan.
            s.close()
            # PUENTE CAIDO: nadie escucha en el puerto. El estado se nombra con todas
            # las letras porque desde fuera se confunde con "ocupado" (ver el timeout
            # de recv más abajo) y son cosas distintas: aquí esperar no sirve de nada.
            return {
                "ok": False,
                "estado": "puente_caido",
                "error": (
                    "PUENTE CAIDO: nadie escucha en %s:%s. O ArcMap no está abierto, o "
                    "lo está pero nadie ha pulsado 'Iniciar' en la barra arcmap-mcp. "
                    "Reintentar sin arrancarlo dará exactamente este mismo error."
                    % (self.host, self.port)
                ),
            }
        try:
            s.sendall(msg)
            # El puente envía UNA respuesta y cierra la conexión: leer hasta EOF
            # y parsear UNA sola vez. Parsear por chunk era O(n²) con payloads
            # grandes (screenshots base64) y un chunk cortado a mitad de un
            # carácter multibyte lanzaba UnicodeDecodeError sin capturar.
            buf = b""
            while True:
                try:
                    chunk = s.recv(65536)
                except socket.timeout:
                    # PUENTE VIVO PERO SIN RESPONDER: la conexión se abrió, o sea que
                    # el add-in está cargado y su listener (que vive en un hilo de
                    # fondo, no en el de ArcMap) sigue aceptando. Lo que no vuelve es
                    # ESTE comando. Es el opuesto exacto de "puente_caido": aquí el
                    # trabajo sigue vivo dentro de ArcMap y matarlo es lo que no hay
                    # que hacer.
                    #
                    # El mensaje decía "ArcMap atiende el socket en su hilo principal"
                    # y mandaba a subir ARCMAP_GP_TIMEOUT. Las dos cosas eran del
                    # puente Python viejo: hoy `ping` contesta al instante aunque haya
                    # un comando en curso, y la variable que gobierna la espera depende
                    # de la llamada. Decirlo mal manda a tocar un número que no cambia
                    # nada y a dar por muerto lo que solo estaba ocupado.
                    variable = getattr(eff, "variable", TIMEOUT.variable)
                    return {"ok": False, "estado": "puente_ocupado", "error": (
                        "PUENTE VIVO PERO SIN RESPONDER en %ss: la conexión se abrió, "
                        "así que el add-in está cargado; lo que no ha vuelto es este "
                        "comando. Llama a `ping` AHORA: responde aunque el puente esté "
                        "ocupado y trae `comando_en_curso` y `ocupado_desde_s`, que es "
                        "lo que distingue 'lleva 8 s exportando' de 'lleva 1.400 s "
                        "atascado'. Si hay un geoproceso, SIGUE CORRIENDO dentro de "
                        "ArcMap: no lo relances. Para esperar más en llamadas como esta "
                        "la variable es %s (ahora %ss)." % (eff, variable, eff))}
                if not chunk:
                    break
                buf += chunk
            if not buf:
                return {"ok": False, "error": "ArcMap cerró la conexión sin respuesta válida."}
            try:
                return json.loads(buf.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {"ok": False, "error":
                        "Respuesta ilegible del puente (%d bytes)." % len(buf)}
        finally:
            s.close()


_client = ArcMapClient()
mcp = FastMCP("arcmap-mcp")


def _exportar(ctype, params):
    """Envía un export y, si ArcMap responde que el mapa aún está dibujando
    (`dibujando: `, su E_PENDING), espera AQUÍ y reintenta.

    La espera estaba dentro del add-in, bombeando mensajes con DoEvents en el hilo
    de ArcMap, y eso metía el dibujado pendiente dentro de la llamada del puente:
    con etiquetas no terminaba nunca y ArcMap quedó colgado tres veces el
    2026-09-25 (Esc sin efecto, mapa en blanco). Esperando fuera, ArcMap dibuja en
    su propio bucle de mensajes: el mismo export salió en 0,8 s tras 20 s de margen.
    Mientras se espera no se manda NADA al puente, ni un ping.
    """
    import time
    inicio = time.monotonic()
    reintentos = 0
    while True:
        r = _client.send(ctype, params, timeout=GP_TIMEOUT)
        error = r.get("error") or ""
        if r.get("ok") or not error.startswith("dibujando: "):
            break
        if time.monotonic() - inicio + _PAUSA_DIBUJO > ESPERA_DIBUJO:
            r = dict(r, error=error + " (se esperó %d s en %d reintentos; sube %s si el mapa"
                                      " es muy pesado)" % (ESPERA_DIBUJO, reintentos,
                                                           ESPERA_DIBUJO.variable))
            break
        time.sleep(_PAUSA_DIBUJO)
        reintentos += 1
    if reintentos and r.get("ok") and isinstance(r.get("result"), dict):
        r["result"]["aviso_dibujo"] = ("ArcMap estaba dibujando el mapa: el export salió tras "
                                       "%d reintentos (%.0f s de espera)."
                                       % (reintentos, time.monotonic() - inicio))
    return r


# --------------------------------------------------------------------------- #
# Lectura del .mxd SIN arcpy y SIN abrirlo.
#
# Un .mxd es un Compound File Binary (el contenedor OLE de Office), y lleva un
# stream `Version` con la versión que declara el documento. Leerlo cuesta
# milisegundos y no necesita ArcMap, ni el puente, ni licencia: es lo único que
# se puede saber de un documento que NO se deja abrir.
#
# Para qué sirve: `arcpy.mapping.MapDocument()` falla con un mensaje genérico
# ("no puede abrir documento de mapa" / "Nombre de archivo MXD no válido") que
# vale igual para una ruta mala, un fichero corrupto o una versión superior a la
# de la sesión. Con la versión declarada en la mano, ese fallo deja de ser mudo.
# --------------------------------------------------------------------------- #

_FIRMA_CFB = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_MARCA = 0xFFFFFFFA  # de aquí arriba son marcas (ENDOFCHAIN, FREESECT, ...)


def _mxd_version_declarada(ruta):
    """Devuelve (version, error). `version` es p.ej. '10.5'; None si no se pudo."""
    try:
        with open(ruta, "rb") as fh:
            datos = fh.read()
    except OSError as exc:
        return None, "no se pudo leer el fichero: %s" % exc

    if datos[:8] != _FIRMA_CFB:
        return None, "no es un .mxd válido (no es un compound document)"

    try:
        ssz = 1 << struct.unpack_from("<H", datos, 30)[0]
        mssz = 1 << struct.unpack_from("<H", datos, 32)[0]
        dir0 = struct.unpack_from("<I", datos, 48)[0]
        mfat0 = struct.unpack_from("<I", datos, 60)[0]
        difat0 = struct.unpack_from("<I", datos, 68)[0]
        n_difat = struct.unpack_from("<I", datos, 72)[0]

        # La DIFAT trae 109 entradas en la cabecera; un documento de más de ~7 MB
        # (sectores de 512 B) necesita más y esas cuelgan de una CADENA de
        # sectores. Saltársela no da error: deja la FAT corta, las cadenas se
        # truncan y los streams salen VACÍOS, o sea un fichero sano que parece
        # ilegible. Pasó con 92 de 400 .mxd al validar esto.
        difat = list(struct.unpack_from("<109I", datos, 76))
        sec, vueltas = difat0, 0
        while sec < _MARCA and vueltas <= n_difat + 1:
            bloque = struct.unpack_from("<%dI" % (ssz // 4), datos, 512 + sec * ssz)
            difat.extend(bloque[:-1])
            sec = bloque[-1]
            vueltas += 1

        fat = []
        for s in difat:
            if s < _MARCA:
                fat.extend(struct.unpack_from("<%dI" % (ssz // 4), datos, 512 + s * ssz))

        def cadena(inicio):
            out, s, n = [], inicio, 0
            while s < _MARCA and n < 2000000:
                out.append(s)
                s = fat[s] if s < len(fat) else 0xFFFFFFFE
                n += 1
            return out

        def leer_sectores(inicio, size):
            trozos = (datos[512 + s * ssz:512 + (s + 1) * ssz] for s in cadena(inicio))
            return b"".join(trozos)[:size]

        entradas = []
        for s in cadena(dir0):
            off = 512 + s * ssz
            for i in range(ssz // 128):
                e = off + i * 128
                nlen = struct.unpack_from("<H", datos, e + 64)[0]
                if nlen < 2:
                    continue
                entradas.append((
                    datos[e:e + nlen - 2].decode("utf-16-le", "replace"),
                    datos[e + 66],
                    struct.unpack_from("<I", datos, e + 116)[0],
                    struct.unpack_from("<I", datos, e + 120)[0]))
        if not entradas:
            return None, "el .mxd no tiene directorio legible"

        raiz = entradas[0]
        mini = leer_sectores(raiz[2], raiz[3])
        mfat = []
        for s in cadena(mfat0):
            mfat.extend(struct.unpack_from("<%dI" % (ssz // 4), datos, 512 + s * ssz))

        def leer_mini(inicio, size):
            out, s, n = [], inicio, 0
            while s < _MARCA and n < 2000000:
                out.append(mini[s * mssz:(s + 1) * mssz])
                s = mfat[s] if s < len(mfat) else 0xFFFFFFFE
                n += 1
            return b"".join(out)[:size]

        for nombre, tipo, inicio, size in entradas:
            if nombre != "Version" or tipo != 2 or size < 6:
                continue
            raw = leer_mini(inicio, size) if size < 4096 else leer_sectores(inicio, size)
            if len(raw) < 4:
                return None, "el stream Version salió vacío"
            nbytes = struct.unpack_from("<I", raw, 0)[0]
            if nbytes <= 0 or nbytes > len(raw) - 4:
                return None, "el stream Version tiene una longitud incoherente"
            txt = raw[4:4 + nbytes].decode("utf-16-le", "replace").rstrip("\x00")
            return (txt.strip() or None), (None if txt.strip() else "versión vacía")

        return None, "el .mxd no tiene stream Version"
    except (struct.error, IndexError, ValueError) as exc:
        return None, "estructura del .mxd ilegible: %s" % exc


def _a_tupla(version):
    """'10.5' -> (10, 5). Devuelve None si no tiene forma de versión."""
    if not version:
        return None
    partes = []
    for trozo in str(version).split("."):
        trozo = trozo.strip()
        if not trozo.isdigit():
            break
        partes.append(int(trozo))
    return tuple(partes) or None


@mcp.tool()
def ping() -> dict:
    """
    Comprueba que el puente dentro de ArcMap responde (y versión de ArcGIS).

    Los tres estados posibles y qué hacer con cada uno:
      - Responde            → puente vivo, adelante.
      - `estado` = `puente_caido`     → nadie escucha: ArcMap cerrado o sin pulsar
        'Iniciar'. Reintentar no arregla nada.
      - `estado` = `puente_ocupado`   → el add-in está cargado pero ArcMap no
        contesta, típicamente porque hay un geoproceso corriendo en su hilo
        principal. El trabajo sigue vivo: esperar, no relanzar.

    Desde el add-in 2.9.0, `ping` NO pasa por el candado del puente: cuando hay un
    comando en curso responde igualmente y al instante, **sin tocar el hilo de
    ArcMap** (que puede ser justo lo atascado), y trae `estado: ocupado` junto con
    `comando_en_curso` y `ocupado_desde_s`. Úsalo antes de dar nada por colgado: la
    diferencia entre "lleva 8 s con un export" y "lleva 1.400 s con execute_code" es
    la diferencia entre esperar y actuar. Un chequeo de salud que solo contesta
    cuando todo va bien no sirve para nada.

    El único caso en que `ping` sí puede tardar es con el puente LIBRE y el hilo de
    ArcMap ocupado por otra cosa (un geoproceso lanzado a mano, un diálogo modal):
    entonces espera al hilo y puede acabar en timeout. Que no responda nunca es señal
    de puente caído, no de puente ocupado.
    """
    return _client.send("ping")


@mcp.tool()
def get_arcmap_info() -> dict:
    """
    Info del documento ArcMap abierto: ruta del .mxd, data frames, escala activa.

    UNA INSTANCIA, NO TODAS. Devuelve el documento de la instancia de ArcMap que
    tiene el puente, no "el ArcMap abierto" ni todos los abiertos. El puerto (27179)
    es único, así que lo agarra el primer ArcMap donde se pulse 'Iniciar'; si tienes
    diez ventanas de ArcMap abiertas, nueve son invisibles para este servidor y aquí
    verás una sola.

    Si esperabas ver otro documento, no es que falle: es que el puente vive en otra
    ventana. Ciérrala o arranca el puente donde toca.
    """
    return _client.send("get_arcmap_info")


@mcp.tool()
def list_layers() -> dict:
    """
    Lista las capas del data frame activo (nombre, visibilidad, fuente, def. query).

    Cada capa trae además su `ruta` dentro de la TOC (`Grupo/Subgrupo/Capa`). Esa
    ruta vale como identificador en CUALQUIER tool que pida una capa, y hace falta
    cuando hay nombres repetidos: si `capa="Parcelas"` casa con dos capas, la tool
    NO elige por ti (antes se quedaba con la primera en silencio, también en
    `remove_layer`): falla listando las rutas candidatas, y repites con la ruta.
    Dos capas homónimas dentro del MISMO grupo (el mismo shapefile añadido dos veces)
    tendrían la misma ruta, así que se numeran en orden de TOC: `Parcelas#1`,
    `Parcelas#2`. Usa la ruta tal cual la devuelve este listado.
    Un data frame vacío devuelve `num: 0`, no un error.
    """
    return _client.send("list_layers")


@mcp.tool()
def zoom_to_layer(nombre: str) -> dict:
    """Encuadra el mapa al extent de una capa por nombre y refresca el canvas vivo."""
    return _client.send("zoom_to_layer", {"nombre": nombre})


@mcp.tool()
def export_pdf(salida: str, dpi: int = 300, sobrescribir: bool = True) -> dict:
    """
    Exporta el layout del documento ABIERTO en ArcMap a un PDF en la ruta `salida`.

    Solo sabe exportar el documento abierto; la respuesta trae `documento` con su
    ruta para que se pueda comprobar. Para exportar OTROS .mxd del disco (una serie
    de planos), `export_mxd_lote`.

    `salida` debe ser una ruta ABSOLUTA a una carpeta que exista; `dpi` entre 24 y
    600. `sobrescribir` (True por defecto, que es lo que necesita una serie de
    planos que se regenera) decide qué pasa si el fichero ya existe: con False se
    devuelve error sin tocarlo. La respuesta trae `sobrescrito` para que pisar un
    plano nunca sea silencioso. Se exporta a un temporal y solo al final se mueve,
    así que cancelar con ESC ya no se lleva por delante el PDF que había.

    Un layout denso a dpi alto se va a minutos: el add-in lo trata como comando
    largo y la espera de aquí la gobierna ARCMAP_GP_TIMEOUT, no el timeout corto.
    """
    return _exportar("export_pdf", {"salida": salida, "dpi": dpi,
                                    "sobrescribir": sobrescribir})


@mcp.tool()
def export_jpg(salida: str, dpi: int = 230, sobrescribir: bool = True) -> dict:
    """
    Exporta el layout del documento ABIERTO en ArcMap a un JPG en la ruta `salida`
    (`dpi` 230 por defecto, calidad JPEG 95). Útil para adjuntar planos por correo
    o incrustarlos en documentos sin el peso de un PDF.

    Solo sabe exportar el documento abierto, y la respuesta trae `documento` con su
    ruta: compruébalo. Para exportar OTROS .mxd del disco, `export_mxd_lote`. Un
    argumento que la tool no declara (`mxd=`, `resolucion=`) es error, no se ignora.
    Si ArcMap sigue dibujando (E_PENDING) se reintenta hasta 3 veces y se dice.

    Mismas reglas que `export_pdf`: ruta absoluta, `dpi` 24-600, `sobrescribir` y
    `sobrescrito` en la respuesta. Vale `.jpg` o `.jpeg`. La espera la gobierna
    ARCMAP_GP_TIMEOUT.
    """
    return _exportar("export_jpg", {"salida": salida, "dpi": dpi,
                                    "sobrescribir": sobrescribir})


@mcp.tool()
def refresh() -> dict:
    """Refresca la vista activa y la tabla de contenidos de ArcMap."""
    return _client.send("refresh")


@mcp.tool()
def execute_arcpy(code: str, usar_documento: bool | None = None,
                  serializar_sesion: bool = False) -> dict:
    """
    Ejecuta código arcpy ARBITRARIO fuera del proceso de ArcMap (Python 2.7).

    El código dispone siempre de:
      - arcpy            : el módulo arcpy
      - MAP / mapping    : arcpy.mapping
    y debe ser Python 2.7 válido (sin f-strings), asignando el resultado a `RESULT`.

    Si el código usa el DOCUMENTO, se le añaden además:
      - mxd              : una COPIA del documento abierto (no la sesión viva)
      - df               : su data frame activo

    Esa copia se toma del .mxd GUARDADO EN DISCO, que es instantáneo y no molesta a
    ArcMap. La contrapartida: los cambios que la sesión aún no haya guardado no
    aparecen. Guarda el documento (`save_mxd`) o pasa `serializar_sesion=True` para
    que ArcMap serialice la sesión tal cual está — cuidado, eso ocupa ArcMap durante
    la operación y en documentos muy pesados puede llegar a tumbarlo.

    Solo se copia el documento cuando hace falta: se decide mirando si el código
    menciona `mxd` o `df` como variable (`MAP`/`mapping` ya no cuentan: existen
    siempre, con o sin copia). `usar_documento` fuerza esa decisión: `False`
    no copia nunca (más rápido, pero `mxd` y `df` no existirán), `True` copia
    siempre. Déjalo sin indicar para que se decida solo.

    COSTE. Lo medido, y ninguna cifra sirve sin su condición:

      - Suelo fijo, sin documento (`usar_documento=False`): ~6,4 s, de los cuales
        ~6,2 son `import arcpy`. Cada llamada arranca un intérprete desde cero.
        Ese suelo es una DECISIÓN ACEPTADA, no un pendiente de optimizar (ADR-005,
        2026-08-28): un runner persistente con arcpy ya importado lo bajaría a casi
        cero, pero reintroduce el estado compartido entre llamadas y convierte el
        runner huérfano —que hoy es un caso raro, y ya impide cerrar ArcMap— en el
        modo normal de operación, que es justo lo que costó dos rediseños quitar de
        en medio. Si algún día duele, la vía acordada NO es un proceso permanente:
        es reutilizar UN intérprete dentro de una operación por lotes.
      - Abrir el documento en sí: **menos de un segundo** (0,7 s medidos sobre un
        .mxd de 36 capas el 2026-08-27). Abrir NO es caro.

    Lo caro, cuando pasa, es un **bloqueo al abrir** que no se ve venir: el
    `import arcpy` va normal y la espera aparece en `MapDocument()`. De ahí salieron
    los 324,5 s que este docstring dio durante un tiempo como coste normal de abrir, y
    los timeouts de 900 s. El 2026-08-27 el mismo .mxd pasó de 0,7 s a más de 180 s
    bloqueado, y se atribuyó a que ArcMap tenía la licencia tomada. **Esa causa está
    descartada**: el 2026-09-25 abrió igual (~8,5 s) con ArcMap cerrado, abierto con
    ese mismo documento y congelado. La causa real sigue sin identificar.

    Si ves esperas largas abriendo documentos, no subas el timeout a ciegas: mira si
    hay `python.exe` de ArcGIS huérfanos de llamadas anteriores y si responden las
    unidades de red de las capas. Para inspeccionar sin abrir nada, `describe_mxd`
    (milisegundos, sin arcpy y sin licencia).

    El resultado se devuelve asignando `RESULT` (en MAYÚSCULAS; `result` en minúscula,
    la convención del MCP de ArcGIS Pro, también se devuelve, con un `aviso_result`).
    La cabecera `# -*- coding: utf-8 -*-` se tolera. Si el código lanza una excepción,
    la respuesta de error trae igualmente el `stdout` impreso hasta ese punto: en un
    bucle sobre documentos, imprime una línea por documento hecho y sabrás por dónde
    iba. Un `UnicodeEncodeError` suele ser `str(ex)` sobre un mensaje con tildes:
    usa `unicode(ex)`.

    Para EXPORTAR varios .mxd no uses un bucle aquí: un proceso arcpy que ya ha
    exportado un layout no vuelve a exportar otro. Usa `export_mxd_lote`.

    NO uses `sys.exit()` ni `exit()`: el resultado se devuelve asignando `RESULT`.
    Si aun así sales, se te responde igual —con el `RESULT` que ya hubieras asignado
    y un `aviso_salida`, o con un error que lo explica si no había ninguno—, pero
    antes el proceso moría sin escribir nada y la llamada parecía colgada.

    Si tu código no usa `mxd` ni `df`, pasa `usar_documento=False`: te ahorras el
    riesgo entero, no solo unos segundos. Pasado `ARCMAP_EXEC_TIMEOUT` (900 s por
    defecto) el add-in mata el subproceso y devuelve un error que dice en qué fase
    se quedó (importando arcpy / abriendo documento / ejecutando codigo), en vez de
    esperar en silencio.

    Ejemplos:
        RESULT = [l.name for l in MAP.ListLayers(mxd)]        # copia el documento
        arcpy.gp.Reclassify_sa(r"C:\\slope.tif", "Value",
                               "0 12 1;12 24 2;24 48 3", r"C:\\slope_rec.tif")
        RESULT = "hecho"                                      # no lo copia
    """
    params: dict = {"code": code}
    if usar_documento is not None:
        params["usar_documento"] = usar_documento
    if serializar_sesion:
        params["serializar_sesion"] = True
    return _client.send("execute_code", params,
                        timeout=EXEC_SESION_TIMEOUT if serializar_sesion else EXEC_TIMEOUT)


# --------------------------------------------------------------------------- #
# Series de planos (Data Driven Pages + layout).
# --------------------------------------------------------------------------- #

@mcp.tool()
def list_ddp(max_valores: int = 500) -> dict:
    """
    Inspecciona las Data Driven Pages (atlas) del documento abierto.

    Devuelve si están habilitadas, número de páginas, campo y capa índice, y la
    lista de valores del campo índice (los "nombres" de cada plano). No cambia la
    página actual. Útil como primer paso antes de exportar una serie.
    `max_valores` acota la lista de valores devuelta (500 por defecto); si se
    trunca, la respuesta lo indica con `valores_truncados=true`.

    Se resuelve sobre una COPIA del documento, y copiarlo puede costar minutos en
    una sesión cargada: la espera de aquí la gobierna ARCMAP_FONDO_TIMEOUT.
    """
    return _client.send("list_ddp", {"max_valores": max_valores},
                        timeout=FONDO_TIMEOUT)


@mcp.tool()
def export_ddp(salida: str, modo: str = "ALL", rango: str = None,
               valores: list = None, un_pdf_por_pagina: bool = False,
               dpi: int = 300) -> dict:
    """
    Exporta el atlas (Data Driven Pages) a PDF.

    Selección de páginas (usa SOLO una):
      - `modo="ALL"`      : todas las páginas (por defecto).
      - `modo="CURRENT"`  : solo la página activa.
      - `rango="1-3,5"`   : por IDs de página (1-based).
      - `valores=[...]`   : por valores del campo índice (ej. expedientes concretos);
                            se convierten a IDs de página automáticamente.
    `un_pdf_por_pagina=False` -> un único PDF multipágina; True -> un PDF por página
    (nombrado por el valor del índice). `dpi` resolución (300 por defecto).

    Con `valores`, los que NO existan en la capa índice vuelven listados en
    `valores_no_encontrados` y con un `aviso`: la exportación sigue con los que sí
    casan, pero nunca en silencio (un expediente mal tecleado daba un PDF más corto
    y ninguna señal).

    Es de las llamadas más lentas del puente —copia del documento más exportación
    página a página—: la espera la gobierna ARCMAP_FONDO_TIMEOUT, no el timeout corto.
    """
    return _client.send("export_ddp", {
        "salida": salida, "modo": modo, "rango": rango, "valores": valores,
        "un_pdf_por_pagina": un_pdf_por_pagina, "dpi": dpi,
    }, timeout=FONDO_TIMEOUT)


@mcp.tool()
def list_layout_elements(tipo: str = None, patron: str = None) -> dict:
    """
    Lista los elementos del layout (página) con su nombre y tipo de clase.

    `tipo` filtra por tipo arcpy: TEXT_ELEMENT, LEGEND_ELEMENT, PICTURE_ELEMENT,
    MAPSURROUND_ELEMENT, DATAFRAME_ELEMENT, GRAPHIC_ELEMENT (None = todos).
    `patron` es un comodín opcional sobre el nombre (ej. "Titulo*").
    En los elementos de texto incluye su contenido actual (`texto`).
    """
    return _client.send("list_layout_elements", {"tipo": tipo, "patron": patron})


@mcp.tool()
def set_legend_item(capa: str, mostrar_nombre: bool = None,
                    mostrar_encabezado: bool = None, mostrar_etiquetas: bool = None,
                    leyenda: str = None, indice: int = None) -> dict:
    """
    Cambia QUÉ muestra la entrada de una capa en la leyenda del layout: el nombre de
    la capa, el encabezado y las etiquetas de sus clases. **No cambia CÓMO se ve**:
    las fuentes y tamaños se quedan como estaban, y la respuesta los devuelve leídos
    (`fuentes`) para comprobarlo. Solo se toca lo que se pase; None = no cambiar.
    Sin ningún interruptor, devuelve el estado actual sin tocar nada.

    `capa` es el nombre con que la capa aparece en la leyenda. Si está en varias
    leyendas (o dos veces en una), no se elige por ti: el error las lista; se
    desempata con `leyenda` (nombre del elemento, ver `list_layout_elements`) o con
    `indice` (el número de esa lista).

    Es la alternativa a aplicar un estilo de leyenda de ESRI.style (lo que haría
    arcpy con `updateItem`), que cambia también las fuentes del elemento.
    """
    params: dict = {"capa": capa, "leyenda": leyenda}
    for clave, valor in (("mostrar_nombre", mostrar_nombre),
                         ("mostrar_encabezado", mostrar_encabezado),
                         ("mostrar_etiquetas", mostrar_etiquetas),
                         ("indice", indice)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_legend_item", params)


@mcp.tool()
def set_labels(capa: str, expresion: str = None, tamano: float = None,
               color: str | list = None, halo: float = None,
               color_halo: str | list = None, activar: bool = None,
               clase: str = None) -> dict:
    """
    Etiquetas de una capa de entidades: expresión, tamaño, color y halo.

    Solo se toca lo que se pase; None = no cambiar. Sin ningún parámetro, devuelve
    el estado actual sin tocar nada. Si cambias algo y no dices `activar`, las
    etiquetas se encienden; `activar=False` las apaga.

    - `expresion`: expresión simple de ArcMap, con los campos entre corchetes:
      `"[NOMBRE]"`, `'[COD] & " " & [NOMBRE]'`.
    - `tamano`: en puntos. `color` y `color_halo`: `[R, G, B]` o `"#RRGGBB"`.
    - `halo`: grosor en puntos; `0` lo quita. El color del halo solo cambia si
      pasas `color_halo` (blanco si la capa no tenía ninguno).
    - `clase`: aplica solo a esa clase de etiquetas. Sin ella, a todas.

    **Modifica las clases que ya tiene la capa; no las borra ni las crea.** Es lo que
    funciona con los dos motores de etiquetado: en un mapa con **Maplex**, sustituir
    las clases por unas nuevas deja la capa sin etiquetas y sin ningún error. Solo si
    la capa no tiene ninguna clase se crea una (hace falta `expresion`), preparada para
    el motor del mapa. La respuesta dice el `motor`, si se creó la clase
    (`clase_creada`) y cómo quedó cada clase **leída de vuelta de la capa**, junto con
    `antes`.
    """
    params: dict = {"capa": capa}
    for clave, valor in (("expresion", expresion), ("tamano", tamano), ("color", color),
                         ("halo", halo), ("color_halo", color_halo),
                         ("activar", activar), ("clase", clase)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_labels", params)


@mcp.tool()
def set_text_element(texto: str, nombre: str = None, buscar: str = None,
                     grupo: str = None, indice: int = None) -> dict:
    """
    Cambia el contenido de un elemento de texto del layout (título, fecha, nº de
    expediente...), también si está DENTRO de un grupo (el cajetín agrupado). Indica
    el `texto` nuevo y UN selector:

      - `nombre`: el .name del elemento (si está nombrado en ArcMap).
      - `buscar`: su texto ACTUAL (find-and-replace). Coincidencia exacta y, si no,
        por subcadena. Útil cuando los textos del layout no están nombrados (lo
        habitual).

    Si el selector casa con VARIOS elementos no se elige por ti: el error los lista
    numerados, con su texto y su grupo. Se desempata con `grupo` (nombre del grupo que
    lo contiene; `""` para los sueltos) o, como último recurso, con `indice` (el número
    de esa lista). Hace falta de verdad: un cajetín copiado de otro plano arrastra
    nombre Y texto, y sus elementos son idénticos en todo lo demás.
    """
    params = {"nombre": nombre, "buscar": buscar, "texto": texto}
    if grupo is not None:
        params["grupo"] = grupo
    if indice is not None:
        params["indice"] = indice
    return _client.send("set_text_element", params)


@mcp.tool()
def goto_ddp_page(pagina: int = None, valor: str = None) -> dict:
    """
    Sitúa el atlas en una página y refresca la vista. Indica `pagina` (ID 1-based)
    o `valor` (un valor del campo índice; se resuelve a su página). Devuelve el ID,
    el valor de la página y la escala resultante.

    Resuelve la página sobre una COPIA del documento y aplica el encuadre a la
    sesión viva: la espera la gobierna ARCMAP_FONDO_TIMEOUT.
    """
    return _client.send("goto_ddp_page", {"pagina": pagina, "valor": valor},
                        timeout=FONDO_TIMEOUT)


@mcp.tool()
def set_definition_query(capa: str, query: str = None) -> dict:
    """
    Fija la definition query (filtro SQL) de una capa del data frame activo.
    `query` vacío o None limpia el filtro. Útil para planos temáticos por filtro.
    Devuelve la query anterior y la nueva.
    """
    return _client.send("set_definition_query", {"capa": capa, "query": query})


@mcp.tool()
def set_layer_visibility(capa: str, visible: bool) -> dict:
    """
    Enciende o apaga una capa (o grupo) por nombre en el data frame activo y refresca.
    """
    return _client.send("set_layer_visibility", {"capa": capa, "visible": visible})


@mcp.tool()
def export_view_png(salida: str, dpi: int = 150, ancho: int = None,
                    alto: int = None, modo: str = "vista",
                    sobrescribir: bool = True) -> dict:
    """
    Exporta a un PNG en disco la vista del mapa (`modo="vista"`, data frame activo)
    o la página de layout completa (`modo="layout"`).

    Genera un ARCHIVO (artefacto reutilizable). `dpi` 150 por defecto (24-600);
    `ancho`/`alto` en píxeles opcionales (hasta 10000). `salida` absoluta;
    `sobrescribir` y `sobrescrito` como en `export_pdf`. Para que el agente VEA el
    mapa al instante sin abrir el archivo, usa `get_canvas_screenshot`. Para el
    entregable final usa `export_pdf` / `export_ddp`.

    El add-in lo trata como comando largo (un layout a dpi alto no se renderiza en
    60 s): la espera la gobierna ARCMAP_GP_TIMEOUT.
    """
    return _exportar("export_view_png", {
        "salida": salida, "dpi": dpi, "ancho": ancho, "alto": alto, "modo": modo,
        "sobrescribir": sobrescribir,
    })


@mcp.tool()
def get_canvas_screenshot(modo: str = "vista", dpi: int = 96):
    """
    Captura el mapa y lo devuelve como IMAGEN EN LÍNEA para que el agente lo vea al
    instante (equivalente a get_canvas_screenshot de QGIS), sin escribir un archivo.

    `modo="vista"` renderiza el data frame activo; `modo="layout"` la página completa.
    `dpi` 96 por defecto (payload pequeño; sube para más detalle). Ideal en bucles de
    QA visual: aplicar un filtro/escala y "ver" el resultado.
    """
    resp = _client.send("get_canvas_screenshot", {"modo": modo, "dpi": dpi})
    if not resp.get("ok"):
        return resp  # error legible (sin puente, etc.)
    data = base64.b64decode(resp["result"]["imagen_b64"])
    return Image(data=data, format="png")


# --------------------------------------------------------------------------- #
# Capas y datos (preparar y consultar).
# --------------------------------------------------------------------------- #

@mcp.tool()
def select_by_attribute(capa: str, where: str) -> dict:
    """
    Selecciona entidades de una capa por SQL (NEW_SELECTION) y refresca.
    `where` es la cláusula SQL (ej. "ESTRATO = 'Pinar'"). Devuelve el nº seleccionado.
    """
    return _client.send("select_by_attribute", {"capa": capa, "where": where})


@mcp.tool()
def clear_selection(capa: str = None) -> dict:
    """
    Limpia la selección de una capa (`capa`=nombre) o de TODAS las capas del data
    frame activo si no se indica `capa`.
    """
    return _client.send("clear_selection", {"capa": capa})


@mcp.tool()
def get_unique_values(capa: str, campo: str, where: str = None,
                      max_valores: int = 1000) -> dict:
    """
    Devuelve los valores únicos (ordenados) de un campo de una capa. Respeta la
    definition query. `where` opcional para acotar. Útil para iterar planos por
    categoría (un plano por estrato, por municipio, etc.).

    `max_valores` (1000 por defecto, hasta 100000) corta el recorrido: la tabla se
    lee en el hilo de ArcMap, y pedir un campo casi único (OBJECTID, una coordenada)
    sobre una capa regional lo dejaba congelado hasta el final. Si se corta, la
    respuesta trae `truncado: true`: nunca se recorta en silencio.
    """
    return _client.send("get_unique_values", {"capa": capa, "campo": campo,
                                              "where": where, "max_valores": max_valores})


@mcp.tool()
def count_features(capa: str, where: str = None) -> dict:
    """
    Cuenta entidades de una capa. Con `where` cuenta las que cumplen el filtro SIN
    alterar la selección actual; sin `where`, el total de la capa.
    """
    return _client.send("count_features", {"capa": capa, "where": where})


@mcp.tool()
def list_fields(capa: str) -> dict:
    """Lista los campos de una capa/tabla: nombre, tipo, alias y longitud."""
    return _client.send("list_fields", {"capa": capa})


@mcp.tool()
def get_layer_info(capa: str) -> dict:
    """
    Detalle de una capa: tipo de geometría, CRS, extent, nº de entidades, campos y
    ruta de la fuente. Un vistazo completo antes de filtrar o simbolizar.
    """
    return _client.send("get_layer_info", {"capa": capa})


@mcp.tool()
def add_layer(fuente: str, posicion: str = "TOP", grupo: str = None,
              nombre: str = None, en_leyenda: bool = True, visible: bool = None,
              subcapas: list = None) -> dict:
    """
    Añade una capa al data frame activo desde una ruta a shapefile, feature class de
    file geodatabase, ráster, **`.lyr`** o **URL de un servicio WMS**. `posicion`:
    TOP / BOTTOM / AUTO_ARRANGE. `grupo` opcional = nombre de una capa de grupo YA
    EXISTENTE donde insertarla (para crearlo, `add_group`). Para recolocarla después,
    `move_layer`. `visible=False` la añade apagada.

    **WMS**: `fuente` = URL del servicio (`https://www.ign.es/wms-inspire/mapa-raster`).
    Un WMS recién conectado trae TODAS sus subcapas apagadas, y encender solo el nodo
    padre no pinta nada: aquí se encienden las de `subcapas` (nombre o ruta
    `Grupo/Subcapa`, con los grupos que las contienen) o todas si no se dice. Una
    subcapa que no existe es error y no se añade nada. La respuesta trae `servicio`
    con el árbol de subcapas leído de vuelta. **Ojo**: con un WMS encendido, cada
    zoom o captura lo redibuja por la red en el hilo de ArcMap; para montar un
    documento, añádelo con `visible=False` y enciéndelo al final.

    `nombre`: cómo se llamará la capa en la TOC. Sin esto entra con el nombre del
    fichero (`Vis_plataforma_oeste.tif`), que es el que acaba saliendo en la
    leyenda del plano.

    **Por defecto la capa entra también en la LEYENDA del plano**: las leyendas de
    ArcMap tienen «añadir capas nuevas» activado, y en un plano que ya va justo una
    capa más la desborda. Con `en_leyenda=False` se apaga eso solo mientras entra
    esta capa y se deja como estaba; las leyendas del documento no cambian.

    **Un `.lyr` entra con todo lo que lleve dentro**: nombre, simbología,
    transparencia y, si es un `.lyr` de GRUPO, el árbol entero con sus
    visibilidades. Es la vía para reproducir de una vez la estructura de grupos de
    un proyecto de QGIS. Lo que no trae un `.lyr` de datos sueltos no se inventa:
    el etiquetado sigue sin viajar.
    """
    params: dict = {"fuente": fuente, "posicion": posicion, "grupo": grupo}
    if nombre is not None:
        params["nombre"] = nombre
    if not en_leyenda:
        params["en_leyenda"] = False
    if visible is not None:
        params["visible"] = visible
    if subcapas is not None:
        params["subcapas"] = subcapas
    return _client.send("add_layer", params)


@mcp.tool()
def add_group(nombre: str, posicion: str = "TOP", grupo: str = None,
              visible: bool = True, en_leyenda: bool = True) -> dict:
    """
    Crea una capa de GRUPO vacía en el data frame activo, para luego meter capas
    con `add_layer(..., grupo=nombre)`. `posicion`: TOP / BOTTOM. `grupo`: grupo YA
    EXISTENTE dentro del cual crearlo (None = en la raíz de la TOC). `visible`:
    encendido o apagado (un grupo apagado sirve de «pendiente de integrar»).

    `en_leyenda=False` evita que el grupo entre en la leyenda del plano, igual que
    en `add_layer`. Si ya hay una capa con ese nombre, el grupo se crea igual y la
    respuesta trae un `aviso`: a partir de ahí hay que nombrarlo por su ruta.
    """
    params: dict = {"nombre": nombre, "posicion": posicion, "grupo": grupo,
                    "visible": visible}
    if not en_leyenda:
        params["en_leyenda"] = False
    return _client.send("add_group", params)


@mcp.tool()
def remove_layer(capa: str) -> dict:
    """Quita una capa del data frame activo por nombre."""
    return _client.send("remove_layer", {"capa": capa})


@mcp.tool()
def move_layer(capa: str, referencia: str = None, posicion: str = None,
               grupo: str = None) -> dict:
    """
    Recoloca una capa (o un grupo) en la TOC del data frame activo **sin quitarla y
    volverla a añadir**: conserva simbología, etiquetas, lo tocado a mano y sus
    entradas de leyenda.

    - `referencia` + `posicion="BEFORE"` (por defecto) o `"AFTER"`: justo encima o
      debajo de otra capa, en el grupo de esa capa (la mueve de grupo si hace falta).
    - `posicion="TOP"` o `"BOTTOM"` (por defecto TOP si no hay referencia): arriba o
      abajo del todo de `grupo`; sin `grupo`, del grupo en que ya está. `grupo="/"`
      es la raíz de la TOC.
    - `capa`, `referencia` y `grupo` por nombre o ruta `Grupo/Capa`, como el resto.

    Meter un grupo dentro de sí mismo es error. La respuesta trae `orden`: las capas
    del grupo de destino leídas de vuelta, de arriba abajo.
    """
    params: dict = {"capa": capa}
    params.update({k: v for k, v in {"referencia": referencia, "posicion": posicion,
                                     "grupo": grupo}.items() if v is not None})
    return _client.send("move_layer", params)


@mcp.tool()
def apply_symbology_from_layer(capa: str, lyr_file: str) -> dict:
    """
    Aplica la simbología de un archivo `.lyr` (estilos canónicos) a una capa
    del mapa. `lyr_file` es la ruta al .lyr de origen.

    Vale para capas de ENTIDADES y para capas RÁSTER. En ráster es la vía para
    los renderers que ninguna tool sabe construir —valores únicos, colormap, RGB
    compuesto—: `set_raster_symbology` solo clasifica o estira, y sobre un ráster
    categórico (una máscara 0/1/2, una reclasificación, un `paletted` traído de
    QGIS) la clasificación falla porque no hay histograma que cortar. El camino
    entonces es fabricar el `.lyr` con `execute_arcpy` + ArcObjects y aplicarlo
    aquí.

    **En ráster viaja también la TRANSPARENCIA del `.lyr`**; en entidades se copia
    solo el renderer. Es deliberado: en un ráster de visibilidad o de afección la
    opacidad no es decoración, es lo que deja ver la ortofoto debajo.

    Del `.lyr` se toma la primera capa del tipo que toque, aunque venga dentro de
    un grupo. Lo que NO viaja: el nombre de la capa y el etiquetado.
    """
    return _client.send("apply_symbology_from_layer", {"capa": capa, "lyr_file": lyr_file})


@mcp.tool()
def set_graduated_symbology(capa: str, campo: str, num_clases: int = 5,
                            metodo: str = "natural_breaks",
                            color_desde: list | str = None,
                            color_hasta: list | str = None,
                            tamano: float = None, algoritmo: str = None) -> dict:
    """
    Simboliza una capa VIVA con colores graduados (class breaks) por un campo
    NUMÉRICO, directamente sobre la sesión de ArcMap (persiste; se guarda con
    save_mxd). Cubre el caso que execute_arcpy no puede —opera sobre una copia del
    documento y descarta el renderer— y sin necesitar un .lyr plantilla como
    apply_symbology_from_layer.

    - `campo`: campo numérico a clasificar (ej. "Elevation").
    - `num_clases`: nº de clases (2-32, defecto 5). El método puede reducirlo si no
      hay variación suficiente.
    - `metodo`: natural_breaks (defecto) | quantile | equal_interval |
      geometrical_interval | standard_deviation.
    - `color_desde` / `color_hasta`: extremos de la rampa, como RGB [r,g,b] 0-255 o
      como "#RRGGBB" (defecto amarillo claro -> rojo oscuro). Un color mal formado
      es ERROR: antes se ignoraba y salía la rampa por defecto sin decir nada.
    - `tamano`: grosor de línea / tamaño de punto / grosor de borde de polígono en
      puntos (defecto según geometría).
    - `algoritmo`: cómo se interpola entre los dos extremos. `cielab` (defecto) |
      `lablch` | `hsv`. Mismos valores que en `set_raster_symbology`, y por el mismo
      motivo: HSV interpola el TONO y entre amarillo y rojo se pasea por verde, azul
      y magenta, o sea un arcoíris donde se pedía una rampa secuencial. Hasta la
      2.11.0 el defecto aquí era HSV; pasa `hsv` solo si necesitas reproducir
      exactamente los colores de una serie ya entregada.

    El histograma usa TODOS los valores del campo en la fuente (ignora definition
    query y selección); para clasificar un subconjunto, fíjalo con una definition
    query permanente antes. Admite campos de una tabla unida (join).
    """
    params = {"capa": capa, "campo": campo, "num_clases": num_clases, "metodo": metodo}
    if color_desde is not None:
        params["color_desde"] = color_desde
    if color_hasta is not None:
        params["color_hasta"] = color_hasta
    if tamano is not None:
        params["tamano"] = tamano
    if algoritmo is not None:
        params["algoritmo"] = algoritmo
    return _client.send("set_graduated_symbology", params)


@mcp.tool()
def set_raster_symbology(capa: str, modo: str = "clasificado", num_clases: int = 5,
                         color_desde: list = None, color_hasta: list = None,
                         colores: list = None, etiquetas: list = None,
                         algoritmo: str = None, valores: list = None,
                         transparentes: list = None, transparencia: int = None) -> dict:
    """
    Simboliza una capa RÁSTER de la TOC: clasificada, estirada o por valores únicos.

    Es la herramienta para NDVI, FCC, P95, pendientes y demás producto ráster.
    `set_graduated_symbology` NO vale aquí: solo acepta capas de entidades.
    Tampoco vale `execute_arcpy`, que opera sobre una copia y descarta los
    cambios de renderer.

    `modo`:
      - `clasificado` (por defecto): `num_clases` clases (2-32) con rampa de color.
      - `estirado`: rampa continua entre el mínimo y el máximo de la banda 0.
      - `unico`: un color por valor de píxel. **Es el modo de un ráster CATEGÓRICO**
        —máscara de visibilidad, FCC binario, reclasificación, `paletted` de QGIS—,
        donde el clasificado falla con un E_FAIL de COM porque no hay histograma
        que cortar, y donde clasificar sería mentir sobre el dato.

    MODO `unico`:
      - `valores`: lista de valores de píxel a pintar (ej. `[1, 2]`). Obligatorio;
        no se deducen del ráster, porque adivinarlos sería inventar la leyenda.
      - `colores` / `etiquetas`: uno por valor. Sin `colores`, tonos repartidos por
        el círculo cromático (distinguir, no ordenar).
      - `transparentes`: valores que NO se pintan y que tampoco salen en la leyenda
        (ej. `[0]`). Es la traducción exacta del `paletted` de QGIS, donde lo que no
        está en la paleta queda transparente. **Sin esto el fondo tapa el mapa**, y
        en una máscara de visibilidad el fondo suele ser el 90 % del ráster.

    `transparencia` (0-100, cualquier modo): opacidad de la capa. La opacidad 0,6 de
    QGIS es `transparencia=40`.

    COLOR. Hay dos vías, y la explícita manda:

      - `colores`: una lista de `num_clases` colores `[R, G, B]`, uno por clase.
        Es la vía FIABLE cuando las clases son categóricas o los cortes vienen
        fijados por percentil: ahí el color no es un gradiente que se pueda
        derivar de dos extremos, es una decisión por clase.
      - `color_desde` / `color_hasta`: los dos extremos de una rampa. Por defecto
        va de amarillo claro `[255, 255, 178]` a rojo oscuro `[189, 0, 38]`.

    `etiquetas`: lista de `num_clases` rótulos de leyenda. Cuando los cortes salen
    de una reclasificación, el número del corte no dice nada ("1 – 2") y lo que
    sirve es el significado ("Defoliación fuerte"). Sin esto, cada clase se rotula
    con su rango.

    `algoritmo` de interpolación de la rampa: `cielab` (por defecto), `lablch` o
    `hsv`. **No uses `hsv` para nada cuantitativo**: interpola el tono dando la
    vuelta a la rueda de color, así que entre dos rojos separados 27° saca un
    arcoíris que pasa por morado, turquesa y verde. Era el comportamiento por
    defecto hasta el 2026-09-04; CIE Lab interpola en espacio perceptualmente
    uniforme y sí produce secuencias que se leen como orden.

    Si el ráster no tiene estadísticas calculadas, se calculan solas y la respuesta trae
    `estadisticas_calculadas` con el tiempo. Los píxeles no cambian, pero el fichero sí:
    ArcMap guarda las estadísticas con el ráster al liberarlo (metadatos dentro del TIFF
    y el histograma en `.aux.xml`). Se calculan una vez (~50 s en un TIFF de 2 GB) y
    después es instantáneo; en una carpeta sin escritura se recalculan cada vez.

    La cabecera de la leyenda queda siempre en `Value`: ArcObjects la fija en el
    `Update()` y después es de solo lectura. Se quita en el elemento de leyenda
    del layout, no desde aquí.
    """
    params: dict = {"capa": capa, "modo": modo, "num_clases": num_clases}
    if valores is not None:
        params["valores"] = valores
    if transparentes is not None:
        params["transparentes"] = transparentes
    if transparencia is not None:
        params["transparencia"] = transparencia
    if color_desde is not None:
        params["color_desde"] = color_desde
    if color_hasta is not None:
        params["color_hasta"] = color_hasta
    if colores is not None:
        params["colores"] = colores
    if etiquetas is not None:
        params["etiquetas"] = etiquetas
    if algoritmo is not None:
        params["algoritmo"] = algoritmo
    return _client.send("set_raster_symbology", params)


@mcp.tool()
def set_unique_values_symbology(capa: str, campo: str, tamano: float = None,
                                color_desde: list | str = None,
                                color_hasta: list | str = None,
                                algoritmo: str = None) -> dict:
    """
    Simboliza una capa de entidades por VALORES ÚNICOS del campo (categórica).

    Complemento de `set_graduated_symbology`: aquella parte un campo numérico en
    rangos, esta da un color por cada valor distinto. Hasta ahora la única vía
    para categorías era preparar un `.lyr` plantilla y aplicarlo con
    `apply_symbology_from_layer`.

    Por defecto reparte tonos por el círculo cromático, que es lo correcto en
    categórico: lo que importa es DISTINGUIR, no ordenar. Es reproducible, así
    que la misma capa con el mismo campo sale siempre igual (importa al
    reexportar una serie de planos). Si pasas `color_desde` y `color_hasta` se
    usa esa rampa en su lugar ([r,g,b] o "#RRGGBB"), interpolada con `algoritmo`
    (`cielab` por defecto | `lablch` | `hsv`, como en `set_graduated_symbology`).

    Las categorías son las que la capa DIBUJA: se respeta su definition query, así
    que una capa regional filtrada a un municipio da la leyenda del municipio. La
    selección NO se tiene en cuenta, a propósito: una leyenda que solo cubriera lo
    seleccionado dejaría el resto de la capa sin pintar. Admite campos de una tabla
    unida (join), y ordena numéricamente cuando el campo es numérico.

    Tope de **100 categorías**: por encima, falla diciendo que las supera (no cuántas
    hay: contarlas obligaría a recorrer la tabla entera, que es el cuelgue que el
    tope evita). Una leyenda de miles de entradas no es una leyenda. Los NULL se
    omiten.
    """
    params: dict = {"capa": capa, "campo": campo}
    if tamano is not None:
        params["tamano"] = tamano
    if color_desde is not None:
        params["color_desde"] = color_desde
    if color_hasta is not None:
        params["color_hasta"] = color_hasta
    if algoritmo is not None:
        params["algoritmo"] = algoritmo
    return _client.send("set_unique_values_symbology", params)


def _params_simbolo(capa, **valores):
    """Solo viaja lo que se pasa: un null que llegara al add-in sería indistinguible
    de «quítalo»."""
    params: dict = {"capa": capa}
    params.update({k: v for k, v in valores.items() if v is not None})
    return params


@mcp.tool()
def set_single_symbology(capa: str, color_relleno: list | str = None,
                         color_borde: list | str = None, grosor_borde: float = None,
                         sin_relleno: bool = None, tamano: float = None,
                         transparencia: float = None, etiqueta: str = None) -> dict:
    """
    Pone a una capa de ENTIDADES **un solo símbolo** (sin clasificar), con los colores
    que pidas. Sustituye la simbología que tenga.

    Es lo que no se podía hacer: forzar la graduada o los valores únicos a un solo color
    llena la tabla de contenidos con una entrada por entidad, y recargar la capa la trae
    con un color aleatorio. Sin colores, sale gris neutro, siempre el mismo.

    - Polígonos: `color_relleno`, `color_borde`, `grosor_borde` (pt); `sin_relleno=True`
      deja solo el contorno.
    - Líneas: el color de la línea es `color_borde` y su ancho `grosor_borde`. Pasar
      `color_relleno` a una línea es error.
    - Puntos: `color_relleno`, `tamano` (pt) y, opcional, `color_borde`/`grosor_borde`.
    - `transparencia` 0-100, de la capa. `etiqueta`: el texto de la entrada en la TOC.
    - Colores: `[R, G, B]` o `"#RRGGBB"`; mal formado es error.

    Para cambiar un color de una simbología que ya está montada (una categoría de unos
    valores únicos, por ejemplo) sin rehacerla, usa `edit_symbol`.
    """
    return _client.send("set_single_symbology", _params_simbolo(
        capa, color_relleno=color_relleno, color_borde=color_borde, grosor_borde=grosor_borde,
        sin_relleno=sin_relleno, tamano=tamano, transparencia=transparencia, etiqueta=etiqueta))


@mcp.tool()
def edit_symbol(capa: str, categoria: str | int = None, color_relleno: list | str = None,
                color_borde: list | str = None, grosor_borde: float = None,
                sin_relleno: bool = None, tamano: float = None,
                transparencia: float = None) -> dict:
    """
    Cambia SOLO lo que pidas de la simbología que la capa YA tiene, sin rehacerla:
    símbolo único, valores únicos o rangos. El resto del símbolo (tipo, patrón, lo que no
    se pida) y las demás categorías se quedan como estaban.

    - `categoria`: en valores únicos, el valor o su etiqueta; en rangos, el número de la
      clase (1..n) o su etiqueta. Sin ella se aplica a TODAS las categorías, pero solo lo
      que no borra la clasificación (borde, grosor, tamaño, transparencia): cambiar el
      relleno de todas a la vez es error.
    - Los demás parámetros, como en `set_single_symbology`.

    Sin nada que cambiar, devuelve la simbología actual: úsalo así para ver las
    categorías y sus colores antes de tocar. La respuesta trae cada clase leída de vuelta,
    `categorias_cambiadas` y el estado `antes`.
    """
    return _client.send("edit_symbol", _params_simbolo(
        capa, categoria=None if categoria is None else str(categoria), color_relleno=color_relleno, color_borde=color_borde,
        grosor_borde=grosor_borde, sin_relleno=sin_relleno, tamano=tamano,
        transparencia=transparencia))


@mcp.tool()
def list_style_symbols(estilo: str = None, clase: str = None, patron: str = None,
                       limite: int = 200) -> dict:
    """
    Símbolos de un estilo `.style` (los del selector de símbolos de ArcMap).

    - Sin `estilo`: lista los estilos CARGADOS en ArcMap (nombre y ruta) y las clases
      de símbolo de la galería. Es el primer paso.
    - `estilo`: el nombre de uno cargado (`Usuario`, `ESRI`) o la ruta ABSOLUTA a un
      `.style`. Si no estaba cargado se añade solo durante la llamada y se quita
      después: la galería del usuario no se queda cambiada.
    - `clase`: `relleno`, `linea` o `marcador` (o el nombre de una clase de la
      galería, p. ej. `Fill Symbols`). Sin ella, las tres.
    - `patron`: filtra por nombre o categoría. Sin comodines es «contiene»; con `*` o
      `?`, comodín sobre el texto entero. Sin distinguir mayúsculas.
    - `limite` (1-2000): cuántos se devuelven; `total` y `truncado` dicen si hay más.

    Cada símbolo trae `nombre`, `categoria`, `id`, `clase` y `tipo_simbolo`. El
    `nombre` es lo que pide `apply_style_symbol`; si se repite, el `id` lo desempata
    (el nombre no es único ni dentro de su categoría: `ESRI.style` trae dos «Verde»
    en «Predeterminado»).
    """
    params: dict = {"limite": limite}
    params.update({k: v for k, v in {"estilo": estilo, "clase": clase,
                                     "patron": patron}.items() if v is not None})
    return _client.send("list_style_symbols", params)


@mcp.tool()
def apply_style_symbol(capa: str, estilo: str, nombre_simbolo: str = None, clase: str = None,
                       categoria_estilo: str = None, etiqueta: str = None,
                       id_simbolo: int = None) -> dict:
    """
    Pone a una capa de ENTIDADES, como símbolo único, un símbolo de un estilo `.style`
    (por ejemplo, el relleno de «Monte público» del estilo de la empresa). Sustituye la
    simbología que tenga, como `set_single_symbology`; la transparencia de la capa no
    cambia.

    - `estilo`: nombre de un estilo cargado o ruta ABSOLUTA a un `.style` (se carga
      solo durante la llamada). `list_style_symbols` da los nombres.
    - `nombre_simbolo`: el nombre exacto (sin distinguir mayúsculas). Si hay varios con
      ese nombre, la llamada falla listando su `id` y su categoría: se desempata con
      `id_simbolo` (o `categoria_estilo`). Con `id_simbolo` solo, no hace falta el nombre.
    - `clase`: por defecto la que toca a la geometría de la capa (relleno para
      polígonos, línea, marcador para puntos). Un símbolo de otra geometría es error.
    - `etiqueta`: el texto de la entrada en la TOC.

    Para retocar después un color del símbolo aplicado, `edit_symbol`. La respuesta
    trae el símbolo leído de vuelta de la capa y `simbolo_de_estilo` (de dónde salió).
    """
    return _client.send("apply_style_symbol", _params_simbolo(
        capa, estilo=estilo, nombre_simbolo=nombre_simbolo, clase=clase,
        categoria_estilo=categoria_estilo, etiqueta=etiqueta, id_simbolo=id_simbolo))


@mcp.tool()
def get_bookmarks() -> dict:
    """Lista los marcadores espaciales del data frame activo, con su extensión."""
    return _client.send("get_bookmarks")


@mcp.tool()
def add_bookmark(nombre: str) -> dict:
    """
    Guarda la extensión ACTUAL de la vista como marcador espacial.

    Si ya existe uno con ese nombre lo reemplaza, en vez de dejar dos entradas
    indistinguibles en el menú de marcadores.
    """
    return _client.send("add_bookmark", {"nombre": nombre})


@mcp.tool()
def remove_bookmark(nombre: str) -> dict:
    """Borra un marcador espacial por nombre."""
    return _client.send("remove_bookmark", {"nombre": nombre})


@mcp.tool()
def goto_bookmark(nombre: str) -> dict:
    """Encuadra la vista en un marcador espacial guardado."""
    return _client.send("goto_bookmark", {"nombre": nombre})


@mcp.tool()
def set_scale(escala: float) -> dict:
    """
    Fija la escala del data frame activo y refresca (ej. 200000 -> 1:200.000).
    """
    return _client.send("set_scale", {"escala": escala})


# --------------------------------------------------------------------------- #
# Geoprocesamiento y mantenimiento.
# --------------------------------------------------------------------------- #

@mcp.tool()
def save_mxd(verificar: bool = None) -> dict:
    """
    Guarda el documento .mxd abierto en su ruta actual. Devuelve la ruta guardada.
    Útil para persistir los cambios que han hecho otras tools (def. query, textos,
    simbología). Para guardar en otra ruta sin tocar el original usa save_mxd_as.

    **Rutas relativas que se pierden al guardar.** Con «Store relative pathnames»,
    ArcMap 10.5 no escribe la ruta de una capa si la carpeta del .mxd más la ruta
    relativa de su workspace llega a 260 caracteres: al reabrir queda
    `\\fichero.shp` y la capa rota, aunque en memoria se vea bien. Antes de guardar,
    la respuesta predice esas capas en `capas_perderan_ruta` y `aviso_rutas_relativas`.

    `verificar`: tras guardar, **reabre el .mxd** con arcpy en un proceso aparte (sin
    tocar la sesión) y lista en `verificacion.rotas_nuevas` las capas rotas al
    reabrir que no lo estaban en memoria. Es la única comprobación que no miente.
    Sin indicarlo, se verifica **solo si la predicción ha encontrado algo**;
    `verificar=False` lo impide. Cuesta unos 10 s.

    Escribir un documento con decenas de capas y ráster pesado no cabe en el timeout
    corto: la espera la gobierna ARCMAP_SAVE_TIMEOUT.
    """
    r = _client.send("save_mxd", {"verificar": True} if verificar else {}, timeout=SAVE_TIMEOUT)
    return _tras_guardar(r, "ruta", verificar)


def _tras_guardar(r, clave_ruta, verificar):
    """Reabre lo guardado si se pidió, o si la predicción del add-in encontró capas que
    se van a perder. `rotas_en_memoria` solo sirve para esa comparación y no se devuelve."""
    if not r.get("ok"):
        return r
    res = r.get("result") or {}
    memoria = res.pop("rotas_en_memoria", None) or []
    if verificar or (verificar is None and res.get("capas_perderan_ruta")):
        res["verificacion"] = _verificar_guardado(res.get(clave_ruta), memoria)
    return r


def _verificar_guardado(ruta, rotas_en_memoria, timeout=180):
    """Reabre `ruta` con el auditor standalone y devuelve las capas rotas en disco que no
    lo estaban en memoria. Las rutas de grupo se comparan con `/` (el auditor da
    `Grupo\\Capa`, el add-in `Grupo/Capa`)."""
    import subprocess

    py27 = _python27_arcgis()
    auditor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auditor_mxd.py")
    if not ruta or py27 is None or not os.path.isfile(auditor):
        return {"reabierto": False, "motivo": "falta el Python 2.7 de ArcGIS (ARCMAP_PYTHON27) o el auditor"}
    try:
        proc = subprocess.run([py27, auditor, ruta], capture_output=True, timeout=timeout)
        bruto = proc.stdout.decode("utf-8", "replace").strip()
        datos = json.loads(bruto) if bruto else {"ok": False, "error": "sin salida"}
    except subprocess.TimeoutExpired:
        return {"reabierto": False, "motivo": "el auditor no abrió el .mxd en %s s" % timeout}
    except (json.JSONDecodeError, OSError) as exc:
        return {"reabierto": False, "motivo": "no se pudo auditar: %s" % exc}
    if not datos.get("ok"):
        return {"reabierto": False, "motivo": datos.get("error") or "desconocido"}

    def clave(df, ruta_capa):
        # El add-in numera los nombres repetidos ("Capa#2") para desambiguarlos; arcpy no.
        ruta_capa = re.sub(r"#[0-9]+(?=/|$)", "", (ruta_capa or "").replace("\\", "/"))
        return ((df or "").lower(), ruta_capa.lower())

    antes = {clave(c.get("data_frame"), c.get("ruta")) for c in rotas_en_memoria}
    nuevas = []
    for c in datos.get("capas") or []:
        if c.get("grupo") or not c.get("rota"):
            continue
        if clave(c.get("data_frame"), c.get("nombre_largo")) not in antes:
            nuevas.append({"data_frame": c.get("data_frame"), "ruta": c.get("nombre_largo"),
                           "fuente_al_reabrir": c.get("fuente")})
    return {"reabierto": True, "rotas_al_reabrir": datos.get("num_rotas"),
            "rotas_en_memoria": len(rotas_en_memoria), "rotas_nuevas": nuevas,
            "guardado_sin_perdidas": not nuevas}


@mcp.tool()
def save_mxd_as(salida: str, sobrescribir: bool = False, verificar: bool = None) -> dict:
    """
    Guarda una COPIA del .mxd en `salida` (no cambia el documento activo ni su ruta).
    `salida` es la ruta de destino (.mxd), ABSOLUTA y en una carpeta que exista.
    Equivale a `mxd.saveACopy(...)`.

    `sobrescribir` es False por defecto, al revés que en los export: un PDF pisado
    se regenera, un .mxd pisado es trabajo de alguien. Si el destino existe, la
    llamada falla sin tocarlo y dice cómo forzarlo. Al REGENERAR una serie de .mxd
    (uno por capa, p. ej.) pasa `sobrescribir=True`. La respuesta trae `sobrescrito`.

    **Rutas relativas**: como en `save_mxd`, la respuesta predice las capas que se
    perderán (`capas_perderan_ruta`), calculadas contra la carpeta de DESTINO: una copia
    más profunda que el original puede romper capas que en él guardan bien. `verificar`
    reabre la copia y lista `verificacion.rotas_nuevas`; sin indicarlo, solo si la
    predicción ha encontrado algo.

    Como `save_mxd`, la espera la gobierna ARCMAP_SAVE_TIMEOUT.
    """
    params = {"salida": salida, "sobrescribir": sobrescribir}
    if verificar:
        params["verificar"] = True
    return _tras_guardar(_client.send("save_mxd_as", params, timeout=SAVE_TIMEOUT),
                         "salida", verificar)


@mcp.tool()
def list_broken_data_sources() -> dict:
    """
    Lista las capas/tablas con la fuente de datos ROTA (rutas que ArcMap no encuentra,
    muy común con unidades de red X:/Y:/G:). Recorre TODOS los data frames, no solo
    el activo. Para cada una devuelve nombre, `ruta` de grupo, `data_frame`, ruta rota
    y workspace si es accesible. Primer paso antes de `repair_data_source`.
    """
    return _client.send("list_broken_data_sources")


@mcp.tool()
def repair_data_source(capa: str, ruta_antigua: str, ruta_nueva: str,
                       validar: bool = True, data_frame: str = None) -> dict:
    """
    Reapunta la fuente de una capa sustituyendo su workspace (`ruta_antigua` ->
    `ruta_nueva`), p. ej. cuando una carpeta de red ha cambiado de letra/ubicación.
    Con `validar=True` el cambio solo se aplica si la ruta nueva es válida. Devuelve
    si la capa estaba/queda rota.

    Busca la capa en TODOS los data frames, igual que el listado (antes solo miraba
    el activo, y una capa rota de un data frame secundario se listaba pero no se
    podía reparar). `data_frame` acota la búsqueda a uno; si el nombre casa en más
    de un sitio y no lo indicas, la llamada falla listando los candidatos.

    **Reparada en memoria no es reparada en disco.** Si el documento guarda rutas
    relativas y la carpeta del .mxd más la ruta relativa del workspace nuevo llega a
    260 caracteres, ArcMap 10.5 perderá la ruta al guardar (queda `\\fichero.shp`). La
    respuesta lo avisa en `aviso_ruta_relativa` y `riesgo_ruta_relativa` (no bloquea).
    """
    params = {"capa": capa, "ruta_antigua": ruta_antigua,
              "ruta_nueva": ruta_nueva, "validar": validar}
    if data_frame is not None:
        params["data_frame"] = data_frame
    return _client.send("repair_data_source", params)


@mcp.tool()
def run_geoprocessing(tool: str, params: list = None,
                      resolver_capas: bool = True, fuera_de_arcmap: bool = False,
                      anadir_al_mapa: bool = True, sobrescribir: bool = False) -> dict:
    """
    Ejecuta un geoproceso por nombre NOMINAL sin escribir código (paridad con el MCP de
    ArcGIS Pro). `tool` admite forma punteada por toolbox (`management.CopyFeatures`,
    `analysis.Buffer`, `sa.Slope`...) o el alias clásico (`Buffer_analysis`). `params`
    es la lista de argumentos posicionales del geoproceso. Devuelve salidas y mensajes.

    Con `resolver_capas=True` (defecto), los strings de `params` que coincidan con el
    nombre de una capa de la TOC se sustituyen por el objeto Layer (honra su definition
    query y selección); los strings con pinta de ruta o de SQL nunca se sustituyen.
    Pasa `resolver_capas=False` si algún parámetro textual (un nombre de campo, una
    keyword) colisiona con el nombre de una capa.

    MULTIVALOR: un parámetro que admite varias entradas (Merge, Union, Intersect...)
    se pasa como LISTA dentro de `params`: `[["capaA", "capaB"], r"C:\\out.shp"]`. Se
    une con ';', que es lo que el geoproceso espera. OJO con la diferencia: una capa
    de la TOC DENTRO de una lista entra por la RUTA de su fuente, así que pierde su
    definition query y su selección (en un multivalor viaja una cadena, no el objeto
    Layer). Si necesitas respetarlas, expórtala antes o pásala como argumento suelto.

    Una tool que no existe o con parámetros de más da error ANTES de ejecutar (el
    geoprocesador ignoraba los de más y devolvía ok).

    Por defecto el geoproceso corre DENTRO de ArcMap y congela su interfaz mientras
    dura. `fuera_de_arcmap=True` lo lanza con el arcpy de ArcGIS en otro proceso y
    ArcMap queda libre; úsalo con geoprocesos largos sobre datos en disco. Diferencias:
    - las capas de la TOC se pasan por la RUTA de su fuente (`capas_por_ruta` en la
      respuesta); una capa con definition query o selección da error en vez de
      procesarse entera sin avisar;
    - `anadir_al_mapa` (defecto True) añade al mapa las salidas que son capas;
    - `sobrescribir=False` (defecto): una salida que ya existe falla antes de calcular.
      Con True tampoco se puede sobrescribir un dato que esté o haya estado cargado
      en esta sesión: ArcMap lo mantiene bloqueado aunque se quite del mapa;
    - arrancar el arcpy de fuera cuesta ~10 s fijos: para geoprocesos cortos, mejor
      dentro.
    Dentro de ArcMap, `anadir_al_mapa` y `sobrescribir` no se usan (manda la
    configuración de geoprocesamiento de ArcMap).
    """
    if fuera_de_arcmap:
        return _client.send("run_geoprocessing_fuera",
                            {"tool": tool, "params": params or [],
                             "resolver_capas": resolver_capas,
                             "anadir_al_mapa": anadir_al_mapa,
                             "sobrescribir": sobrescribir},
                            timeout=FONDO_TIMEOUT)
    return _client.send("run_geoprocessing",
                        {"tool": tool, "params": params or [],
                         "resolver_capas": resolver_capas},
                        timeout=GP_TIMEOUT)


# --------------------------------------------------------------------------- #
# Visualización y datos (inspección + multi-data-frame + encuadre).
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_layer_features(capa: str, where: str = None, campos: list = None,
                       limite: int = 50) -> dict:
    """
    Devuelve FILAS de atributos de una capa (respeta su definition query y selección).
    `campos` = lista de campos a traer (None = todos salvo geometría). `where` opcional.
    `limite` = máximo de filas (50 por defecto, entre 1 y 5000; fuera de rango es error)
    para no inflar la respuesta. Complementa a get_unique_values / count_features cuando
    necesitas ver registros concretos. Admite campos de una tabla unida (join).
    """
    return _client.send("get_layer_features", {"capa": capa, "where": where,
                                                "campos": campos, "limite": limite})


@mcp.tool()
def describe_data(ruta: str) -> dict:
    """
    Describe un dataset EN DISCO (no necesita estar en el mapa): tipo, CRS, geometría,
    extent y campos. Útil antes de `add_layer` o como `arcpy.Describe` ergonómico.
    """
    return _client.send("describe_data", {"ruta": ruta})


@mcp.tool()
def list_data_frames() -> dict:
    """
    Lista los data frames del .mxd (nombre, escala y cuál es el activo). Los mxds de
    series de planos suelen tener varios (mapa principal + locator).
    """
    return _client.send("list_data_frames")


@mcp.tool()
def set_active_df(nombre: str) -> dict:
    """
    Fija el data frame activo por nombre y refresca. Las tools que operan sobre 'el df
    activo' (set_scale, set_extent, export_view_png...) pasarán a actuar sobre éste.
    """
    return _client.send("set_active_df", {"nombre": nombre})


@mcp.tool()
def set_extent(coords: list = None, capa: str = None) -> dict:
    """
    Encuadra el data frame activo a unas coordenadas `[xmin, ymin, xmax, ymax]` (en el
    CRS del data frame) o al extent de una `capa` (el de su selección si la hay; si no,
    el total de la capa). Indica UNO de los dos.
    """
    return _client.send("set_extent", {"coords": coords, "capa": capa})


# --------------------------------------------------------------------------- #
# Catálogo y workspace (paridad con ArcGIS Pro MCP).
# --------------------------------------------------------------------------- #

@mcp.tool()
def get_workspace() -> dict:
    """Devuelve el workspace y scratch workspace actuales de `arcpy.env`."""
    return _client.send("get_workspace")


@mcp.tool()
def set_workspace(workspace: str) -> dict:
    """
    Fija `arcpy.env.workspace` (gdb o carpeta) — base para list_feature_classes /
    list_tables / list_rasters y destino por defecto de geoprocesos.
    """
    return _client.send("set_workspace", {"workspace": workspace})


@mcp.tool()
def list_feature_classes(workspace: str = None) -> dict:
    """
    Lista las feature classes de un `workspace` (incluye las dentro de datasets). Si no
    se indica, usa el workspace fijado con set_workspace.
    """
    return _client.send("list_feature_classes", {"workspace": workspace})


@mcp.tool()
def list_tables(workspace: str = None) -> dict:
    """Lista las tablas independientes de un `workspace` (o el fijado por set_workspace)."""
    return _client.send("list_tables", {"workspace": workspace})


@mcp.tool()
def list_rasters(workspace: str = None) -> dict:
    """Lista los datasets ráster de un `workspace` (o el fijado por set_workspace)."""
    return _client.send("list_rasters", {"workspace": workspace})


# --------------------------------------------------------------------------- #
# Análisis ambiental y teledetección (geoprocesos pesados).
#
# Requieren las extensiones Spatial Analyst o 3D Analyst y operan sobre datos EN
# DISCO. Son LENTOS: los corre el runner en un proceso aparte y la espera de aquí la
# gobierna ARCMAP_FONDO_TIMEOUT (ver los presupuestos de arriba). Por defecto añaden
# el resultado al data frame activo (anadir_al_mapa=True), y ese último paso sí ocupa
# el hilo de ArcMap unos segundos.
#
# Las cinco llevan `sobrescribir` (False por defecto). Con él en False, si la salida
# ya existe la operación falla ANTES de calcular nada, en vez de gastar el geoproceso
# entero para morir al guardar (arcpy.env.overwriteOutput llega a False en un proceso
# nuevo). Ponerlo en True activa el overwrite para esa llamada — y solo para esa.
# --------------------------------------------------------------------------- #

@mcp.tool()
def raster_index(indice: str, bandas: dict, salida: str,
                 L: float = 0.5, anadir_al_mapa: bool = True,
                 banda_a: str = None, banda_b: str = None,
                 sobrescribir: bool = False) -> dict:
    """
    Calcula un ÍNDICE ESPECTRAL de teledetección desde bandas ráster (Spatial Analyst).

    `indice` (uno de): NDVI, GNDVI, NDRE, NDWI, MNDWI, NDMI, NBR, SAVI, EVI.
    `bandas` = dict {ROL: ruta_raster} con los roles que pida el índice. Roles válidos:
    BLUE, GREEN, RED, REDEDGE, NIR, SWIR1, SWIR2.

    Correspondencia ROL -> banda física por sensor:
      ROL      Sentinel-2   Landsat 8-9 (OLI)   Landsat 4-7 (TM/ETM+)
      BLUE     B2           B2                  B1
      GREEN    B3           B3                  B2
      RED      B4           B4                  B3
      REDEDGE  B5           (no tiene)          (no tiene)
      NIR      B8 / B8A     B5                  B4
      SWIR1    B11          B6                  B5
      SWIR2    B12          B7                  B7

    Fórmulas y bandas por índice:
      NDVI  (NIR-RED)/(NIR+RED)              — verdor de la vegetación
      GNDVI (NIR-GREEN)/(NIR+GREEN)          — clorofila
      NDRE  (NIR-REDEDGE)/(NIR+REDEDGE)      — red-edge (solo S2), vigor/estrés
      NDWI  (GREEN-NIR)/(GREEN+NIR)          — agua superficial (McFeeters)
      MNDWI (GREEN-SWIR1)/(GREEN+SWIR1)      — agua mejorado (Xu)
      NDMI  (NIR-SWIR1)/(NIR+SWIR1)          — humedad de la vegetación
      NBR   (NIR-SWIR2)/(NIR+SWIR2)          — área quemada / severidad
      SAVI  ((NIR-RED)/(NIR+RED+L))*(1+L)    — vegetación corregido por suelo (L=0.5)
      EVI   2.5*((NIR-RED)/(NIR+6*RED-7.5*BLUE+1)) — vegetación, alta biomasa

    `L` ajusta SAVI. Para un índice arbitrario: indice="CUSTOM" + banda_a/banda_b
    -> (banda_a - banda_b)/(banda_a + banda_b). `salida` = ruta del ráster resultante.

    `sobrescribir=False` (defecto): si `salida` ya existe, falla antes de calcular.
    """
    return _client.send("raster_index", {
        "indice": indice, "bandas": bandas, "salida": salida, "L": L,
        "anadir_al_mapa": anadir_al_mapa, "banda_a": banda_a, "banda_b": banda_b,
        "sobrescribir": sobrescribir,
    }, timeout=FONDO_TIMEOUT)


@mcp.tool()
def hydrology(operacion: str, parametros: dict, anadir_al_mapa: bool = True,
              sobrescribir: bool = False) -> dict:
    """
    Análisis hidrológico sobre un MDT (Spatial Analyst). `operacion` + `parametros`:

      - "cuenca": Fill > FlowDirection > FlowAccumulation y delimita cuencas.
            {mdt, salida_dir, salida, pour_points?, snap_dist?}
            Con `pour_points` (puntos de desagüe) usa Watershed (snap opcional con
            `snap_dist`); sin ellos, Basin (todas las cuencas del MDT).
            Deja también fdir y facc en `salida_dir`.
      - "red_drenaje": red de drenaje por umbral de acumulación de flujo.
            {mdt | (fdir + facc), umbral, salida}   (salida = líneas)
      - "inundacion": cota de inundación simple (celdas con MDT <= nivel).
            {mdt, nivel, salida}

    Rutas en `parametros` apuntan a datos en disco. Geoproceso pesado.

    `sobrescribir=False` (defecto): si `salida` ya existe, falla antes de calcular.
    En "cuenca" cuentan también los `fdir` y `facc` que deja en `salida_dir`, que
    llevan nombre fijo y por eso chocan al repetir la operación en la misma carpeta.
    """
    return _client.send("hydrology", {
        "operacion": operacion, "parametros": parametros,
        "anadir_al_mapa": anadir_al_mapa, "sobrescribir": sobrescribir,
    }, timeout=FONDO_TIMEOUT)


@mcp.tool()
def contours(mdt: str, salida: str, intervalo: float, base: float = 0,
             dxf: str = None, anadir_al_mapa: bool = True,
             sobrescribir: bool = False) -> dict:
    """
    Genera curvas de nivel desde un MDT (3D Analyst). `intervalo` = equidistancia
    (en unidades Z del MDT), `base` = cota base (0 por defecto). Si pasas `dxf`
    (ruta), exporta además las curvas a DXF (entrega CAD). `salida` = feature class
    de líneas. Geoproceso pesado.

    `sobrescribir=False` (defecto): si `salida` (o el `dxf`) ya existe, falla antes
    de calcular.
    """
    return _client.send("contours", {
        "mdt": mdt, "salida": salida, "intervalo": intervalo, "base": base,
        "dxf": dxf, "anadir_al_mapa": anadir_al_mapa, "sobrescribir": sobrescribir,
    }, timeout=FONDO_TIMEOUT)


@mcp.tool()
def topographic_profile(superficie: str, lineas: str, salida: str,
                        anadir_al_mapa: bool = True,
                        sobrescribir: bool = False) -> dict:
    """
    Perfil topográfico: interpola una capa de LÍNEAS 2D sobre una `superficie`
    (MDT ráster o TIN) y devuelve líneas 3D con la Z del terreno (3D Analyst,
    InterpolateShape). Útil para perfiles longitudinales de caminos, cauces o
    transectos. `salida` = feature class de líneas 3D. Geoproceso pesado.

    `sobrescribir=False` (defecto): si `salida` ya existe, falla antes de calcular.
    """
    return _client.send("topographic_profile", {
        "superficie": superficie, "lineas": lineas, "salida": salida,
        "anadir_al_mapa": anadir_al_mapa, "sobrescribir": sobrescribir,
    }, timeout=FONDO_TIMEOUT)


@mcp.tool()
def least_cost_path(coste: str, origen: str, destino: str, salida: str,
                    salida_dir: str = None, anadir_al_mapa: bool = True,
                    sobrescribir: bool = False) -> dict:
    """
    Ruta de mínimo coste (Spatial Analyst): calcula CostDistance desde `origen`
    sobre el ráster de fricción `coste` y traza CostPath hasta `destino`.
    `origen`/`destino` = features (puntos/polígonos) o ráster; `coste` = ráster de
    fricción (mayor valor = más difícil de atravesar). `salida` = ráster con la ruta
    óptima. Útil para trazado de pistas, cortafuegos o accesos. Geoproceso pesado.

    `sobrescribir=False` (defecto): si `salida` ya existe, falla antes de calcular.
    El ráster de backlink que deja como subproducto lleva nombre único por llamada,
    así que dos rutas seguidas en la misma carpeta ya no chocan entre sí.
    """
    return _client.send("least_cost_path", {
        "coste": coste, "origen": origen, "destino": destino, "salida": salida,
        "salida_dir": salida_dir, "anadir_al_mapa": anadir_al_mapa,
        "sobrescribir": sobrescribir,
    }, timeout=FONDO_TIMEOUT)


@mcp.tool()
def calculate_geometry(entrada: str, propiedades, unidad_longitud: str = "",
                       unidad_area: str = "", crs: str = "") -> dict:
    """
    Calcula atributos de geometría sobre una capa/feature class IN PLACE
    (AddGeometryAttributes, ArcMap 10.2+). NO requiere extensión.

    `entrada` = nombre de capa de la TOC (se resuelve, honra def. query/selección) o
    ruta a feature class en disco. `propiedades` = lista o cadena separada por ';'
    con una o varias de: AREA, AREA_GEODESIC, PERIMETER_LENGTH, LENGTH,
    LENGTH_GEODESIC, CENTROID, CENTROID_INSIDE, POINT_X_Y_Z_M, EXTENT, LINE_BEARING,
    LINE_START_MID_END. `unidad_longitud` (ej. METERS, KILOMETERS) y `unidad_area`
    (ej. SQUARE_METERS, HECTARES) opcionales; `crs` opcional para medidas en otro
    sistema. Añade las columnas calculadas a la tabla de atributos.

    Recorre la tabla entera fila a fila, así que sobre una capa de decenas de miles
    de registros es un geoproceso largo: la espera la gobierna ARCMAP_GP_TIMEOUT.
    """
    return _client.send("calculate_geometry", {
        "entrada": entrada, "propiedades": propiedades,
        "unidad_longitud": unidad_longitud, "unidad_area": unidad_area, "crs": crs,
    }, timeout=GP_TIMEOUT)


def _clave_version(v):
    """'10.8' -> (10, 8) para ordenar como números: alfabéticamente, '10.10'
    quedaría por detrás de '10.8'. Lo que no se entienda va al final."""
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return ()


def _arcmap_por_registro():
    """Versiones de ArcGIS Desktop según el registro: [(version, install_dir), ...].

    Es la fuente buena, la misma que usa install.ps1: la escribe el instalador de
    Esri y vale para CUALQUIER ruta de instalación. Hasta la 2.13.0 solo se miraban
    las carpetas por defecto de Program Files, y una instalación en otra ruta
    (issue #1: `D:\\软件安装\\Desktop10.8\\`) dejaba `describe_mxd` sin su veredicto
    más útil, en silencio.
    """
    try:
        import winreg
    except ImportError:          # no es Windows
        return []
    encontradas = []
    for ruta in (r"SOFTWARE\WOW6432Node\ESRI", r"SOFTWARE\ESRI"):
        try:
            esri = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, ruta)
        except OSError:
            continue
        with esri:
            i = 0
            while True:
                try:
                    nombre = winreg.EnumKey(esri, i)
                except OSError:
                    break
                i += 1
                if not nombre.lower().startswith("desktop"):
                    continue
                version = nombre[len("Desktop"):]
                if not _clave_version(version):
                    continue
                install_dir = None
                try:
                    with winreg.OpenKey(esri, nombre) as k:
                        install_dir = winreg.QueryValueEx(k, "InstallDir")[0]
                except OSError:
                    pass
                encontradas.append((version, install_dir))
    return encontradas


def _detectar_arcmap_local():
    """(version, fuente) de ArcMap instalado EN ESTA MÁQUINA. (None, motivo) si no.

    Se mira la máquina y no el puente porque `ping` no devuelve la versión de
    ArcGIS. Ojo: si el servidor corre en una máquina distinta de ArcMap (acceso
    remoto), esto NO es la versión de la sesión; por eso quien lo use lo dice.

    1. Registro. Si hay varias, se prefieren las que tienen `InstallDir` existente
       (una desinstalación puede dejar la clave huérfana) y, entre ellas, la mayor.
    2. Respaldo: las carpetas por defecto de Program Files.
    """
    registro = _arcmap_por_registro()
    if registro:
        vivas = [v for v, d in registro if d and os.path.isdir(d)]
        candidatas = vivas or [v for v, _ in registro]
        mejor = max(candidatas, key=_clave_version)
        return mejor, ("registro" if vivas else "registro (sin InstallDir existente)")
    for base in (r"C:\Program Files (x86)\ArcGIS", r"C:\Program Files\ArcGIS"):
        try:
            nombres = os.listdir(base)
        except OSError:
            continue
        versiones = [n[len("Desktop"):] for n in nombres
                     if n.lower().startswith("desktop") and _clave_version(n[len("Desktop"):])]
        if versiones:
            return max(versiones, key=_clave_version), "carpeta %s" % base
    return None, ("no se encontró ArcGIS Desktop en el registro "
                  "(HKLM\\SOFTWARE\\[WOW6432Node\\]ESRI\\Desktop*) ni en las carpetas por "
                  "defecto de Program Files")


def _version_arcmap_local():
    """Solo la versión (compatibilidad con quien ya la usaba)."""
    return _detectar_arcmap_local()[0]


@mcp.tool()
def describe_mxd(ruta: str) -> dict:
    """
    Inspecciona un .mxd SIN abrirlo: versión declarada, tamaño y si es válido.

    Cuesta milisegundos y NO necesita ArcMap, ni el puente, ni licencia, porque
    lee la estructura del fichero en vez de pedírselo a arcpy. Funciona sobre
    documentos que ArcMap se niega a abrir, que es justo cuando hace falta.

    PARA QUÉ. `arcpy.mapping.MapDocument()` falla con un mensaje genérico ("no
    puede abrir documento de mapa", "Nombre de archivo MXD no válido") que sirve
    igual para una ruta mala, un fichero corrupto o un documento guardado con una
    versión superior a la de la sesión. Esta herramienta separa esos casos: si la
    versión declarada es mayor que la de ArcMap, ahí está la causa; si coincide,
    la versión queda DESCARTADA y hay que mirar otra cosa (fuentes de datos
    inaccesibles, permisos, ruta). Descartar vale tanto como acusar.

    Devuelve `version_declarada`, `version_arcmap_local`, `veredicto` y `motivo`.

    LÍMITE, dicho claro: `version_declarada` es lo que el documento dice de sí
    mismo en su stream `Version`, no necesariamente la versión de la aplicación
    que lo grabó. Se ha visto un lote entero de planos declarando `10.5` cuando
    se creían de 10.8, así que un `compatible` NO garantiza que abra: garantiza
    que el documento no se declara más nuevo que tu ArcMap.
    """
    ruta_abs = os.path.abspath(os.path.expandvars(ruta))
    if not os.path.isfile(ruta_abs):
        return {"ok": False, "ruta": ruta_abs,
                "error": "no existe o no es un fichero: %s" % ruta_abs}

    version, error = _mxd_version_declarada(ruta_abs)
    local, fuente_local = _detectar_arcmap_local()
    v_doc, v_app = _a_tupla(version), _a_tupla(local)

    if version is None:
        veredicto, motivo = "ilegible", error
    elif v_doc and v_app and v_doc > v_app:
        veredicto = "mas_nuevo_que_arcmap"
        motivo = ("el documento se declara %s y ArcMap instalado aquí es %s: "
                  "ArcMap no abre documentos de versión superior. ESTA es la causa "
                  "del fallo genérico al abrirlo." % (version, local))
    elif v_doc and v_app:
        veredicto = "compatible"
        motivo = ("el documento se declara %s y ArcMap instalado aquí es %s, así que "
                  "la versión NO explica un fallo al abrirlo. Mira las fuentes de "
                  "datos (rutas de red que no responden hacen que abrir el documento "
                  "tarde muchísimo o se cuelgue), los permisos y la ruta."
                  % (version, local))
    elif local is None:
        # Que no se pudo detectar tiene que DECIRSE: un `null` a secas se leía igual que
        # "se comparó y no salió nada" (issue #1).
        veredicto = "indeterminado"
        motivo = ("el documento se declara %s, pero NO se ha podido detectar la versión "
                  "de ArcMap instalada en esta máquina: %s. Sin ella no hay comparación. "
                  "Compárala tú con la de tu ArcMap (Ayuda > Acerca de ArcMap)."
                  % (version, fuente_local))
    else:
        veredicto = "indeterminado"
        motivo = ("versión del documento: %s; ArcMap local: %s. No se pudieron comparar "
                  "(formato de versión no reconocido)." % (version, local))

    return {
        "ok": True,
        "ruta": ruta_abs,
        "tamano_bytes": os.path.getsize(ruta_abs),
        "version_declarada": version,
        "version_arcmap_local": local,
        "version_arcmap_local_fuente": fuente_local,
        "veredicto": veredicto,
        "motivo": motivo,
        "aviso_version_local": ("'version_arcmap_local' se deduce de la instalación de "
                                "ESTA máquina, no de la sesión conectada al puente."),
    }


# Lo que `audit_folder` copia del auditor a cada documento, y lo que suma en el
# resumen. Los `_en_plano` van primero a propósito: son los que dicen si el plano
# sale mal; los totales incluyen capas en grupos apagados y marcos fuera de la hoja.
_CLAVES_AUDITOR = ("num_rotas_en_plano", "num_con_query_en_plano", "num_capas",
                   "num_rotas", "num_con_query", "num_en_plano_desconocido")
_SUMAS_AUDITOR = ("num_rotas_en_plano", "num_rotas", "num_con_query_en_plano",
                  "num_con_query", "num_en_plano_desconocido")


# Timeouts SEGUIDOS tras los que `audit_folder` deja de abrir documentos. Si algo
# bloquea al arcpy standalone para todos (lo que se vio el 2026-08-27, de causa sin
# identificar), sin este corte una carpeta de 200 planos costaría 200 timeouts.
_CORTE_TIMEOUTS_SEGUIDOS = 2


def _python27_arcgis():
    """Ruta del Python 2.7 de ArcGIS, o None. Se puede forzar con ARCMAP_PYTHON27."""
    forzado = os.environ.get("ARCMAP_PYTHON27")
    if forzado and os.path.isfile(forzado):
        return forzado
    for base in (r"C:\Python27", r"C:\Python27_x64"):
        try:
            nombres = os.listdir(base)
        except OSError:
            continue
        for nombre in sorted(nombres, reverse=True):
            cand = os.path.join(base, nombre, "python.exe")
            if os.path.isfile(cand):
                return cand
    return None


@mcp.tool()
def audit_folder(ruta: str, con_capas: bool = True, timeout_por_documento: int = 90,
                 max_documentos: int = 200, recursivo: bool = False,
                 forzar_con_arcmap_abierto: bool = False) -> dict:
    """
    Audita TODOS los .mxd de una carpeta: versión, capas, fuentes y definition queries.

    El caso de uso real no es inspeccionar un documento sino **auditar una serie
    de planos**, y hasta ahora eso obligaba a un script externo porque todas las
    herramientas operaban sobre el documento abierto. Esta NO necesita ArcMap
    abierto ni el puente: se apoya en el arcpy standalone.

    DOS PASADAS, y la separación importa:

    1. **Siempre**, y cuesta milisegundos por documento: lee del propio fichero la
       versión declarada y comprueba que es un .mxd válido, sin abrir nada
       (lo mismo que `describe_mxd`).
    2. **Solo si `con_capas`**: abre cada documento con arcpy **en un proceso
       aparte y con `timeout_por_documento`**. El aislamiento no es adorno: si un
       documento se atasca, muere su proceso y la auditoría continúa marcándolo
       como `timeout`.

    **Funciona con ArcMap abierto.** Hasta la 2.14.0 se negaba a la pasada 2 con
    ArcMap corriendo, por una medición del 2026-08-27 (mismo .mxd: 0,7 s con ArcMap
    cerrado, bloqueado a los 180 s con ArcMap abierto). No se ha vuelto a
    reproducir: el 2026-09-25 el auditor abrió el mismo documento en ~8,5 s con
    ArcMap cerrado, abierto con ese mismo .mxd y **congelado** (proceso suspendido),
    así que el estado de ArcMap no es la causa. La causa de agosto sigue sin
    identificar, y la protección ya no mira a ArcMap sino al síntoma: tras
    **dos timeouts seguidos** la pasada 2 se corta para el resto de documentos y la
    respuesta lo dice en `aviso_capas` (quedan con `sin_abrir: true`). Si pasa, lo
    primero es mirar si hay `python.exe` de ArcGIS huérfanos y si las unidades de red
    de las capas responden. `forzar_con_arcmap_abierto` se sigue aceptando por
    compatibilidad y ya no hace nada.

    Presupuesta el tiempo: abrir cuesta ~1-3 s por documento más ~6 s de
    `import arcpy` por proceso. Empieza con `con_capas=False` para tener el mapa de
    versiones al instante.

    **QUÉ SALE EN EL PLANO Y QUÉ NO.** Lee primero `num_rotas_en_plano` y
    `num_con_query_en_plano` (por documento y sumados en `resumen`), no los totales:
    `num_rotas` cuenta también capas dentro de grupos apagados y en marcos fuera de
    la hoja, que no se dibujan. En una serie real de planos había 2.335
    capas rotas y solo 87 salían en los planos. Cada capa trae:
    - `data_frame` y `nombre_largo` (la ruta de grupos, `Grupo\\Capa`);
    - `visible` (su casilla), `visible_efectivo` (ella y todos sus grupos) y
      `en_plano` (además, su marco cae al menos en parte en la hoja).
    Cada documento trae `marcos`, con posición, tamaño y `en_pagina`
    (`dentro` / `parcial` / `fuera`). Un `None` en cualquiera de esos campos es «no se
    pudo leer», no «apagada»; se cuentan en `num_en_plano_desconocido`. Lo que NO se
    mira: el rango de escalas de la capa, que arcpy.mapping 10.x no expone.

    `max_documentos` corta la lista (por defecto 200) y **lo dice** en la
    respuesta: nunca trunca en silencio. `max_documentos` y `timeout_por_documento`
    tienen que ser enteros >= 1; con 0 o negativos la auditoría se devuelve vacía o
    recortada por el final y parece completa, así que se rechazan con un error.
    """
    import subprocess  # local: solo esta herramienta lo necesita

    # Los dos números se validan ANTES de listar nada, porque un valor absurdo no
    # falla: MIENTE. `max_documentos=0` devuelve una auditoría vacía con cara de
    # completa; en negativo, `encontrados[:-5]` recorta los ÚLTIMOS cinco y el aviso
    # de truncado dice "los -5 primeros"; y un `timeout_por_documento` <= 0 hace que
    # subprocess corte al instante y TODOS los documentos salgan como timeout.
    try:
        max_documentos = int(max_documentos)
        timeout_por_documento = int(timeout_por_documento)
    except (TypeError, ValueError):
        return {"ok": False, "error":
                "max_documentos y timeout_por_documento deben ser enteros (recibidos: "
                "%r y %r)." % (max_documentos, timeout_por_documento)}
    if max_documentos < 1:
        return {"ok": False, "error":
                "max_documentos debe ser >= 1 (recibido: %d). Con 0 o negativo la "
                "auditoría saldría vacía o recortada por el final sin decirlo."
                % max_documentos}
    if timeout_por_documento < 1:
        return {"ok": False, "error":
                "timeout_por_documento debe ser >= 1 segundo (recibido: %d). Con 0 o "
                "negativo cada documento agotaría su timeout al instante y la "
                "auditoría diría que todos están colgados." % timeout_por_documento}

    carpeta = os.path.abspath(os.path.expandvars(ruta))
    if not os.path.isdir(carpeta):
        return {"ok": False, "error": "no existe o no es una carpeta: %s" % carpeta}

    encontrados = []
    if recursivo:
        for raiz, _dirs, ficheros in os.walk(carpeta):
            for f in ficheros:
                if f.lower().endswith(".mxd"):
                    encontrados.append(os.path.join(raiz, f))
    else:
        for f in sorted(os.listdir(carpeta)):
            if f.lower().endswith(".mxd"):
                encontrados.append(os.path.join(carpeta, f))
    encontrados.sort()

    total = len(encontrados)
    recortada = total > max_documentos
    documentos = encontrados[:max_documentos]

    aviso_capas = None
    py27 = _python27_arcgis() if con_capas else None
    auditor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auditor_mxd.py")
    if con_capas and (py27 is None or not os.path.isfile(auditor)):
        con_capas = False
        aviso_capas = ("No se pudo inspeccionar capas: falta el Python 2.7 de ArcGIS "
                       "(define ARCMAP_PYTHON27) o el auditor. Se devuelve solo la pasada 1.")

    resultados = []
    resumen = {"con_capas": 0, "timeout": 0, "error_al_abrir": 0, "ilegibles": 0}
    if con_capas:
        resumen.update((clave, 0) for clave in _SUMAS_AUDITOR)
        resumen["sin_abrir"] = 0
    versiones: dict = {}
    timeouts_seguidos = 0
    cortada = False

    for doc in documentos:
        version, err_version = _mxd_version_declarada(doc)
        fila = {
            "fichero": os.path.basename(doc),
            "ruta": doc,
            "tamano_bytes": os.path.getsize(doc),
            "version_declarada": version,
        }
        if version:
            versiones[version] = versiones.get(version, 0) + 1
        else:
            fila["problema_fichero"] = err_version
            resumen["ilegibles"] += 1

        if con_capas and cortada:
            fila["sin_abrir"] = True
            resumen["sin_abrir"] += 1
        elif con_capas:
            timeout_previo = resumen["timeout"]
            try:
                proc = subprocess.run(
                    [py27, auditor, doc],
                    capture_output=True, timeout=timeout_por_documento)
                bruto = proc.stdout.decode("utf-8", "replace").strip()
                datos = json.loads(bruto) if bruto else {"ok": False, "error": "sin salida"}
                if datos.get("ok"):
                    for clave in _CLAVES_AUDITOR:
                        fila[clave] = datos.get(clave)
                    for clave in _SUMAS_AUDITOR:
                        resumen[clave] += datos.get(clave) or 0
                    fila["marcos"] = datos.get("marcos")
                    fila["capas"] = datos.get("capas")
                    resumen["con_capas"] += 1
                else:
                    fila["error_al_abrir"] = datos.get("error") or "desconocido"
                    resumen["error_al_abrir"] += 1
            except subprocess.TimeoutExpired:
                # El proceso hijo muere con él; ArcMap y este servidor siguen enteros.
                fila["error_al_abrir"] = ("TIMEOUT tras %s s. Causa habitual: capas que apuntan "
                                          "a datos que no responden (unidades de red caídas)."
                                          % timeout_por_documento)
                fila["timeout"] = True
                resumen["timeout"] += 1
            except (json.JSONDecodeError, OSError) as exc:
                fila["error_al_abrir"] = "no se pudo auditar: %s" % exc
                resumen["error_al_abrir"] += 1
            # Seguidos, no en total: un documento con la red caída entre cien sanos
            # no debe cortar la auditoría; dos seguidos ya huelen a algo general.
            timeouts_seguidos = timeouts_seguidos + 1 if resumen["timeout"] > timeout_previo else 0
            if timeouts_seguidos >= _CORTE_TIMEOUTS_SEGUIDOS:
                cortada = True
                aviso_capas = (
                    "PASADA 2 CORTADA tras %d timeouts seguidos (de %d s cada uno): el resto "
                    "de documentos NO se ha abierto y va marcado con sin_abrir=true. Cuando "
                    "todo agota el timeout, la causa no suele ser el documento. Mira si hay "
                    "python.exe de ArcGIS huérfanos (tasklist) y si responden las unidades de "
                    "red de las capas; luego repite, o sube timeout_por_documento."
                    % (timeouts_seguidos, timeout_por_documento))

        resultados.append(fila)

    salida = {
        "ok": True,
        "carpeta": carpeta,
        "recursivo": recursivo,
        "mxd_encontrados": total,
        "mxd_auditados": len(documentos),
        "versiones": versiones,
        "resumen": resumen,
        "documentos": resultados,
    }
    if recortada:
        salida["aviso_truncado"] = ("Se encontraron %d .mxd y se auditaron los %d primeros "
                                    "(max_documentos). Los %d restantes NO están en esta "
                                    "respuesta." % (total, len(documentos), total - len(documentos)))
    if aviso_capas:
        salida["aviso_capas"] = aviso_capas
    return salida


@mcp.tool()
def export_mxd_lote(mxds: list, formato: str = "jpg", dpi: int = 230,
                    carpeta_salida: str = None, sobrescribir: bool = True,
                    timeout_por_documento: int = 600) -> dict:
    """
    Exporta el layout de VARIOS .mxd del disco, cada uno en su propio proceso.

    Es la herramienta para series de planos: `export_jpg`/`export_pdf` solo saben
    exportar el documento ABIERTO en ArcMap, y un bucle en `execute_arcpy` no sirve,
    porque **un proceso arcpy que ya ha exportado un layout no vuelve a exportar
    otro** (medido el 2026-09-22: en un mismo proceso sale el primero y fallan los
    siguientes). Aquí cada documento se exporta con un `python.exe` de ArcGIS
    nuevo, que es la única vía que ha dado cero fallos. No usa el puente: funciona
    con ArcMap abierto o cerrado, y no toca la sesión.

    - `mxds`: lista de rutas ABSOLUTAS a .mxd.
    - `formato`: "jpg" o "pdf". `dpi` entre 24 y 600 (230 por defecto, el de los
      planos). El JPG sale con la calidad por defecto de arcpy, no con la 95 de
      `export_jpg`: pesa más.
    - `carpeta_salida`: dónde dejar los ficheros. Sin indicar, cada uno JUNTO A SU
      .mxd y con su mismo nombre, que es como se entrega una serie.
    - `sobrescribir` (True por defecto, como los otros export): con False, un
      destino que ya existe se salta y se dice. Se exporta a un temporal y solo al
      final se mueve encima: un fallo no se lleva el plano que había.
    - `timeout_por_documento`: segundos por documento (600). Un documento con
      fuentes de red que no responden muere solo, y el lote sigue.

    Presupuesto: ~6 s de `import arcpy` por documento más lo que cueste dibujarlo;
    planos de ~100 capas con WMS salen a ~25 s cada uno. Un lote de 150 son una hora
    larga: pártelo si el cliente MCP corta antes.

    Devuelve, POR DOCUMENTO, el .mxd, la salida, los bytes y si pisó algo. Mira los
    bytes: un plano que pesa la mitad que sus vecinos es el primer síntoma de capas
    rotas o de una vista que no es la que crees.
    """
    import shutil
    import subprocess
    import tempfile
    import time

    formato = (formato or "jpg").lower().lstrip(".")
    if formato not in ("jpg", "pdf"):
        return {"ok": False, "error": "formato debe ser 'jpg' o 'pdf' (recibido: %r)." % formato}
    try:
        dpi = int(dpi)
        timeout_por_documento = int(timeout_por_documento)
    except (TypeError, ValueError):
        return {"ok": False, "error": "dpi y timeout_por_documento deben ser enteros."}
    if not 24 <= dpi <= 600:
        return {"ok": False, "error": "dpi fuera de rango (24-600): %s." % dpi}
    if timeout_por_documento < 1:
        return {"ok": False, "error": "timeout_por_documento debe ser >= 1."}
    if isinstance(mxds, str):
        mxds = [mxds]
    if not mxds:
        return {"ok": False, "error": "mxds está vacía: no hay nada que exportar."}
    if carpeta_salida and not os.path.isdir(carpeta_salida):
        return {"ok": False, "error": "carpeta_salida no existe: %s" % carpeta_salida}

    py27 = _python27_arcgis()
    exportador = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exportar_mxd.py")
    if py27 is None or not os.path.isfile(exportador):
        return {"ok": False, "error": "No se encuentra el Python 2.7 de ArcGIS (define "
                                      "ARCMAP_PYTHON27) o src/exportar_mxd.py."}

    resultados = []
    resumen = {"exportados": 0, "fallidos": 0, "saltados": 0}
    trabajos = tempfile.mkdtemp(prefix="arcmap-mcp-lote_")
    try:
        for i, bruto in enumerate(mxds):
            ruta = os.path.abspath(os.path.expandvars(str(bruto)))
            nombre = os.path.splitext(os.path.basename(ruta))[0] + "." + formato
            destino = os.path.join(carpeta_salida or os.path.dirname(ruta), nombre)
            fila = {"mxd": ruta, "salida": destino}
            if not (os.path.isfile(ruta) and ruta.lower().endswith(".mxd")):
                fila["error"] = "no existe o no es un .mxd"
                resumen["fallidos"] += 1
                resultados.append(fila)
                continue
            if os.path.exists(destino) and not sobrescribir:
                fila["saltado"] = "el destino ya existe y sobrescribir=false"
                resumen["saltados"] += 1
                resultados.append(fila)
                continue

            trabajo = os.path.join(trabajos, "trabajo_%d.json" % i)
            with open(trabajo, "w", encoding="utf-8") as f:
                json.dump({"mxd": ruta, "salida": destino, "formato": formato, "dpi": dpi},
                          f, ensure_ascii=False)
            inicio = time.time()
            try:
                proc = subprocess.run([py27, exportador, trabajo], capture_output=True,
                                      timeout=timeout_por_documento)
                bruto_salida = proc.stdout.decode("utf-8", "replace").strip()
                datos = json.loads(bruto_salida) if bruto_salida else {
                    "ok": False, "error": "sin salida; stderr: %s"
                    % proc.stderr.decode("utf-8", "replace")[-500:]}
            except subprocess.TimeoutExpired:
                datos = {"ok": False, "error": "TIMEOUT tras %s s (el proceso muere solo; el lote "
                                               "sigue). Causa habitual: fuentes de red que no "
                                               "responden." % timeout_por_documento}
            except (json.JSONDecodeError, OSError) as exc:
                datos = {"ok": False, "error": "no se pudo exportar: %s" % exc}

            # No basta con que el hijo diga ok: el fichero tiene que estar, tener
            # tamaño y ser de ESTA pasada. Un código de salida no prueba la salida.
            if datos.get("ok"):
                try:
                    st = os.stat(destino)
                    if st.st_size == 0 or st.st_mtime < inicio - 2:
                        datos = {"ok": False, "error": "el proceso dijo ok pero %s no es de esta "
                                                       "pasada o está vacío." % destino}
                except OSError:
                    datos = {"ok": False, "error": "el proceso dijo ok pero %s no existe." % destino}

            if datos.get("ok"):
                fila.update(bytes=datos.get("bytes"), sobrescrito=datos.get("sobrescrito"),
                            segundos=datos.get("segundos"))
                resumen["exportados"] += 1
            else:
                fila["error"] = datos.get("error")
                if datos.get("fase"):
                    fila["fase"] = datos["fase"]
                resumen["fallidos"] += 1
            resultados.append(fila)
    finally:
        shutil.rmtree(trabajos, ignore_errors=True)

    return {"ok": resumen["fallidos"] == 0, "formato": formato, "dpi": dpi,
            "total": len(mxds), "resumen": resumen, "documentos": resultados}


# --------------------------------------------------------------------------- #
# Argumentos estrictos.
#
# FastMCP descarta EN SILENCIO los argumentos que la tool no declara (su modelo
# pydantic ignora los extra). El 2026-09-22 una llamada a export_jpg con
# `mxd=<otro plano>` y `resolucion=230` —ninguno de los dos existe—
# devolvió `ok: true` tras exportar el documento ABIERTO, que era otro plano de
# otro monte. En un lote de 149 planos de cliente habría pisado 149 entregables con
# el mismo dibujo. Un argumento que no se entiende tiene que ser un error.
# --------------------------------------------------------------------------- #

from pydantic import BaseModel, model_validator  # noqa: E402


class _SinArgumentosDesconocidos(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _rechazar_desconocidos(cls, datos):
        if isinstance(datos, dict):
            validos = {f.alias or n for n, f in cls.model_fields.items()}
            sobran = sorted(set(datos) - validos)
            if sobran:
                raise ValueError(
                    "argumento(s) que esta herramienta NO admite: %s. Se rechazan en vez "
                    "de ignorarlos, porque ignorarlos hace otra cosa de la que pediste. "
                    "Admite: %s." % (", ".join(sobran), ", ".join(sorted(validos)) or "(ninguno)"))
        return datos


def _argumentos_estrictos(servidor):
    """Hace que TODAS las tools registradas rechacen argumentos desconocidos.

    Toca atributos internos de FastMCP (`_tool_manager`, `fn_metadata`): si una
    versión futura los cambia, `test_client_protocol` lo detecta.
    """
    for tool in servidor._tool_manager.list_tools():
        base = tool.fn_metadata.arg_model
        tool.fn_metadata.arg_model = type(base.__name__, (_SinArgumentosDesconocidos, base), {})
        tool.parameters["additionalProperties"] = False


_argumentos_estrictos(mcp)


if __name__ == "__main__":
    mcp.run()
