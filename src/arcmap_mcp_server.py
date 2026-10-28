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
import subprocess
import uuid

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

# Todo subproceso que lance el servidor va SIN VENTANA. Claude Desktop y Cowork lo
# arrancan sin consola, y entonces cada python.exe o tasklist hijo abre la suya: en
# un export_mxd_lote de 80 MXD son 80 ventanas robando el foco a quien esté
# trabajando en el equipo. CREATE_NO_WINDOW, nunca DETACHED_PROCESS: este deja al
# hijo sin consola y es cada nieto el que abre una ventana nueva.
#
# Y SIN HEREDAR EL STDIN. El stdin de este servidor es el pipe JSON-RPC del cliente, y
# FastMCP lo tiene SIEMPRE con una lectura pendiente esperando el siguiente mensaje. En
# Windows un pipe síncrono serializa las operaciones sobre el mismo objeto de fichero:
# el hijo que lo hereda se bloquea en su arranque (el CRT consulta los handles
# estándar) hasta que esa lectura termina, o sea hasta que el cliente manda otra cosa.
# Medido el 2026-10-06: export_mxd_lote cortaba 13 de 16 planos con 0,0 s de CPU (el
# hijo no llegaba ni a `import arcpy`) mientras el mismo export, lanzado a mano,
# tardaba 8 s. Era también el «cuelgue del arcpy de fuera» que no se reproducía: se
# probaba siempre fuera del MCP, donde nadie lee el stdin.
SIN_VENTANA = {"stdin": subprocess.DEVNULL}
if os.name == "nt":
    SIN_VENTANA["creationflags"] = 0x08000000

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

# Tope del add-in para un request (MaxRequestBytes en McpServer.cs). Se mira AQUÍ, antes
# de enviar: el add-in que recibe un request mayor contesta y cierra sin leer el resto, y
# Windows convierte ese cierre en un RST. El cliente veía WinError 10054 en vez del motivo,
# y el log del add-in no decía nada (reproducido el 2026-09-26 con un execute_code de
# 1,1 MB; era el «crash del puente» del informe de uso de agosto).
MAX_REQUEST_BYTES = 1024 * 1024

# --------------------------------------------------------------------------- #
# Versión mínima del add-in para cada comando o parámetro nuevo.
#
# El servidor corre desde el repo, pero el add-in nuevo solo entra al instalarlo y
# reiniciar ArcMap. Con servidor nuevo y add-in viejo, un parámetro que el add-in no
# conoce se IGNORA y la respuesta es `ok: true`: set_text_element(x=5) BORRABA el título
# (el add-in 2.16 hacía Text = texto ?? "" y el servidor nuevo ya no manda texto), y
# media docena más no hacían nada sin decirlo (revisión del 2026-10-01 y controles del
# 2026-10-06). Aquí, en un solo sitio, lo que exige cada versión: el comando entero
# (clave None), un parámetro (su nombre, si va en el mensaje) o un valor de un parámetro.
# Se sacó comparando las firmas de las tools entre v2.16.0 y la rama.
# --------------------------------------------------------------------------- #
_MINIMOS_ADDIN = {
    "rename_layer": {None: "2.17.0"},
    "open_mxd": {None: "2.17.0"},
    "set_legend": {None: "2.17.0", "indice": "2.17.0", "fuente": "2.17.0", "negrita_nombres": "2.17.0",
                   "negrita_etiquetas": "2.17.0", "parche": "2.17.0", "separaciones": "2.17.0"},
    "set_text_element": {"x": "2.17.0", "y": "2.17.0", "tamano": "2.17.0", "patron": "2.17.0",
                         "reemplazo": "2.17.0", "todos": "2.17.0", "mayusculas": "2.17.0",
                         "region": "2.17.0", "centrar_en": "2.17.0", "encajar_en": "2.17.0",
                         "margen": "2.17.0", "tamano_min": "2.17.0"},
    "set_legend_item": {"columna_nueva": "2.17.0", "quitar": "2.17.0", "posicion": "2.17.0",
                        "despues_de": "2.17.0"},
    "edit_symbol": {"etiqueta": "2.17.0", "quitar": "2.17.0", "anadir_valores": "2.17.0",
                    "encabezado": "2.17.0", "patron_linea": "2.17.0", "trama": "2.17.0",
                    "estilo_marcador": "2.17.0"},
    "set_single_symbology": {"patron_linea": "2.17.0", "trama": "2.17.0", "estilo_marcador": "2.17.0"},
    "set_labels": {"parte_mayor": "2.17.0", "sin_duplicados_m": "2.17.0", "tamano_minimo_m": "2.17.0",
                   "repetir_cada_mm": "2.17.0", "copiar_de": "2.17.0", "por_valor": "2.17.0",
                   "where": "2.17.0", "fuente": "2.17.0", "negrita": "2.17.0", "cursiva": "2.17.0",
                   "nunca_quitar": "2.17.0", "sobrepasar_entidad": "2.17.0", "buffer": "2.17.0",
                   "colocacion_linea": "2.17.0"},
    "set_unique_values_symbology": {"campos": "2.17.0", "delimitador": "2.17.0", "clases": "2.17.0",
                                    "resto": "2.17.0"},
    "set_raster_symbology": {("modo", "rgb"): "2.17.0", "bandas": "2.17.0", "cortes": "2.17.0",
                             ("etiquetas", "auto"): "2.17.0", ("etiquetas", "auto<"): "2.17.0",
                             "conservar_colores": "2.17.0"},
    "set_graduated_symbology": {"cortes": "2.17.0", "etiquetas": "2.17.0"},
    "move_layer": {"en_leyenda": "2.17.0"},
    "set_frame_clip": {None: "2.17.0"},
    "duplicate_layer": {None: "2.17.0"},
    "replace_data_source": {None: "2.17.0"},
    "replace_text": {None: "2.17.0"},
    "set_definition_query": {"anadir": "2.17.0"},
    "add_layer": {"si_existe": "2.17.0"},
    "save_layer_as_lyr": {None: "2.17.0"},
    "copy_layout_elements": {None: "2.17.0"},
}


def _version_addin(texto):
    """'arcmap-mcp 2.16.0 (.NET)' -> (2, 16, 0); None si no trae versión."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", texto or "")
    return tuple(int(x) for x in m.groups()) if m else None


def _minimo_addin(ctype, params):
    """(versión mínima como tupla, qué la exige) para este envío; (None, None) si no hay."""
    exige, que = None, None
    for clave, version in _MINIMOS_ADDIN.get(ctype, {}).items():
        if clave is None:
            motivo = "el comando %s" % ctype
        elif isinstance(clave, tuple):
            if (params or {}).get(clave[0]) != clave[1]:
                continue
            motivo = "%s(%s=%r)" % (ctype, clave[0], clave[1])
        elif (params or {}).get(clave) is not None:
            motivo = "el parámetro '%s' de %s" % (clave, ctype)
        else:
            continue
        v = _version_addin(version)
        if exige is None or v > exige:
            exige, que = v, motivo
    return exige, que


class ArcMapClient:
    """Cliente socket hacia el puente dentro de ArcMap. Reconecta por comando."""

    def __init__(self, host=HOST, port=PORT, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._version = None          # del add-in, leída con ping; se olvida si el puente cae

    def _version_del_addin(self, refrescar=False):
        """Versión del add-in que escucha, como tupla; None si no se sabe (puente caído u
        ocupado: entonces el comando sigue y falla con su propio error, que ya es claro)."""
        if self._version is None or refrescar:
            r = self._enviar("ping", {}, self.timeout)
            self._version = _version_addin((r.get("result") or {}).get("addin")) if r.get("ok") else None
        return self._version

    def _comprobar_version(self, ctype, params):
        """Error con forma de respuesta si el add-in es anterior a lo que exige el envío."""
        minimo, motivo = _minimo_addin(ctype, params)
        if minimo is None:
            return None
        tiene = self._version_del_addin()
        if tiene is not None and tiene < minimo:
            # La versión guardada puede ser de antes de reinstalar y reiniciar ArcMap:
            # antes de negarse, se pregunta otra vez.
            tiene = self._version_del_addin(refrescar=True)
        if tiene is None or tiene >= minimo:
            return None
        return {"ok": False, "estado": "addin_antiguo", "error": (
            "ADD-IN ANTIGUO: %s necesita el add-in %s y el que tiene ArcMap es el %s. No se ha "
            "enviado, porque el add-in viejo ignoraría lo nuevo y respondería ok sin hacerlo. "
            "Instala el .esriaddin de esta versión y REINICIA ArcMap (el add-in se carga al "
            "arrancar)." % (motivo, ".".join(map(str, minimo)), ".".join(map(str, tiene))))}

    def send(self, ctype, params=None, timeout=None):
        """Envía un comando al puente. `timeout` (s) sobrescribe el del cliente para
        esta llamada: cada tool pasa el presupuesto (`_Espera`) que le corresponde
        según el tope que el add-in aplique a ESE comando."""
        error = self._comprobar_version(ctype, params)
        if error:
            return error
        r = self._enviar(ctype, params, timeout)
        if r.get("estado") == "puente_caido":
            self._version = None      # al volver puede ser otro ArcMap con otro add-in
        elif ctype == "ping" and r.get("ok") and (r.get("result") or {}).get("addin"):
            self._version = _version_addin(r["result"]["addin"])
        return r

    def _enviar(self, ctype, params=None, timeout=None):
        eff = timeout or self.timeout
        msg = json.dumps({"type": ctype, "params": params or {}}).encode("utf-8")
        if len(msg) > MAX_REQUEST_BYTES:
            return {
                "ok": False,
                "error": (
                    "Request DEMASIADO GRANDE: %d KB, y el puente admite %d KB. No se ha "
                    "enviado. Si estás mandando datos a granel dentro de 'code', escríbelos "
                    "a un fichero y que el código los lea de ahí."
                    % (len(msg) // 1024, MAX_REQUEST_BYTES // 1024)
                ),
            }
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
    streams, error = _mxd_streams(ruta, ("Version",))
    if error:
        return None, error
    raw = streams.get("Version")
    if raw is None:
        return None, "el .mxd no tiene stream Version"
    if len(raw) < 4:
        return None, "el stream Version salió vacío"
    nbytes = struct.unpack_from("<I", raw, 0)[0]
    if nbytes <= 0 or nbytes > len(raw) - 4:
        return None, "el stream Version tiene una longitud incoherente"
    txt = raw[4:4 + nbytes].decode("utf-16-le", "replace").rstrip("\x00")
    return (txt.strip() or None), (None if txt.strip() else "versión vacía")


# Clase del marco OLE (objeto incrustado, p. ej. un documento de Word) en ArcMap 10.5. No
# está en el registro: la sirve ArcMap desde su propio proceso, y por eso, con ArcMap
# abierto, un arcpy de fuera que carga un .mxd con uno se queda esperando para siempre
# (medido 2026-09-26; ver MarcosOle.cs). Leída del propio ArcMap con IPersist.GetClassID.
_CLSID_MARCO_OLE = uuid.UUID("f6705e85-523b-11d1-86e7-0000f8751720").bytes_le


def _mxd_marcos_ole(ruta):
    """Devuelve (n, error): cuántos marcos OLE tiene el layout del .mxd, sin abrirlo.

    El layout vive en `PageLayout` en lo que guarda ArcMap; lo que guarda ArcObjects desde
    fuera (MapDocument.SaveAs) lo escribe en `Mx Document` y deja el `PageLayout` antiguo sin
    tocar. Si hay `Mx Document`, manda ese. Validado contra 7 documentos (el plano del caso,
    sus copias por ArcMap, arcpy y ArcObjects, y tres sin marcos)."""
    streams, error = _mxd_streams(ruta, ("Mx Document", "PageLayout"))
    if error:
        return None, error
    capa = streams.get("Mx Document") if "Mx Document" in streams else streams.get("PageLayout")
    if capa is None:
        return None, "el .mxd no tiene layout legible"
    return capa.count(_CLSID_MARCO_OLE), None


def _cfb_cadena(inicio, tabla, tope):
    """Sectores de la cadena que empieza en `inicio`, como mucho `tope`. Se corta al repetir un
    sector: un compound file corrupto (o fabricado) puede tener una cadena que se apunta a sí
    misma, y sin este corte un .mxd de 2,5 KB llegó a 2,1 GB de memoria (revisión 2026-10-01)."""
    out, vistos, s = [], set(), inicio
    while s < _MARCA and s not in vistos and len(out) < tope:
        vistos.add(s)
        out.append(s)
        s = tabla[s] if s < len(tabla) else 0xFFFFFFFE
    return out


def _cfb_fat(datos, ssz):
    """(fat, n_sec) de un compound file: la FAT completa (DIFAT de la cabecera más su cadena)
    y el número de sectores que caben en el fichero, que acota todo lo demás.

    La DIFAT trae 109 entradas en la cabecera; un documento de más de ~7 MB (sectores de
    512 B) necesita más y esas cuelgan de una CADENA de sectores. Saltársela no da error: deja
    la FAT corta, las cadenas se truncan y los streams salen VACÍOS, o sea un fichero sano que
    parece ilegible. Pasó con 92 de 400 .mxd al validar esto."""
    n_sec = max((len(datos) - 512) // ssz, 0)
    difat0, n_difat = struct.unpack_from("<II", datos, 68)
    difat = list(struct.unpack_from("<109I", datos, 76))
    sec, vistos = difat0, set()
    while sec < _MARCA and sec not in vistos and len(vistos) <= min(n_difat, n_sec):
        # la cadena de la DIFAT no está en la FAT: cada bloque trae el siguiente al final
        vistos.add(sec)
        bloque = struct.unpack_from("<%dI" % (ssz // 4), datos, 512 + sec * ssz)
        difat.extend(bloque[:-1])
        sec = bloque[-1]
    fat = []
    for s in difat:
        if len(fat) >= n_sec:
            break                                 # entradas de sectores que no existen
        if s < _MARCA:
            fat.extend(struct.unpack_from("<%dI" % (ssz // 4), datos, 512 + s * ssz))
    return fat, n_sec


def _mxd_streams(ruta, nombres):
    """Devuelve ({nombre: bytes}, error) con los streams de primer nivel pedidos que existan."""
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
        fat, n_sec = _cfb_fat(datos, ssz)

        def cadena(inicio, tope=n_sec):
            return _cfb_cadena(inicio, fat, tope)

        def leer_sectores(inicio, size):
            trozos = (datos[512 + s * ssz:512 + (s + 1) * ssz] for s in cadena(inicio, -(-size // ssz)))
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
            trozos = (mini[s * mssz:(s + 1) * mssz] for s in _cfb_cadena(inicio, mfat, -(-size // mssz)))
            return b"".join(trozos)[:size]

        salida = {}
        for nombre, tipo, inicio, size in entradas:
            if nombre not in nombres or tipo != 2:
                continue
            salida[nombre] = leer_mini(inicio, size) if size < 4096 else leer_sectores(inicio, size)
        return salida, None
    except (struct.error, IndexError, ValueError) as exc:
        return None, "estructura del .mxd ilegible: %s" % exc


def _cfb_streams_anidados(ruta, quiere):
    """{"Embedding 1/WordDocument": bytes, ...} de los streams del compound file cuya ruta
    completa cumple `quiere(ruta)`; (None, error) si no se puede leer.

    A diferencia de _mxd_streams (solo primer nivel, validado contra 400 .mxd y que no se
    toca), recorre el árbol rojo-negro del directorio: las entradas se enlazan por ÍNDICE
    (izquierdo, derecho, hijo), así que se guardan todas, también las vacías."""
    try:
        with open(ruta, "rb") as fh:
            datos = fh.read()
    except OSError as exc:
        return None, "no se pudo leer el fichero: %s" % exc
    if datos[:8] != _FIRMA_CFB:
        return None, "no es un compound document (.mxd)"
    try:
        ssz = 1 << struct.unpack_from("<H", datos, 30)[0]
        mssz = 1 << struct.unpack_from("<H", datos, 32)[0]
        dir0, = struct.unpack_from("<I", datos, 48)
        mfat0, = struct.unpack_from("<I", datos, 60)
        fat, n_sec = _cfb_fat(datos, ssz)

        entradas = []
        for s in _cfb_cadena(dir0, fat, n_sec):
            for i in range(ssz // 128):
                e = 512 + s * ssz + i * 128
                nlen, = struct.unpack_from("<H", datos, e + 64)
                entradas.append((datos[e:e + max(nlen - 2, 0)].decode("utf-16-le", "replace"),
                                 datos[e + 66], struct.unpack_from("<III", datos, e + 68),
                                 struct.unpack_from("<I", datos, e + 116)[0],
                                 struct.unpack_from("<I", datos, e + 120)[0]))

        def sectores(inicio, size, tabla, tam, fuente, base):
            """Los `size` bytes del stream, sin leer más sectores de los que hacen falta."""
            trozos = (fuente[base + s * tam:base + (s + 1) * tam]
                      for s in _cfb_cadena(inicio, tabla, -(-size // tam)))
            return b"".join(trozos)[:size]

        raiz = entradas[0]
        mini = sectores(raiz[3], raiz[4], fat, ssz, datos, 512)
        mfat = []
        for s in _cfb_cadena(mfat0, fat, n_sec):
            mfat.extend(struct.unpack_from("<%dI" % (ssz // 4), datos, 512 + s * ssz))

        def leer(inicio, size):
            if size < 4096:
                return sectores(inicio, size, mfat, mssz, mini, 0)
            return sectores(inicio, size, fat, ssz, datos, 512)

        salida = {}
        pendientes = [(raiz[2][2], "")]          # (índice, prefijo): hijos de la raíz
        vistos = set()
        while pendientes:
            idx, prefijo = pendientes.pop()
            if idx >= len(entradas) or idx in vistos or idx == 0xFFFFFFFF:
                continue
            vistos.add(idx)
            nombre, tipo, (izq, der, hijo), inicio, size = entradas[idx]
            pendientes += [(izq, prefijo), (der, prefijo)]
            camino = prefijo + nombre
            if tipo == 1:                         # storage: bajar
                pendientes.append((hijo, camino + "/"))
            elif tipo == 2 and quiere(camino):
                salida[camino] = leer(inicio, size)
        return salida, None
    except (struct.error, IndexError, ValueError) as exc:
        return None, "estructura ilegible: %s" % exc


def _almacenes_vivos(ruta):
    """Números N de los "Embedding N" a los que apuntan los marcos OLE del layout, en orden.

    Un .mxd ACUMULA copias antiguas de los objetos incrustados (el Plano 04 de un expediente real
    llevaba 17 para 2 marcos, con tres versiones distintas del contenido). El marco guarda el
    número de su almacén en el cuarto entero de 32 bits tras el CLSID de la clase OleFrame.
    Validado el 2026-10-01 contra los 8 planos con OLE de un expediente real (ver la nota del pendiente)."""
    streams, error = _mxd_streams(ruta, ("Mx Document", "PageLayout"))
    if error:
        return None
    capa = streams.get("Mx Document") if "Mx Document" in streams else streams.get("PageLayout")
    if capa is None:
        return None
    numeros, i = [], 0
    while True:
        i = capa.find(_CLSID_MARCO_OLE, i)
        if i < 0 or i + 32 > len(capa):
            return numeros
        numeros.append(struct.unpack_from("<I", capa, i + 16 + 12)[0])
        i += 16


def _leer_tablas_ole(ruta, incluir_antiguos=False):
    """Lo que hace get_ole_tables, sin el decorador (para poder probarlo)."""
    nombres = ("WordDocument", "1Table", "0Table", "Data", "\x01CompObj", "Package")
    streams, error = _cfb_streams_anidados(ruta, lambda p: p.rsplit("/", 1)[-1] in nombres)
    if error:
        return {"ok": False, "error": error}
    almacenes = {}
    for camino, contenido in streams.items():
        if "/" not in camino:
            continue
        carpeta, hoja = camino.rsplit("/", 1)
        almacenes.setdefault(carpeta, {})[hoja] = contenido
    vivos = _almacenes_vivos(ruta)
    nombres_vivos = None if vivos is None else ["Embedding %d" % n for n in vivos]

    def numero(carpeta):
        try:
            return int(carpeta.rsplit(" ", 1)[-1])
        except ValueError:
            return 0

    objetos, antiguos = [], 0
    for carpeta in sorted(almacenes, key=numero):
        en_layout = None if nombres_vivos is None else carpeta in nombres_vivos
        if en_layout is False and not incluir_antiguos:
            # Un objeto dentro de otro (Embedding N/ObjectPool/...) no es una copia vieja:
            # contarlo inflaba el número.
            antiguos += "/" not in carpeta
            continue
        partes = almacenes[carpeta]
        compobj = partes.get("\x01CompObj", b"")
        tipo = next((t for t in compobj.decode("latin-1").split("\x00") if len(t) > 6 and t.isprintable()),
                    "(desconocido)")
        obj = {"almacen": carpeta, "tipo": tipo, "en_el_layout": en_layout}
        try:
            if "Package" in partes:
                filas, texto = _filas_docx(partes["Package"])
                if filas:
                    obj["filas"] = filas
                else:
                    obj["texto"] = texto
            elif "WordDocument" in partes:
                wd = partes["WordDocument"]
                flags, = struct.unpack_from("<H", wd, 0x0A)
                tabla = partes.get("1Table" if flags & 0x0200 else "0Table")
                if tabla is None:
                    raise ValueError("falta el stream de tabla del documento")
                texto = _texto_word97(wd, tabla)
                try:
                    filas = _filas_word97(wd, tabla, partes.get("Data", b""))
                except (struct.error, IndexError, ValueError):
                    filas = None
                if filas is None and "\x07" in texto:
                    # sin las propiedades de párrafo no se distingue una celda vacía de un fin
                    # de fila: el resultado puede salir con filas de menos o desalineadas
                    filas = _filas_de_tabla_word(texto)
                    obj["aviso"] = ("filas deducidas solo del texto: si la tabla tiene celdas vacías"
                                    " pueden salir filas de menos o desalineadas")
                if filas:
                    obj["filas"] = filas
                else:
                    obj["texto"] = texto.replace("\r", "\n").strip()
            else:
                obj["error"] = "no es un documento de Word: no se sabe leer este tipo de objeto"
        except Exception as exc:  # zip/xml/struct: un objeto ilegible no tumba los demás
            obj["error"] = "no se pudo leer el documento: %s" % exc
        objetos.append(obj)
    res = {"ruta": ruta, "num_objetos": len(objetos), "objetos": objetos}
    if nombres_vivos is None:
        res["aviso"] = ("no se pudo leer el layout para saber qué objetos usan los marcos: se listan"
                        " TODOS los almacenes, y puede haber copias antiguas")
    elif antiguos:
        res["copias_antiguas_omitidas"] = antiguos
    return {"ok": True, "result": res}


@mcp.tool()
def get_ole_tables(ruta: str, incluir_antiguos: bool = False) -> dict:
    """
    Lee las tablas de los objetos OLE incrustados en un .mxd (en planos reales, tablas
    de Word 97-2003 en el layout), **desde el fichero**: sin ArcMap, sin Word, sin abrir el
    documento y sin modificar nada. Sirve para verificar una entrega: una errata en una de
    esas tablas solo se veía mirando el JPG.

    - `ruta`: .mxd en disco (absoluta). Puede estar abierto en ArcMap: se lee lo GUARDADO.
    - `incluir_antiguos`: un .mxd acumula copias viejas de sus objetos incrustados (17 para
      2 tablas en un plano real, con versiones distintas). Por defecto solo salen los que usan
      los marcos del layout; con True salen todos, marcados con `en_el_layout`.

    Devuelve cada objeto (`almacen`, `tipo`, `en_el_layout`) con sus `filas` (lista de
    celdas) o su `texto` si no es una tabla. Lee Word 97-2003 y Word 2007+, con las celdas
    vacías en su sitio. Si un objeto trae `aviso`, sus filas se dedujeron solo del texto y
    pueden salir desalineadas: no lo des por verificado. Lee los DATOS del objeto; lo que
    ArcMap dibuja es una imagen cacheada que normalmente coincide.

    **Solo lectura, a propósito.** Editar estas tablas no es viable: activar el objeto desde el
    puente bloquea ArcMap con un diálogo, abre Word en la pantalla y lo editado no vuelve al
    plano (probado el 2026-10-01). Una corrección se hace a mano en ArcMap (doble clic en la
    tabla) y se comprueba después con esta herramienta.
    """
    if not ruta or not os.path.isabs(ruta) or not ruta.lower().endswith(".mxd"):
        return {"ok": False, "error": "'ruta' debe ser la ruta ABSOLUTA de un .mxd. Recibido: %r" % ruta}
    if not os.path.isfile(ruta):
        return {"ok": False, "error": "No existe el fichero: %s" % ruta}
    return _leer_tablas_ole(ruta, incluir_antiguos)


def _filas_docx(paquete):
    """(filas, texto) de un Word 2007+ incrustado (stream "Package": un .docx en zip): las
    filas de sus tablas y, para el caso sin tablas, el texto de sus párrafos."""
    import io
    import zipfile
    import xml.etree.ElementTree as ET
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(paquete)) as z:
        raiz = ET.fromstring(z.read("word/document.xml"))
    filas = []
    for tr in raiz.iter(w + "tr"):
        celdas = []
        for tc in tr.findall(w + "tc"):
            parrafos = ["".join(t.text or "" for t in p.iter(w + "t")) for p in tc.iter(w + "p")]
            celdas.append(" ".join(x for x in parrafos if x).strip())
        if any(celdas):
            filas.append(celdas)
    texto = "\n".join("".join(t.text or "" for t in p.iter(w + "t")) for p in raiz.iter(w + "p")).strip()
    return filas, texto


def _piezas_word97(word_document, tabla):
    """[(carácter, posición en el stream WordDocument)] del texto principal de un Word
    97-2003, leído por la tabla de piezas (CLX). La posición es la que usan las propiedades
    de párrafo para decir a qué párrafo pertenece cada carácter."""
    ccp, = struct.unpack_from("<i", word_document, 0x4C)
    fc_clx, lcb_clx = struct.unpack_from("<II", word_document, 0x01A2)
    clx = tabla[fc_clx:fc_clx + lcb_clx]
    i = 0
    while clx[i] == 0x01:                         # Prc: propiedades, se saltan
        i += 3 + struct.unpack_from("<H", clx, i + 1)[0]
    if clx[i] != 0x02:
        raise ValueError("CLX sin tabla de piezas")
    lcb, = struct.unpack_from("<I", clx, i + 1)
    plc = clx[i + 5:i + 5 + lcb]
    n = (lcb - 4) // 12
    cps = struct.unpack_from("<%di" % (n + 1), plc, 0)
    salida = []
    for k in range(n):
        fc, = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)
        largo = cps[k + 1] - cps[k]
        if fc & 0x40000000:                       # pieza en cp1252, un byte por carácter
            fc = (fc & 0x3FFFFFFF) // 2
            texto = word_document[fc:fc + largo].decode("cp1252", "replace")
            salida += [(c, fc + j) for j, c in enumerate(texto)]
        else:
            texto = word_document[fc:fc + 2 * largo].decode("utf-16-le", "replace")
            salida += [(c, fc + 2 * j) for j, c in enumerate(texto)]
    return salida[:ccp]


def _texto_word97(word_document, tabla):
    """Texto principal de un documento Word 97-2003 (streams WordDocument y 1Table/0Table).
    Sin Word y sin dependencias."""
    return "".join(c for c, _ in _piezas_word97(word_document, tabla))


_SPRM_P_EN_TABLA, _SPRM_P_FIN_DE_FILA = 0x2416, 0x2417     # sprmPFInTable, sprmPFTtp
_SPRM_P_HUGE_PAPX = 0x6646                                  # propiedades en el stream Data


def _parrafos_word97(word_document, tabla, data=b""):
    """[(fc_inicio, fc_fin, en_tabla, fin_de_fila)] de los párrafos, ordenados, leídos de las
    propiedades de párrafo (PlcBtePapx del stream de tabla → páginas PapxFkp de 512 bytes).

    En el texto, una celda vacía y un fin de fila son los dos un \\x07: solo esta propiedad
    (sprmPFTtp) dice cuál es cuál. Sin ella, «A | (vacía)» seguido de «C | D» salía como una
    sola fila ['', 'C', 'D'] (revisión del 2026-10-01), y el TOTAL del Plano 09 de un expediente real
    salía partido en dos filas.

    Cuando las propiedades no caben en la página (el fin de fila lleva la definición de la
    tabla entera), el párrafo solo trae sprmPHugePapx: un puntero al stream `Data`. Pasa en
    tablas anchas de documentos Word 97 (nFib 0xC1): sin seguirlo no aparece ningún fin de fila."""
    fc, lcb = struct.unpack_from("<II", word_document, 0x0102)       # fcPlcfBtePapx
    plc = tabla[fc:fc + lcb]
    n = (lcb - 4) // 8
    if n <= 0 or len(plc) < lcb:
        raise ValueError("el documento no trae propiedades de párrafo")
    parrafos = []
    for k in range(n):
        pn = struct.unpack_from("<I", plc, 4 * (n + 1) + 4 * k)[0] & 0x3FFFFF
        pagina = word_document[pn * 512:(pn + 1) * 512]
        crun = pagina[511]
        rgfc = struct.unpack_from("<%dI" % (crun + 1), pagina, 0)
        for j in range(crun):
            off = pagina[4 * (crun + 1) + 13 * j] * 2
            if off == 0:                          # párrafo sin propiedades propias
                parrafos.append((rgfc[j], rgfc[j + 1], False, False))
                continue
            cb = pagina[off]
            grp = (pagina[off + 2:off + 2 + 2 * pagina[off + 1]] if cb == 0
                   else pagina[off + 1:off + 2 * cb])
            props = _sprms_de_parrafo(grp[2:])  # los 2 primeros bytes son el estilo (istd)
            if _SPRM_P_HUGE_PAPX in props:
                pos, = struct.unpack_from("<I", props[_SPRM_P_HUGE_PAPX])
                largo, = struct.unpack_from("<H", data, pos)
                props.update(_sprms_de_parrafo(data[pos + 2:pos + 2 + largo]))
            parrafos.append((rgfc[j], rgfc[j + 1], props.get(_SPRM_P_EN_TABLA, b"\0")[0] != 0,
                             props.get(_SPRM_P_FIN_DE_FILA, b"\0")[0] != 0))
    parrafos.sort()
    return parrafos


def _sprms_de_parrafo(grpprl):
    """{sprm: operando} de una lista de propiedades de Word 97."""
    props, i = {}, 0
    while i + 2 < len(grpprl):
        sprm, = struct.unpack_from("<H", grpprl, i)
        spra = sprm >> 13                         # tamaño del operando según el tipo
        if spra in (0, 1):
            largo = 1
        elif spra in (2, 4, 5):
            largo = 2
        elif spra == 3:
            largo = 4
        elif spra == 7:
            largo = 3
        elif sprm in (0xD606, 0xD608):            # sprmTDefTable: longitud en 2 bytes
            largo = 1 + struct.unpack_from("<H", grpprl, i + 2)[0]
        else:
            largo = 1 + grpprl[i + 2]
        props[sprm] = grpprl[i + 2:i + 2 + largo]
        i += 2 + largo
    return props


def _filas_word97(word_document, tabla, data=b""):
    """Filas de las tablas de un Word 97-2003 por su ESTRUCTURA: una celda acaba en un \\x07
    de un párrafo normal y la fila en el \\x07 del párrafo marcado como fin de fila. Las celdas
    vacías y las filas vacías se conservan, y el texto fuera de las tablas no entra.

    None si el documento no trae las propiedades que hacen falta (el que llama decide)."""
    import bisect
    parrafos = _parrafos_word97(word_document, tabla, data)
    inicios = [p[0] for p in parrafos]
    piezas = _piezas_word97(word_document, tabla)
    filas, fila, celda, hubo_fin_de_fila = [], [], [], False
    for c, fc in piezas:
        k = bisect.bisect_right(inicios, fc) - 1
        en_tabla, fin_de_fila = (parrafos[k][2], parrafos[k][3]) if k >= 0 and fc < parrafos[k][1] \
            else (False, False)
        if c == "\x07" and fin_de_fila:
            filas.append(fila)
            fila, celda, hubo_fin_de_fila = [], [], True
        elif c == "\x07" and en_tabla:
            fila.append("".join(celda).replace("\r", " ").replace("\x0b", " ").strip())
            celda = []
        elif en_tabla:
            celda.append(c)
    if not hubo_fin_de_fila:
        return None if any(c == "\x07" for c, _ in piezas) else []
    return filas


def _filas_de_tabla_word(texto):
    """Filas y celdas de las tablas de un texto de Word: cada celda acaba en \\x07 y cada fila
    en una marca de fin de fila (otro \\x07 suelto). Solo como RESERVA de _filas_word97,
    cuando el documento no trae propiedades de párrafo: una celda vacía también deja \\x07\\x07
    y se toma por fin de fila, así que con huecos las filas salen mal (el que llama avisa).

    Otro límite conocido: el texto plano no dice dónde empieza la tabla. Si hay párrafos ANTES de
    ella, acaban pegados a la primera celda; se separan por el último salto de párrafo del
    primer trozo, lo que solo falla si esa primera celda tiene varios párrafos. En los planos
    reales el documento empieza por la tabla."""
    filas = []
    for bruta in texto.split("\x07\x07"):
        if "\x07" not in bruta and not filas:
            continue                              # texto sin tabla antes de la primera
        if not filas and "\r" in bruta.split("\x07", 1)[0]:
            bruta = bruta[bruta.split("\x07", 1)[0].rfind("\r") + 1:]
        celdas = [c.replace("\r", " ").replace("\x0b", " ").strip() for c in bruta.split("\x07")]
        if any(celdas):
            filas.append(celdas)
    return filas


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

    Dos prefijos más, válidos en cualquier tool que pida una capa (y en `edit_mxd_lote`,
    donde una ruta fija no vale para todos los planos de una serie):
      - `visible:Parcelas`: solo entre las capas que se DIBUJAN (ella y sus grupos
        encendidos). Las homónimas de grupos apagados dejan de estorbar.
      - `fuente:Rodales_V1`: la capa cuya fuente (carpeta\\dataset) contiene ese texto, se
        llame como se llame en la TOC. Combinables: `visible:fuente:Rodales_V1`.
    Con varias candidatas, el mismo error con sus rutas.
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

    NADA de lo que hagas sobre `mxd` o `df` se ve en ArcMap: es la copia, y se tira
    al terminar. No sirve para encuadrar, añadir capas ni simbolizar el mapa que ve
    el usuario. Para eso hay herramientas: encuadrar → `zoom_to_layer`, `set_extent`
    o `set_scale`; añadir capas → `add_layer`; simbolizar → `set_single_symbology`,
    `set_unique_values_symbology`, `set_graduated_symbology` o
    `set_raster_symbology`; filtrar → `set_definition_query`.

    Si el código abre con `MapDocument` un .mxd (por ruta escrita en el código) que tiene
    marcos OLE, NO se lanza: con ArcMap abierto se colgaría al cargarlo, y guardarlo con
    arcpy de fuera lo corrompe. Para esos planos, las herramientas del puente y `save_mxd`.

    Si tu código lanza subprocesos, hazlo con
    `creationflags=0x08000000` (CREATE_NO_WINDOW): sin él, cada uno abre una ventana
    de consola que roba el foco a quien esté usando el equipo. Nunca con
    DETACHED_PROCESS (0x8): deja al hijo sin consola y cada nieto abre una nueva.

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
    con_ole = _mxd_con_ole_en_codigo(code)
    if con_ole:
        return {"ok": False, "error": (
            "marcos_ole: no se ha lanzado. El código abre con MapDocument %s, que tiene marcos OLE"
            " (objetos incrustados, p. ej. tablas de Excel). Con ArcMap abierto, el arcpy de fuera se"
            " queda colgado al cargarlos; y con ArcMap cerrado, guardarlo con save()/saveACopy() lo"
            " corrompe (el 2026-10-01, uno de 8 planos tumbaba ArcMap al abrirlo). Este freno vale"
            " también para solo LEER: execute_arcpy corre siempre con ArcMap abierto, que es justo"
            " cuando se cuelga. Para LEER sus capas, fuentes y filtros: ábrelo en ArcMap con"
            " open_mxd y usa list_layers / get_layer_info / list_broken_data_sources; o, con ArcMap"
            " CERRADO, audit_folder sobre su carpeta (no necesita el puente). Para CAMBIARLO,"
            " ábrelo en ArcMap y usa las herramientas del puente (rename_layer, edit_symbol,"
            " set_text_element, set_legend_item...) y save_mxd." % ", ".join(
                "'%s' (%d)" % (ruta, n) for ruta, n in con_ole))}
    params: dict = {"code": code}
    if usar_documento is not None:
        params["usar_documento"] = usar_documento
    if serializar_sesion:
        params["serializar_sesion"] = True
    return _client.send("execute_code", params,
                        timeout=EXEC_SESION_TIMEOUT if serializar_sesion else EXEC_TIMEOUT)


_RE_RUTA_MXD = re.compile(r"""([rRuU]{0,2})(["'])((?:(?!\2).)+?\.mxd)\2""", re.IGNORECASE)


def _mxd_con_ole_en_codigo(code):
    """[(ruta, n_marcos)] de los .mxd con marcos OLE que el código abre por ruta literal.

    Solo mira si hay `MapDocument`: una ruta .mxd en un texto o un print no abre nada. Se
    lee el fichero sin abrirlo (`_mxd_marcos_ole`, milisegundos), así que no cuesta lanzar
    el freno. Las rutas construidas en tiempo de ejecución no se ven: es un freno para el
    caso habitual (una ruta escrita en el código), no una garantía."""
    if not code or "MapDocument" not in code:
        return []
    salida = []
    for m in _RE_RUTA_MXD.finditer(code):
        ruta = m.group(3)
        if "r" not in m.group(1).lower():
            ruta = ruta.replace("\\\\", "\\")
        if not os.path.isfile(ruta):
            continue
        n, _err = _mxd_marcos_ole(ruta)
        if n:
            salida.append((ruta, n))
    return salida


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
                    leyenda: str = None, indice: int = None, columna_nueva: bool = None,
                    quitar: bool = None, posicion: int = None, despues_de: str = None) -> dict:
    """
    Cambia QUÉ muestra la entrada de una capa en la leyenda del layout: el nombre de
    la capa, el encabezado y las etiquetas de sus clases; y DÓNDE está la entrada, o la
    quita. **No cambia CÓMO se ve**: las fuentes y tamaños se quedan como estaban, y la
    respuesta los devuelve leídos (`fuentes`) para comprobarlo, junto con el `orden`
    de las entradas de esa leyenda. Solo se toca lo que se pase; None = no cambiar.
    Sin ningún interruptor, devuelve el estado actual sin tocar nada.

    - `quitar=True`: quita la entrada de la leyenda y deja la capa en el mapa (para que
      una leyenda quepa sin las capas que no salen en esa hoja). Va sola: no se combina
      con nada más.
    - `posicion`: el puesto de la entrada en la leyenda, de 1 al número de entradas.
    - `despues_de`: el nombre de otra entrada de la MISMA leyenda; esta queda justo
      detrás. Hace falta porque la leyenda no sigue la TOC: tras `move_layer`, la
      entrada se queda donde estaba. `posicion` y `despues_de` son excluyentes.

    `capa` es el nombre con que la capa aparece en la leyenda. Si está en varias
    leyendas (o dos veces en una), no se elige por ti: el error las lista; se
    desempata con `leyenda` (nombre del elemento, ver `list_layout_elements`) o con
    `indice` (el número de esa lista).

    `columna_nueva`: el «Colocar en columna nueva» de ArcMap, que hace que esta entrada
    abra columna. Una capa sustituida (quitar y añadir) nace sin él, y la leyenda pasa
    de dos columnas a una: con esto se recupera sin tocarla a mano.

    Es la alternativa a aplicar un estilo de leyenda de ESRI.style (lo que haría
    arcpy con `updateItem`), que cambia también las fuentes del elemento.
    """
    params: dict = {"capa": capa, "leyenda": leyenda}
    for clave, valor in (("mostrar_nombre", mostrar_nombre),
                         ("mostrar_encabezado", mostrar_encabezado),
                         ("mostrar_etiquetas", mostrar_etiquetas),
                         ("columna_nueva", columna_nueva),
                         ("indice", indice),
                         ("quitar", quitar),
                         ("posicion", posicion),
                         ("despues_de", despues_de)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_legend_item", params)


@mcp.tool()
def set_legend(leyenda: str = None, tamano: float = None, columnas: int = None,
               recuadro: list = None, indice: int = None, fuente: str = None,
               negrita_nombres: bool = None, negrita_etiquetas: bool = None,
               parche: list = None, separaciones: dict = None) -> dict:
    """
    Lo que es de la LEYENDA ENTERA, no de una entrada (para eso, `set_legend_item`):
    tamaño de letra, número de columnas y encajar su marco en un recuadro del layout
    **sin escalar la letra**. Solo se toca lo que se pase; sin nada, devuelve el estado.

    - `tamano`: puntos, para el nombre de capa, el encabezado, las etiquetas y las
      descripciones de TODAS las entradas. La fuente, la negrita y el color se quedan. El
      título de la leyenda no se toca.
    - `columnas`: reparte las entradas en ese número de columnas. Reescribe el «colocar
      en columna nueva» de cada entrada.
    - `recuadro`: `[xmin, ymin, xmax, ymax]` en unidades de página (las de «Tamaño y
      posición»; la respuesta trae `unidades_pagina`). Mide lo que ocupa la leyenda a su
      tamaño real, pone el marco a esa medida anclado arriba a la izquierda del recuadro y
      dice si cabe (`encaje.cabe`, y cuánto sobra o falta en `sobra_ancho`/`sobra_alto`).
      Si no cabe, el marco se coloca igual: se arregla bajando `tamano`, subiendo
      `columnas` o quitando entradas con `set_legend_item(quitar=True)`.

    Formato (lo de la serie del Estudio Ambiental: Arial 8, nombres en negrita, parches de
    16×9 y separaciones compactas):
    - `fuente`: el nombre de la fuente de todas las entradas (`"Arial"`). Una que no esté
      instalada es error.
    - `negrita_nombres`: nombre de capa y encabezado; `negrita_etiquetas`: las etiquetas y
      descripciones de las clases. Cada uno por separado.
    - `parche`: `[ancho, alto]` del parche de cada clase, en puntos (`[16, 9]`); también
      el de las entradas que se añadan después.
    - `separaciones`, en puntos, con los nombres del «Espaciado entre» de ArcMap:
      `titulo`, `entre_entradas`, `entre_columnas`, `entre_clases`, `encabezado`,
      `nombre_capa`, `grupos` y `parche_texto` (`{"entre_entradas": 3, "entre_clases": 2}`).
    Con `recuadro` en la misma llamada, se encaja ya con el formato nuevo. La respuesta trae
    `formato` en `antes` y `ahora` (fuentes, negritas, parches y separaciones leídos).

    Por qué existe: ajustar la altura del marco (lo que hace arcpy con
    `elementHeight`) **escala la letra**, y una leyenda de 9,75 pt acaba a 6 pt. La
    respuesta trae `antes` y `ahora` leídos de la leyenda: tamaños mínimo y máximo,
    columnas, marco y tamaño natural.

    Con varias leyendas en el layout: `leyenda` (el nombre del elemento, ver
    `list_layout_elements`); si no se indica, la del data frame activo, si es una (la
    respuesta lo dice en `elegida`). Si aun así quedan varias (dos «Legend» del mismo data
    frame), el error las lista numeradas y se elige con `indice`.
    """
    params: dict = {}
    for clave, valor in (("leyenda", leyenda), ("tamano", tamano), ("columnas", columnas),
                         ("recuadro", recuadro), ("indice", indice), ("fuente", fuente),
                         ("negrita_nombres", negrita_nombres), ("negrita_etiquetas", negrita_etiquetas),
                         ("parche", parche), ("separaciones", separaciones)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_legend", params)


@mcp.tool()
def set_labels(capa: str, expresion: str = None, tamano: float = None,
               color: str | list = None, halo: float = None,
               color_halo: str | list = None, activar: bool = None,
               clase: str = None, parte_mayor: bool = None,
               sin_duplicados_m: float = None, tamano_minimo_m: float = None,
               repetir_cada_mm: float = None, copiar_de: str = None,
               por_valor: dict = None, where: str = None, fuente: str = None,
               negrita: bool = None, cursiva: bool = None, nunca_quitar: bool = None,
               sobrepasar_entidad: bool = None, buffer: int = None,
               colocacion_linea: str = None) -> dict:
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

    Colocación con **Maplex** (con el motor estándar son error). `0` apaga la opción:
    - `parte_mayor=True`: rotula solo la parte mayor de un polígono multiparte (una
      zonificación troceada salía rotulada 30 veces en una hoja).
    - `sin_duplicados_m`: quita rótulos repetidos a menos de esa distancia, en metros.
    - `tamano_minimo_m`: no rotula entidades menores que eso, en metros. Los dos
      parámetros en metros exigen un mapa en metros.
    - `repetir_cada_mm`: repite el rótulo a lo largo de una línea cada tantos mm de papel.

    - `copiar_de`: copia las clases de etiquetas de otra capa (fuente, halo, colocación,
      expresión) y sobre ellas aplica lo demás que se pase. Es la vía segura para dar
      etiquetas Maplex a una capa nueva. La expresión copiada se comprueba contra los
      campos de esta capa.
    - `por_valor`: `{"campo": "ZONA"}` hace **una clase por valor**, cada una con el color
      de su categoría en la simbología por valores únicos de ese campo (de un relleno se
      toma un tono OSCURO: una etiqueta del color del polígono no se ve), o con
      `"colores": {valor: color}`. Se clonan de la primera clase de la capa y sustituyen a
      todas. No se combina con `clase` ni con `color`; con `where`, cada clase rotula su
      valor Y lo que case con el `where` (se componen con AND).
    - `where`: el filtro SQL de la clase de etiquetas: solo se rotula lo que casa
      (`"\\"ID_MONTE\\" = 'M0001'"`); `""` lo quita. Se prueba contra la capa antes de
      ponerlo: un campo mal escrito dejaría la clase sin rotular y sin error.
    - `fuente`, `negrita`, `cursiva`: la fuente y su estilo. Una fuente que no está
      instalada es error.
    - Más Maplex: `nunca_quitar` (no quitar nunca la etiqueta aunque se solape),
      `sobrepasar_entidad` (puede salirse de la entidad), `buffer` (separación alrededor,
      en % del tamaño) y `colocacion_linea` para líneas: `horizontal`, `recta`, `curva`,
      `perpendicular` (desplazadas de la línea) o con `_centrada` (sobre ella).

    **Modifica las clases que ya tiene la capa; no las borra ni las crea** (salvo
    `copiar_de` y `por_valor`, que las sustituyen por clones). Es lo que
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
                         ("activar", activar), ("clase", clase),
                         ("parte_mayor", parte_mayor), ("sin_duplicados_m", sin_duplicados_m),
                         ("tamano_minimo_m", tamano_minimo_m), ("repetir_cada_mm", repetir_cada_mm),
                         ("copiar_de", copiar_de), ("por_valor", por_valor), ("where", where),
                         ("fuente", fuente), ("negrita", negrita), ("cursiva", cursiva),
                         ("nunca_quitar", nunca_quitar), ("sobrepasar_entidad", sobrepasar_entidad),
                         ("buffer", buffer), ("colocacion_linea", colocacion_linea)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_labels", params)


@mcp.tool()
def set_text_element(texto: str = None, nombre: str = None, buscar: str = None,
                     grupo: str = None, indice: int = None, x: float = None,
                     y: float = None, tamano: float = None, patron: str = None,
                     reemplazo: str = None, todos: bool = None, mayusculas: str = None,
                     region: list = None, centrar_en: list = None, encajar_en: list = None,
                     margen: float = None, tamano_min: float = None) -> dict:
    """
    Cambia el contenido de un elemento de texto del layout (título, fecha, nº de
    expediente...), también si está DENTRO de un grupo (el cajetín agrupado), y/o su
    posición y tamaño de letra. Lo que no se pase no cambia: sin `texto`, el texto se
    queda como estaba.

      - `texto`: el contenido nuevo, entero.
      - `patron` + `reemplazo`: cambia solo la PARTE que casa con la expresión regular
        (sintaxis de .NET; los grupos van como `$1` en el reemplazo, no `\\1`). Excluyente
        con `texto`. Sirve para la fecha, el expediente o una errata sin conocer el texto
        entero. Sin otro selector, los elementos son los que casan con el patrón; con
        varios, error con la lista, salvo `todos=True`, que los cambia todos. Un elegido
        en el que el patrón no casa es error y no se cambia ninguno. El patrón ve también
        las etiquetas de formato (`<BOL>`, `<FNT ...>`). La respuesta trae `antes` y
        `ahora` de cada elemento.
      - `x`, `y`: el punto de anclaje del texto, en unidades de PÁGINA (las de
        «Tamaño y posición» en ArcMap; la respuesta trae `unidades_pagina`). Basta una
        de las dos para moverlo solo en ese eje.
      - `tamano`: tamaño de letra en puntos. La fuente, el color y el halo se quedan.
      - `mayusculas`: `"normal"` (el texto tal cual se escribe), `"mayusculas"`,
        `"minusculas"` o `"versalitas"`. Es una propiedad del SÍMBOLO, no del texto: un
        cajetín escrito en minúsculas que se ve en mayúsculas se arregla con `"normal"`.
      - `centrar_en`: `[xmin, ymin, xmax, ymax]` en unidades de página; centra lo que el
        texto ocupa DIBUJADO (no su punto de anclaje) en ese recuadro. Excluye `x`/`y`.
      - `encajar_en` (+ `margen`, `tamano_min`, por defecto 0 y 4): baja la letra de 0,25
        en 0,25 hasta que el texto quepa en el recuadro menos el margen, sin pasar de
        `tamano_min`, y lo centra en él. La respuesta trae `encaje` (`cabe`, tamaño
        inicial y final, lo que ocupa); si ni al mínimo cabe, lo dice y lo deja así.

    Con cambios de aspecto, la respuesta trae `antes` y `ahora` leídos del elemento
    (con `mayusculas`, y con lo `dibujado` si se centra o encaja).

    Para elegir el elemento, UN selector:

      - `nombre`: el .name del elemento (si está nombrado en ArcMap).
      - `buscar`: su texto ACTUAL (find-and-replace). Coincidencia exacta y, si no,
        por subcadena. Útil cuando los textos del layout no están nombrados (lo
        habitual).
      - `region`: `[xmin, ymin, xmax, ymax]` en unidades de página: el texto cuyo centro
        dibujado cae dentro. Para los textos que se repiten entre planos (el «3» del nº de
        plano) y no tienen nombre. Combinada con `buscar` o `patron`, filtra además por
        posición. Si no cae ninguno, el error lista los textos con su centro.

    Si el selector casa con VARIOS elementos no se elige por ti: el error los lista
    numerados, con su texto y su grupo. Se desempata con `grupo` (nombre del grupo que
    lo contiene; `""` para los sueltos) o, como último recurso, con `indice` (el número
    de esa lista). Hace falta de verdad: un cajetín copiado de otro plano arrastra
    nombre Y texto, y sus elementos son idénticos en todo lo demás.
    """
    params = {"nombre": nombre, "buscar": buscar}
    for clave, valor in (("texto", texto), ("grupo", grupo), ("indice", indice),
                         ("x", x), ("y", y), ("tamano", tamano), ("patron", patron),
                         ("reemplazo", reemplazo), ("todos", todos), ("mayusculas", mayusculas),
                         ("region", region), ("centrar_en", centrar_en), ("encajar_en", encajar_en),
                         ("margen", margen), ("tamano_min", tamano_min)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_text_element", params)


@mcp.tool()
def replace_text(cambios: dict, ambitos: list = None, palabra_completa: bool = True,
                 conservar_mayusculas: bool = True, simular: bool = False) -> dict:
    """
    Buscar y reemplazar por diccionario en TODO el plano a la vez: textos del layout
    (también los de dentro de un grupo, como el cajetín), nombres de capa, etiquetas de las
    clases de la simbología (vectorial y ráster) y encabezados. Para las erratas y tildes de
    una serie: `{"Area": "Área", "Proteccion": "Protección"}` (una serie de montes lo hizo en 386 planos).

    - `cambios`: `{"buscar": "poner"}`. Sin distinguir mayúsculas al buscar; las frases van
      antes que las palabras sueltas que contienen.
    - `palabra_completa` (True): «Area» no cambia dentro de «Areas» ni de «Fareas».
    - `conservar_mayusculas` (True): «AREA» → «ÁREA», «Area» → «Área», «area» → «área». Las
      mayúsculas propias del reemplazo (siglas, nombres) no se bajan nunca:
      `{"Murcia": "Región de Murcia"}` da «Región de Murcia», y `{"Rn2000": "RN2000"}`, «RN2000».
    - `ambitos`: cualquiera de `textos`, `capas`, `clases`, `encabezados` (por defecto todos).
      Las capas son las del data frame activo.
    - Las etiquetas de formato de los textos (`<BOL>`, `<FNT name='Arial'>`) no se tocan.
    - `simular=True`: dice qué cambiaría, sin tocar nada.

    La respuesta trae `cambiados`, `por_ambito` y la lista `cambios` (ámbito, dónde, antes y
    ahora). También es operación de `edit_mxd_lote`.
    """
    params = {"cambios": cambios, "palabra_completa": palabra_completa,
              "conservar_mayusculas": conservar_mayusculas, "simular": simular}
    if ambitos is not None:
        params["ambitos"] = ambitos
    return _client.send("replace_text", params)


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
def set_definition_query(capa: str, query: str = None, anadir: bool = False) -> dict:
    """
    Fija la definition query (filtro SQL) de una capa del data frame activo.
    `query` vacío o None limpia el filtro. Útil para planos temáticos por filtro.
    Devuelve la query anterior y la nueva.

    `anadir=True` no sustituye: añade `query` a la que ya hay con AND, cada una entre
    paréntesis (`(anterior) AND (nueva)`); sin query previa queda solo la nueva. Para
    acotar por monte una capa que ya trae su filtro sin conocerlo (`"MUP" = '{MUP}'` en
    `edit_mxd_lote`). Si la condición ya está en la query, no se repite (`ya_estaba`):
    relanzar la misma receta no la acumula.
    """
    params = {"capa": capa, "query": query}
    if anadir:
        params["anadir"] = True
    return _client.send("set_definition_query", params)


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
    # Por _exportar: la captura también llega con E_PENDING (`dibujando: `) si el mapa
    # sigue dibujando, y se reintenta aquí, fuera de ArcMap (ADR-007).
    resp = _exportar("get_canvas_screenshot", {"modo": modo, "dpi": dpi})
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
              subcapas: list = None, si_existe: str = "anadir") -> dict:
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

    `si_existe`: qué hacer si en el data frame ya hay una capa con ese nombre (el de
    `nombre` o, sin él, el que traiga la fuente), en cualquier grupo. `"anadir"` (por
    defecto) la añade igual, como siempre; `"omitir"` no añade nada y responde
    `omitida: true` con las `existentes` (ruta y fuente); `"error"`, error. En
    `edit_mxd_lote`, `"omitir"` hace que relanzar una receta no duplique la capa.

    **La extensión del data frame no se mueve.** ArcMap reencuadra a veces al añadir o
    quitar capas (en vista de datos y de composición por igual); si lo hace, la vista
    vuelve a donde estaba y la respuesta trae `extension_restaurada: true`. Si lo que
    cambió es el CRS del data frame (se quitó la capa que lo fijaba) no se restaura a
    ciegas: viene `crs_cambiado` y la extensión la fijas tú con `set_extent`.
    """
    params: dict = {"fuente": fuente, "posicion": posicion, "grupo": grupo}
    if si_existe != "anadir":
        params["si_existe"] = si_existe
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
    """
    Quita una capa (o un grupo) del data frame activo, por nombre o ruta `Grupo/Capa`.

    Un borrado no adivina: si el nombre casa con varias capas, falla listando sus rutas
    ANCLADAS, con `/` delante, también cuando una de ellas está en la raíz (en las demás
    herramientas el nombre pelado elige la de la raíz). Repite con una de ellas tal cual:
    `"/Límite"` es la de la raíz y `"/Grupo/Límite"` la del grupo.

    La extensión del data frame no se mueve: si ArcMap reencuadra al quitar la capa, la
    vista vuelve a donde estaba (`extension_restaurada: true`); si cambió el CRS del data
    frame, `crs_cambiado` y la extensión la fijas tú con `set_extent`. Para cambiar el
    orden de las capas no quites y vuelvas a añadir: `move_layer` conserva la simbología.
    """
    return _client.send("remove_layer", {"capa": capa})


@mcp.tool()
def move_layer(capa: str, referencia: str = None, posicion: str = None,
               grupo: str = None, en_leyenda: bool = None) -> dict:
    """
    Recoloca una capa (o un grupo) en la TOC del data frame activo **sin quitarla y
    volverla a añadir**: conserva simbología, etiquetas, lo tocado a mano y sus
    entradas de leyenda.

    La leyenda NO sigue a la TOC: sin más, la entrada se queda donde estaba. Con
    `en_leyenda=True` se recoloca también, en cada leyenda de ese mapa: detrás de la
    entrada de la capa más cercana que quede encima en la TOC (las de un grupo, en
    bloque). La respuesta trae `leyendas` con la `posicion` y el `orden` leídos, o un
    `aviso` si la capa no tiene entrada en alguna.

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
                                     "grupo": grupo, "en_leyenda": en_leyenda}.items() if v is not None})
    return _client.send("move_layer", params)


@mcp.tool()
def rename_layer(capa: str, nombre_nuevo: str) -> dict:
    """
    Cambia el nombre de una capa (o grupo) del data frame activo: el que se ve en la
    TOC y en la leyenda. Refresca las leyendas que dibujan ese mapa.

    Es la vía para corregir el texto de una capa en la leyenda **sin quitarla y volverla
    a añadir**: la entrada de leyenda conserva su posición y sus opciones (p. ej.
    «colocar en columna nueva»), que una capa sustituida pierde. Para el texto de una
    CLASE de la simbología, usa `edit_symbol(etiqueta=...)`.

    - `capa`: nombre o ruta `Grupo/Capa`, como el resto.
    - `nombre_nuevo`: no puede llevar `/`, `\\` ni `#`, que son los separadores de ruta.

    La respuesta trae `nombre_anterior`, `nombre_nuevo` (leído de vuelta) y
    `leyendas_refrescadas`, y avisa si el nombre nuevo ya lo tenía otra capa.
    """
    return _client.send("rename_layer", {"capa": capa, "nombre_nuevo": nombre_nuevo})


@mcp.tool()
def duplicate_layer(capa: str, nombre_nuevo: str, referencia: str = None, posicion: str = None,
                    etiquetas: bool = None, en_leyenda: bool = None) -> dict:
    """
    Copia una capa dentro del mapa: la misma fuente dibujada dos veces (una con relleno y
    otra solo con el rótulo, o con otra definition query), sin volver a añadirla ni
    rehacer su simbología. La copia hereda simbología, etiquetas, definition query,
    transparencia y escalas de visibilidad, y son SUYAS: cambiarlas no toca la original.

    - `nombre_nuevo`: obligatorio y distinto de todas las capas del mapa (dos homónimas
      dan guerra en todas las herramientas). Sin `/`, `\\` ni `#`.
    - `referencia` + `posicion` (`"AFTER"`, debajo, por defecto; o `"BEFORE"`): dónde va,
      en el grupo de la referencia. Sin `referencia`, junto a la original.
    - `etiquetas`: `False` deja la copia sin rótulos (o `True` para encenderlos).
    - `en_leyenda` (False por defecto): si entra en la leyenda del plano. Por defecto no:
      una copia para el rótulo duplicaría la entrada.

    La respuesta trae el `orden` del grupo leído de vuelta y `simbologia_propia`.
    """
    params = {"capa": capa, "nombre_nuevo": nombre_nuevo}
    params.update({k: v for k, v in {"referencia": referencia, "posicion": posicion, "etiquetas": etiquetas,
                                     "en_leyenda": en_leyenda}.items() if v is not None})
    return _client.send("duplicate_layer", params)


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
def set_graduated_symbology(capa: str, campo: str, num_clases: int = None,
                            metodo: str = None,
                            color_desde: list | str = None,
                            color_hasta: list | str = None,
                            tamano: float = None, algoritmo: str = None,
                            cortes: list = None, etiquetas: list | str = None) -> dict:
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
    - `cortes`: en vez de un método, los límites SUPERIORES de cada clase, de menor a
      mayor (los umbrales de un documento técnico). Excluyente con `num_clases` y
      `metodo`. Lo que pase del último corte no se dibuja (la respuesta lo avisa).
    - `etiquetas`: una por clase, o `"auto"`, que las escribe desde los cortes con coma
      decimal: «≤ a», «a - b», …, «> y». La respuesta trae `cortes` y `etiquetas`
      leídos del renderer.
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
    params = {"capa": capa, "campo": campo}
    # Solo lo que se pasa: con `cortes`, un num_clases o metodo enviados por defecto
    # harían imposible saber si alguien los pidió a la vez (el add-in pone los defectos).
    for clave, valor in (("num_clases", num_clases), ("metodo", metodo), ("color_desde", color_desde),
                         ("color_hasta", color_hasta), ("tamano", tamano), ("algoritmo", algoritmo),
                         ("cortes", cortes), ("etiquetas", etiquetas)):
        if valor is not None:
            params[clave] = valor
    return _client.send("set_graduated_symbology", params)


@mcp.tool()
def set_raster_symbology(capa: str, modo: str = "clasificado", num_clases: int = None,
                         color_desde: list = None, color_hasta: list = None,
                         colores: list = None, etiquetas: list | str = None,
                         algoritmo: str = None, valores: list = None,
                         transparentes: list = None, transparencia: int = None,
                         bandas: list = None, cortes: list = None,
                         conservar_colores: bool = None) -> dict:
    """
    Simboliza una capa RÁSTER de la TOC: clasificada, estirada, por valores únicos o
    como composición RGB.

    Es la herramienta para NDVI, FCC, P95, pendientes y demás producto ráster.
    `set_graduated_symbology` NO vale aquí: solo acepta capas de entidades.
    Tampoco vale `execute_arcpy`, que opera sobre una copia y descarta los
    cambios de renderer.

    `modo`:
      - `clasificado` (por defecto): `num_clases` clases (2-32, defecto 5) con rampa de
        color, o las que den `cortes` (límites SUPERIORES de cada clase, de menor a mayor;
        el inferior de la primera es el mínimo del ráster). `cortes` y `num_clases` se
        excluyen; los cortes se devuelven leídos del renderer.
      - `estirado`: rampa continua entre el mínimo y el máximo de la banda 0.
      - `unico`: un color por valor de píxel. **Es el modo de un ráster CATEGÓRICO**
        —máscara de visibilidad, FCC binario, reclasificación, `paletted` de QGIS—,
        donde el clasificado falla con un E_FAIL de COM porque no hay histograma
        que cortar, y donde clasificar sería mentir sobre el dato.
      - `rgb`: composición de color de un ráster multibanda (una ortofoto PNOA de 4
        bandas que entró con una sola banda, en azul). `bandas`: rojo, verde y azul,
        contando desde 1 (por defecto `[1, 2, 3]`). En 8 bits no se estira, como
        corresponde a una ortofoto; la respuesta trae `bandas` leídas y `estiramiento`.

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
      - `conservar_colores=True` (solo `clasificado`): los colores de la clasificación
        que la capa YA tiene, para cambiar solo los cortes (una serie de planos con su
        paleta propia). Exige tantos `cortes` como clases tenga; sin `cortes` ni
        `num_clases`, reclasifica con el mismo número. Excluye `colores` y la rampa. La
        respuesta trae los `colores` leídos de vuelta.

    `etiquetas`: lista de `num_clases` rótulos de leyenda. Cuando los cortes salen
    de una reclasificación, el número del corte no dice nada ("1 – 2") y lo que
    sirve es el significado ("Defoliación fuerte"). Sin esto, cada clase se rotula
    con su rango. En `clasificado`, `"auto"` las escribe desde los cortes con coma
    decimal («≤ a», «a - b», …, «> y») y `"auto<"` igual pero con la primera como
    «< a», la convención de unos planos reales.

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

    La cabecera de la leyenda la fija ArcObjects en el `Update()` (`Value`, o el
    «<VALOR>» de una fuente sustituida). Se cambia después con
    `edit_symbol(capa, encabezado="...")`, comprobado en un JPG exportado, o se oculta
    con `set_legend_item(mostrar_encabezado=False)`.
    """
    params: dict = {"capa": capa, "modo": modo}
    if num_clases is not None:
        params["num_clases"] = num_clases
    if cortes is not None:
        params["cortes"] = cortes
    if conservar_colores is not None:
        params["conservar_colores"] = conservar_colores
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
    if bandas is not None:
        params["bandas"] = bandas
    return _client.send("set_raster_symbology", params)


@mcp.tool()
def set_unique_values_symbology(capa: str, campo: str = None, tamano: float = None,
                                color_desde: list | str = None,
                                color_hasta: list | str = None,
                                algoritmo: str = None, campos: list = None,
                                delimitador: str = None, clases: list = None,
                                resto: str = None) -> dict:
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

    - `campos`: en lugar de `campo`, **hasta 3 campos** (p. ej. `["ANIO_ACT",
      "Actuacion"]`): cada combinación es una categoría, con sus valores unidos por
      `delimitador` (por defecto `", "`, el de ArcMap). Los valores de las clases se
      escriben así: `"2019, Clareo"`.
    - `clases`: las clases explícitas, en este orden:
      `[{"valores": ["0", "99"], "etiqueta": "Sin actuación", "color": "#CCCCCC"}, ...]`.
      Varios `valores` van a la MISMA clase (un símbolo, una entrada de leyenda).
      `etiqueta` (por defecto, el primer valor) y `color` (por defecto, la paleta) son
      opcionales.
    - `resto`: etiqueta de «todos los demás valores», en gris. Sin él, lo que no tiene
      clase NO se dibuja, y la respuesta lo dice en `valores_sin_clase`.

    Con `campos`, `clases` o `resto` la respuesta trae las clases **leídas del
    renderer** (`valor`, `etiqueta`, `valores` agrupados), `valores_sin_clase` y
    `clases_sin_entidades`: arcpy devuelve `classValues` vacío con varios campos y no
    sirve para comprobarlo.
    """
    params: dict = {"capa": capa, "campo": campo}
    for clave, valor in (("tamano", tamano), ("color_desde", color_desde),
                         ("color_hasta", color_hasta), ("algoritmo", algoritmo),
                         ("campos", campos), ("delimitador", delimitador),
                         ("clases", clases), ("resto", resto)):
        if valor is not None:
            params[clave] = valor
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
                         transparencia: float = None, etiqueta: str = None,
                         patron_linea: str | list = None, trama: dict | bool = None,
                         estilo_marcador: str = None) -> dict:
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
    - `patron_linea` (líneas, y el BORDE de los polígonos): `"discontinua"` (guion 9 pt,
      hueco 4,5), `"punteada"` (1,5 / 3), `"guion_punto"`, `"continua"`, o la lista de
      guiones y huecos en puntos (`[9, 4.5]`). Una línea simple pasa a cartográfica: el
      estilo «Dash» de la simple son guiones de píxel y a 2 pt se imprime CONTINUA.
    - `trama` (polígonos): relleno de líneas `{"angulo": 45, "separacion": 5, "color":
      "#RRGGBB", "grosor": 1}` (pt); lo que no se dé sale de lo que había (el color, del
      relleno de esa clase). `true`, la trama por defecto; `false`, vuelve a relleno liso.
      Con trama el color va en `trama.color`, no en `color_relleno`.
    - `estilo_marcador` (puntos): `circulo`, `cuadrado`, `rombo`, `cruz` o `x`.
    Para cambiar un color de una simbología que ya está montada (una categoría de unos
    valores únicos, por ejemplo) sin rehacerla, usa `edit_symbol`.
    """
    return _client.send("set_single_symbology", _params_simbolo(
        capa, color_relleno=color_relleno, color_borde=color_borde, grosor_borde=grosor_borde,
        sin_relleno=sin_relleno, tamano=tamano, transparencia=transparencia, etiqueta=etiqueta,
        patron_linea=patron_linea, trama=trama, estilo_marcador=estilo_marcador))


@mcp.tool()
def edit_symbol(capa: str, categoria: str | int = None, color_relleno: list | str = None,
                color_borde: list | str = None, grosor_borde: float = None,
                sin_relleno: bool = None, tamano: float = None,
                transparencia: float = None, etiqueta: str = None,
                quitar: bool = None, anadir_valores: list = None,
                encabezado: str = None, patron_linea: str | list = None,
                trama: dict | bool = None, estilo_marcador: str = None) -> dict:
    """
    Cambia SOLO lo que pidas de la simbología que la capa YA tiene, sin rehacerla:
    símbolo único, valores únicos o rangos. El resto del símbolo (tipo, patrón, lo que no
    se pida) y las demás categorías se quedan como estaban.

    - `anadir_valores` con `categoria`: mete esos valores en una clase de valores únicos
      que ya existe; se dibujan con su símbolo y salen bajo su etiqueta («todo lo de año 0
      → Sin actuación programada»). Un valor que ya tenga clase es error.
    - `encabezado`: el texto que sale encima de las clases en la TOC y en la leyenda;
      `""` lo deja vacío. (`set_legend_item(mostrar_encabezado=...)` solo lo muestra u
      oculta.)
    - **Capas RÁSTER** clasificadas o por valores únicos: solo `encabezado` (quita el
      «<VALOR>» que deja una fuente sustituida), y `etiqueta` y `color_relleno` de una
      `categoria` (su etiqueta, o su número 1..n). Para reclasificar, `set_raster_symbology`.

    - `categoria`: en valores únicos, el valor o su etiqueta; en rangos, el número de la
      clase (1..n) o su etiqueta. Sin ella se aplica a TODAS las categorías, pero solo lo
      que no borra la clasificación (borde, grosor, tamaño, transparencia): cambiar el
      relleno de todas a la vez es error.
    - `etiqueta`: el texto de esa clase en la leyenda y la TOC (p. ej. para corregir
      tildes), sin rehacer la simbología. Con la capa clasificada exige `categoria`, y
      que case con una sola clase.
    - `quitar=True` con `categoria`: quita esa clase de unos valores únicos y deja los
      colores de las demás COMO ESTABAN (por arcpy, `classValues` sin el valor regenera
      la rampa entera). La respuesta dice qué pasa con sus entidades: van a «todos los
      demás valores» o dejan de dibujarse. No vale para rangos.
    - `patron_linea`, `trama` y `estilo_marcador`, como en `set_single_symbology`, en una
      `categoria` o en todas: «Propuesta» discontinua a 4 pt es
      `edit_symbol(capa, categoria="Propuesta", patron_linea="discontinua", grosor_borde=4)`.
      Una trama sin color conserva el de cada clase, así que vale para todas a la vez.
      **Símbolos de varias capas**: la lectura los marca con `capas_simbolo` (y `capas_borde`
      si lo es el borde de un relleno). Sobre ellos `patron_linea` y `trama` son error (no se
      sabe a qué capa aplicarlos) y `estilo_marcador` los cambia por un marcador simple, que
      pierde las demás capas: para eso, `set_single_symbology`.
    - Los demás parámetros, como en `set_single_symbology`.

    Sin nada que cambiar, devuelve la simbología actual: úsalo así para ver las
    categorías y sus colores antes de tocar. La respuesta trae cada clase leída de vuelta,
    `categorias_cambiadas` y el estado `antes`.
    """
    return _client.send("edit_symbol", _params_simbolo(
        capa, categoria=None if categoria is None else str(categoria), color_relleno=color_relleno, color_borde=color_borde,
        grosor_borde=grosor_borde, sin_relleno=sin_relleno, tamano=tamano,
        transparencia=transparencia, etiqueta=etiqueta, quitar=quitar,
        anadir_valores=None if anadir_valores is None else [str(v) for v in anadir_valores],
        encabezado=encabezado, patron_linea=patron_linea, trama=trama, estilo_marcador=estilo_marcador))


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


@mcp.tool()
def open_mxd(ruta: str, cambios_sin_guardar: str) -> dict:
    """
    Abre otro .mxd en la sesión de ArcMap que tiene el puente, **sin cerrar ArcMap** (el
    puente sigue vivo). Sustituye a cerrar ArcMap, lanzarlo con el .mxd y volver a
    arrancar el puente: ~45 s por documento y alguna caída.

    - `ruta`: ruta ABSOLUTA del .mxd.
    - `cambios_sin_guardar` (obligatorio): `"guardar"` guarda antes el documento abierto
      en su ruta; `"descartar"` tira lo que no esté guardado. Es obligatorio porque ArcMap
      no deja consultar si hay cambios pendientes, y si los hay los pregunta con un diálogo
      que deja el puente colgado esperando un clic. Si no sabes cuál, pregunta al usuario.
    - Un .mxd de una versión de ArcMap posterior no se abre (también saldría un diálogo).

    ArcMap abre el documento en diferido; esta herramienta espera a verlo abierto y
    responde con su ruta, data frames y `marcos_ole` (si tiene, no se edita con arcpy:
    se corrompe). La espera la gobierna ARCMAP_SAVE_TIMEOUT.
    """
    import time
    # UN plazo para todo (guardar, pedir la apertura y esperarla). Con un SAVE_TIMEOUT por
    # llamada, el peor caso eran dos enteros seguidos: ~21 min (revisión del 2026-10-01).
    limite = time.monotonic() + int(SAVE_TIMEOUT)
    r = _client.send("open_mxd", {"ruta": ruta, "cambios_sin_guardar": cambios_sin_guardar},
                     timeout=SAVE_TIMEOUT)
    if not r.get("ok") or "solicitado" not in (r.get("result") or {}):
        # Sin `solicitado` no hay apertura que esperar (un add-in con otro contrato).
        return r
    # ntpath y no os.path: las dos rutas son de Windows. En Linux (el CI) os.path no iguala
    # `C:\b.mxd` con `C:\B.mxd` y el bucle no acababa nunca (2026-10-28).
    import ntpath
    pedido = ntpath.normcase(ntpath.normpath(ruta))
    info = None
    while time.monotonic() < limite:
        # Mientras ArcMap carga el documento, su hilo está ocupado y esto espera en cola:
        # no hace falta un sondeo agresivo.
        info = _client.send("get_arcmap_info", timeout=max(1, int(limite - time.monotonic())))
        mxd = ((info or {}).get("result") or {}).get("mxd")
        if info.get("ok") and mxd and ntpath.normcase(ntpath.normpath(mxd)) == pedido:
            break
        time.sleep(0.5)
    else:
        return {"ok": False, "error": (
            "ArcMap no ha llegado a abrir %s en %s s: el documento activo sigue siendo %s. Mira si hay"
            " un diálogo abierto en ArcMap." % (ruta, int(SAVE_TIMEOUT),
                                              ((info or {}).get("result") or {}).get("mxd")))}
    res = dict(r.get("result") or {})
    res.pop("solicitado", None)
    res["abierto"] = info["result"]["mxd"]
    for clave in ("data_frames", "df_activo", "vista_activa"):
        res[clave] = info["result"].get(clave)
    n_ole, _err = _mxd_marcos_ole(ruta)
    if n_ole:
        res["marcos_ole"] = n_ole
        res["aviso_marcos_ole"] = ("tiene %d marco(s) OLE: edítalo con las herramientas del puente y"
                                   " save_mxd, nunca con arcpy de fuera (se corrompe)." % n_ole)
    return {"ok": True, "result": res}


def _tras_guardar(r, clave_ruta, verificar):
    """Reabre lo guardado si se pidió, o si la predicción del add-in encontró capas que
    se van a perder. `rotas_en_memoria` solo sirve para esa comparación y no se devuelve."""
    if not r.get("ok"):
        return r
    res = r.get("result") or {}
    memoria = res.pop("rotas_en_memoria", None) or []
    if verificar or (verificar is None and res.get("capas_perderan_ruta")):
        if res.get("marcos_ole"):
            # Reabrir con el arcpy de fuera un .mxd con marcos OLE se cuelga mientras
            # ArcMap está abierto (medido 2026-09-26; ver MarcosOle.cs): se avisa y no se lanza.
            res["verificacion"] = {
                "reabierto": False,
                "motivo": ("el documento tiene %d marco(s) OLE (objetos incrustados, p. ej. de Word) y,"
                           " con ArcMap abierto, el arcpy de fuera se queda colgado al cargarlo; ábrelo"
                           " con ArcMap cerrado para comprobar las rutas, o quita esos marcos"
                           % len(res["marcos_ole"])),
            }
        else:
            res["verificacion"] = _verificar_guardado(res.get(clave_ruta), memoria)
    return r


def _verificar_guardado(ruta, rotas_en_memoria, timeout=60):
    """Reabre `ruta` con el auditor standalone y devuelve las capas rotas en disco que no
    lo estaban en memoria. Las rutas de grupo se comparan con `/` (el auditor da
    `Grupo\\Capa`, el add-in `Grupo/Capa`).

    60 s y no 180: reabrir un .mxd sano cuesta ~9 s (medido con ArcMap abierto), y cuando
    no termina no es por lento, es que se ha quedado esperando a algo. El 2026-10-01 un
    plano de un expediente real SIN marcos OLE se quedó así los 180 s enteros, y otro del mismo
    proyecto colgó un export_mxd_lote 10 min; la causa está sin identificar (no es la
    licencia ni el OLE). Esperar más no la arregla."""
    import subprocess

    py27 = _python27_arcgis()
    auditor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auditor_mxd.py")
    if not ruta or py27 is None or not os.path.isfile(auditor):
        return {"reabierto": False, "motivo": "falta el Python 2.7 de ArcGIS (ARCMAP_PYTHON27) o el auditor"}
    try:
        proc = subprocess.run([py27, auditor, ruta], capture_output=True, timeout=timeout,
                              **SIN_VENTANA)
        bruto = proc.stdout.decode("utf-8", "replace").strip()
        datos = json.loads(bruto) if bruto else {"ok": False, "error": "sin salida"}
    except subprocess.TimeoutExpired:
        return {"reabierto": False,
                "motivo": ("el documento SÍ se ha guardado; lo que no ha terminado es la comprobación:"
                           " el arcpy de fuera no abrió el .mxd en %s s (lo normal son ~10 s). No es"
                           " lentitud, se ha quedado esperando a algo: mira `describe_mxd` (marcos OLE)"
                           " o comprueba las rutas reabriendo el documento con ArcMap cerrado" % timeout)}
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
    de un sitio y no lo indicas, la llamada falla listando los candidatos. Como
    `remove_layer`, no adivina entre homónimas: hace falta la ruta anclada con `/`
    delante (`"/Límite"`, `"/Grupo/Límite"`), y `data_frame` si se repite entre marcos.

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
def replace_data_source(capa: str, fuente: str) -> dict:
    """
    Apunta una capa del data frame activo a OTRO dataset, conservando su simbología,
    etiquetas y definition query: el `replaceDataSource` de arcpy. Para cuando cambia el
    fichero, no solo la carpeta: el ráster regional por el de cada monte
    (`N_pies_ha_20m.tif` → `M0001_N_pies_ha_20m_Anexo1.tif`), o `Masas_V0` por
    `Rodales_V1`. Si solo cambia el principio de la ruta, `repair_data_source`.

    - `fuente`: ruta completa del dataset nuevo (`.shp`, `.tif`, `Carpeta.gdb\\clase`...). Se
      abre antes de tocar nada: si no existe, error y la capa queda como estaba.
    - Ráster con ráster y entidades con entidades, con la misma geometría; si no, error.
    - En un ráster CLASIFICADO se conservan los cortes, las etiquetas, los colores y el
      encabezado (al reapuntar, ArcMap los regeneraba: «<VALOR>» y la primera clase en
      blanco). Si el rango del dato nuevo es otro, la respuesta lo recuerda en `avisos`.
    - Avisa si la definition query usa campos que el dataset nuevo no tiene.
    - Con homónimas no adivina: hace falta la ruta anclada con `/` delante, como en
      `remove_layer`.
    - También es operación de `edit_mxd_lote` (con `variables_por_mxd`, un fichero por
      monte en una sola llamada).

    La respuesta trae `fuente_anterior`, `fuente_nueva` (leída de vuelta), y si la capa
    estaba o queda rota.
    """
    return _client.send("replace_data_source", {"capa": capa, "fuente": fuente})


@mcp.tool()
def save_layer_as_lyr(capa: str, ruta: str, sobrescribir: bool = False) -> dict:
    """
    Guarda una capa del data frame activo como fichero `.lyr`, con su simbología,
    etiquetas, definition query y transparencia; si es un GRUPO, con todo su árbol. Es el
    «Guardar como archivo de capa» de ArcMap. Para sacar la simbología buena de un plano y
    llevarla a los demás de la serie con `apply_symbology_from_layer` o `add_layer`.

    - `ruta`: ruta ABSOLUTA del `.lyr`, en una carpeta que exista. Si ya existe, error salvo
      con `sobrescribir=True`.
    - Se escribe a un temporal y se REABRE antes de dejarlo en `ruta`: si lo leído (nombre,
      tipo, fuente, renderer, etiquetas, query, transparencia y las hijas de un grupo) no
      coincide con la capa, error y no se deja nada. La respuesta trae `leido`.
    - Las rutas de los datos se guardan absolutas.
    - También es operación de `edit_mxd_lote` (`ruta` con `{VARIABLE}` para un .lyr por
      plano); el lote guarda además el .mxd, como siempre.
    """
    return _client.send("save_layer_as_lyr", {"capa": capa, "ruta": ruta,
                                              "sobrescribir": bool(sobrescribir)})


def _origen_con_ole(origen):
    """Motivo para no abrir el .mxd de origen de copy_layout_elements, o None. Abrirlo desde
    fuera con ArcMap abierto y un marco OLE en su layout se queda colgado (ver describe_mxd)."""
    if not origen or not os.path.isfile(origen):
        return None  # el add-in da el error de ruta, con su texto
    n, error = _mxd_marcos_ole(origen)
    if n:
        return ("el origen %s tiene %d marco(s) OLE: abrirlo desde fuera se queda colgado. Copia los"
                " elementos con ese plano abierto en ArcMap, o quita antes los marcos." % (origen, n))
    return None


@mcp.tool()
def copy_layout_elements(origen: str, nombres: list = None, region: list = None,
                         tipos: list = None, quitar: list = None) -> dict:
    """
    Copia elementos del layout de OTRO .mxd a este, en la misma posición: los logos del
    cajetín de un plano bueno al resto de la serie. Busca también DENTRO de los grupos (el
    cajetín agrupado) y la imagen incrustada viaja con sus bytes.

    - `origen`: ruta ABSOLUTA del .mxd del que copiar (se abre solo para leer).
    - Qué copiar, uno o los dos: `nombres` (los nombres de los elementos en el origen) y
      `region` (`[xmin, ymin, xmax, ymax]` en unidades de página: los que tengan el centro
      dentro; para el logo sin nombre). Si no casa nada, el error lista lo que hay, con su
      nombre, grupo y caja.
    - `tipos`: `imagen` (por defecto), `texto`, `grafico` (líneas, rectángulos...). Los
      marcos de datos, leyendas y demás de un mapa no se copian.
    - `quitar`: nombres de elementos SUELTOS de este documento que se borran antes (los
      logos viejos). Si alguno no está, error y no se toca nada.
    - Si la página del origen mide distinto, se pega igual y se avisa.
    - Un origen con marcos OLE se rechaza sin abrirlo.
    - También es operación de `edit_mxd_lote`.

    La respuesta trae `copiados` (nombre, tipo, grupo de donde salió y `caja` leída ya
    pegada) y `quitados`.
    """
    error = _origen_con_ole(origen)
    if error:
        return {"ok": False, "error": error}
    params = {"origen": origen}
    for clave, valor in (("nombres", nombres), ("region", region), ("tipos", tipos), ("quitar", quitar)):
        if valor is not None:
            params[clave] = valor
    return _client.send("copy_layout_elements", params)


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
def set_frame_clip(data_frame: str = None, capa: str = None, where: str = None,
                   excluir: list = None, quitar: bool = None) -> dict:
    """
    Recorta un data frame a la forma de una capa de polígonos (el «Clip to shape» de
    ArcMap: el mapa de un monte recortado a su perímetro), con capas que se libran del
    recorte y se ven enteras, o quita el recorte. arcpy no lo expone.

    - `data_frame`: por nombre; sin él, el activo.
    - `capa` (+ `where` opcional): la forma es la UNIÓN de sus entidades. Se respeta la
      definition query de la capa, y `where` filtra además (un monte de la capa de montes).
    - `excluir`: capas que NO se recortan (la base cartográfica, el propio límite). Sin
      `capa`, cambia solo las excluidas de un recorte que ya existe.
    - `quitar=True`: sin recorte (también quita las excluidas). Va solo.

    Sin `capa`, `excluir` ni `quitar`, devuelve el estado: `recorte` (None | Shape |
    MapExtent | PageIndex), `excluidas` y `area_forma`. Con cambios, trae `antes` y el estado
    leído de vuelta. Compruébalo en el plano exportado: el recorte solo se VE en el export.
    """
    params = {k: v for k, v in {"data_frame": data_frame, "capa": capa, "where": where,
                                "excluir": excluir, "quitar": quitar}.items() if v is not None}
    return _client.send("set_frame_clip", params)


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


def _arcmap_en_marcha():
    """¿Hay algún ArcMap.exe corriendo en este equipo? Si no se puede saber, se asume que sí:
    es lo prudente para no abrir un .mxd con marcos OLE (ver _CLSID_MARCO_OLE)."""
    import subprocess
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ArcMap.exe", "/NH"],
                             capture_output=True, timeout=15,
                             **SIN_VENTANA).stdout.decode("utf-8", "replace")
        return "arcmap.exe" in out.lower()
    except (OSError, subprocess.SubprocessError):
        return True


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

    Devuelve `version_declarada`, `version_arcmap_local`, `veredicto` y `motivo`, y
    `marcos_ole`: los objetos incrustados del layout, que con ArcMap abierto dejan
    colgado a cualquier arcpy de fuera que abra el documento.

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

    salida = {
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
    n_ole, _err_ole = _mxd_marcos_ole(ruta_abs)
    if n_ole is not None:
        salida["marcos_ole"] = n_ole
    if n_ole:
        salida["aviso_marcos_ole"] = (
            "tiene %d marco(s) OLE (objetos incrustados, p. ej. de Word). Con ArcMap abierto, "
            "abrirlo con el arcpy de fuera (execute_arcpy, audit_folder, un script) se queda "
            "colgado; en ArcMap abre bien, y con ArcMap cerrado también." % n_ole)
    return salida


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


def _estado_exportacion(mxd, margen=60):
    """¿El JPG/PDF del mismo nombre junto al .mxd es posterior a su último guardado?

    Solo fechas de fichero, sin abrir nada. `margen` porque guardar y exportar van
    seguidos y el export puede llevar unos segundos de ventaja. Con JPG y PDF, manda el
    más nuevo. Visto el 2026-10-06: un plano editado a las 10:47 se quedó sin re-exportar
    y el recuento de JPG cuadraba por casualidad."""
    base = os.path.splitext(mxd)[0]
    exportes = []
    for ext in (".jpg", ".pdf"):
        try:
            exportes.append((os.path.getmtime(base + ext), base + ext))
        except OSError:
            pass
    if not exportes:
        return {"exportacion": "sin_exportar"}
    fecha, fichero = max(exportes)
    estado = "al_dia" if fecha >= os.path.getmtime(mxd) - margen else "desfasada"
    return {"exportacion": estado, "exportacion_fichero": os.path.basename(fichero)}


@mcp.tool()
def audit_folder(ruta: str, con_capas: bool = True, timeout_por_documento: int = 90,
                 max_documentos: int = 200, recursivo: bool = False,
                 forzar_con_arcmap_abierto: bool = False, detalle: bool = False) -> dict:
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

    **¿ESTÁ EXPORTADO?** Cada documento trae `exportacion`: `al_dia`, `desfasada`
    (el .mxd se guardó después de su JPG/PDF) o `sin_exportar`, mirando el fichero del
    mismo nombre junto al .mxd; el `resumen` los cuenta. Solo fechas, sin abrir nada:
    sale también con `con_capas=False`. Úsalo antes de entregar una serie: un plano
    editado y no re-exportado no se nota contando ficheros.

    **INVENTARIO RICO (`detalle=True`)**: además, cada documento se lee con
    `arcmap-mcp-disco.exe` (los mismos handlers que la sesión viva): por capa del data
    frame activo, su simbología clase a clase (valores, etiquetas, colores, patrones) y su
    etiquetado (expresión, fuente, tamaño, Maplex); los textos del layout con su posición, y
    cada leyenda con sus entradas y su formato. En la respuesta va un `detalle` por
    documento con los recuentos (capas, clasificadas, etiquetadas, textos, leyendas...); el
    inventario entero, en el fichero de `detalle_completo` (en un plano de 160 capas son
    ~130 KB). **Con `detalle=True` la lista `capas` de cada documento también va solo al
    fichero** (en la respuesta queda `capas_en_detalle` con cuántas son, y todos los
    recuentos): con seis planos reales la respuesta pasaba de 100 KB y el cliente no la
    mostraba. Cuesta ~3 s por plano. Para comparar un plano con su plantilla, `compare_mxd`.
    Un documento con marcos OLE y ArcMap abierto no se lee (se quedaría colgado).

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
    # Solo se pregunta si algún documento tiene marcos OLE: es lo único que depende de que
    # ArcMap esté abierto (desde la 2.15.0 la pasada 2 no mira a ArcMap para nada más).
    arcmap_abierto = None
    inventarios = {}
    aviso_detalle = None
    # Lo pedido, no lo que se pudo: sin el ejecutable no hay inventario, pero la lista de capas
    # va igual al fichero (es lo que pesa en la respuesta).
    pidio_detalle = bool(detalle)
    if detalle and _disco_exe() is None:
        detalle = False
        aviso_detalle = ("detalle=True necesita arcmap-mcp-disco.exe, que viaja en el .esriaddin de la 2.17.0:"
                         " instálalo (o define ARCMAP_DISCO_EXE). Se devuelve la auditoría sin inventario.")

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
        fila.update(_estado_exportacion(doc))
        por_estado = resumen.setdefault("exportacion", {})
        por_estado[fila["exportacion"]] = por_estado.get(fila["exportacion"], 0) + 1

        n_ole, _err_ole = _mxd_marcos_ole(doc)
        if n_ole:
            fila["marcos_ole"] = n_ole
            resumen["con_marcos_ole"] = resumen.get("con_marcos_ole", 0) + 1
            if (con_capas or detalle) and arcmap_abierto is None:
                arcmap_abierto = _arcmap_en_marcha()

        if detalle:
            if n_ole and arcmap_abierto:
                fila["detalle"] = {"sin_leer": "tiene %d marco(s) OLE y ArcMap está abierto: leerlo desde fuera se"
                                               " queda colgado. Repite con ArcMap cerrado." % n_ole}
            else:
                inv = _inventario_mxd(doc, timeout_por_documento)
                if inv.get("ok"):
                    inventarios[doc] = inv["inventario"]
                    fila["detalle"] = _resumen_inventario(inv["inventario"])
                else:
                    fila["detalle"] = {"error": inv.get("error")}

        if con_capas and cortada:
            fila["sin_abrir"] = True
            resumen["sin_abrir"] += 1
        elif con_capas and n_ole and arcmap_abierto:
            # Abrirlo con el arcpy de fuera colgaría hasta el timeout y dejaría a ArcMap sin
            # poder cerrarse (medido 2026-09-26; ver _CLSID_MARCO_OLE). Con ArcMap cerrado
            # se abre sin problema, así que no se da por perdido: se dice cómo auditarlo.
            fila["sin_abrir"] = True
            fila["error_al_abrir"] = ("no se abre: tiene %d marco(s) OLE y ArcMap está abierto, y en "
                                      "ese caso el arcpy de fuera se queda colgado al cargarlo. Repite "
                                      "con ArcMap cerrado." % n_ole)
            resumen["sin_abrir"] += 1
        elif con_capas:
            timeout_previo = resumen["timeout"]
            try:
                proc = subprocess.run(
                    [py27, auditor, doc],
                    capture_output=True, timeout=timeout_por_documento, **SIN_VENTANA)
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
    if aviso_detalle:
        salida["aviso_detalle"] = aviso_detalle
    if pidio_detalle:
        # La lista `capas` de cada documento va SOLO al fichero: con 6 planos reales la
        # respuesta pesaba 106 KB y el cliente no la mostraba (2026-10-10). Los recuentos se
        # quedan. Si el fichero no se puede escribir, la lista se queda en la respuesta.
        fichero = _guardar_detalle_lote({"carpeta": carpeta, "documentos": resultados,
                                         "inventarios": inventarios}, prefijo="audit_folder")
        if fichero:
            salida["detalle_completo"] = fichero
            salida["documentos"] = [_fila_sin_capas(f) for f in resultados]
        else:
            salida["aviso_detalle_completo"] = ("No se pudo escribir el fichero de detalle (%TEMP%\\arcmap-mcp-lotes):"
                                                " la lista de capas y el inventario no se guardan; las capas van en"
                                                " la respuesta.")
    return salida


def _fila_sin_capas(fila):
    """La fila de audit_folder sin su lista `capas` (que queda en el fichero de detalle) y con
    cuántas eran, para que se sepa que no se ha perdido nada."""
    if "capas" not in fila:
        return fila
    f = dict(fila)
    capas = f.pop("capas")
    f["capas_en_detalle"] = len(capas) if isinstance(capas, list) else 0
    return f


def _cpu_segundos(proc):
    """Segundos de CPU (usuario + núcleo) que lleva el proceso, o None si no se puede leer."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes
    c, e, k, u = (wintypes.FILETIME() for _ in range(4))
    try:
        ok = ctypes.windll.kernel32.GetProcessTimes(wintypes.HANDLE(int(proc._handle)),
                                                    ctypes.byref(c), ctypes.byref(e),
                                                    ctypes.byref(k), ctypes.byref(u))
    except (AttributeError, OSError, ValueError):
        return None
    if not ok:
        return None
    total = 0
    for ft in (k, u):
        total += (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return total / 1e7


def _ejecutar_vigilando(args, timeout, corte_sin_cpu, intervalo=5.0):
    """Lanza `args` y devuelve (returncode, stdout, stderr, motivo_corte).

    Además del tiempo límite, corta el proceso si su CPU no se mueve en `corte_sin_cpu`
    segundos (0 = sin ese corte). Un arcpy colgado no consume nada: el 2026-10-01 un
    export_mxd_lote se quedó 10 min al 0 % con un plano de un expediente real que, repetido en cinco
    condiciones distintas, abre en 10-12 s. Sin esto, cada documento colgado cuesta el
    timeout entero. Si hay que cortar, motivo_corte dice por qué; si no, es None."""
    import subprocess
    import threading
    import time

    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **SIN_VENTANA)
    salida = {"out": b"", "err": b""}

    def leer(flujo, clave):
        salida[clave] = flujo.read()

    hilos = [threading.Thread(target=leer, args=(proc.stdout, "out"), daemon=True),
             threading.Thread(target=leer, args=(proc.stderr, "err"), daemon=True)]
    for h in hilos:
        h.start()
    inicio = time.monotonic()
    cpu_previa, quieto_desde = None, inicio
    motivo = None
    while proc.poll() is None:
        ahora = time.monotonic()
        if ahora - inicio >= timeout:
            motivo = "TIMEOUT tras %s s" % timeout
            break
        if corte_sin_cpu:
            cpu = _cpu_segundos(proc)
            if cpu is not None:
                if cpu_previa is None or cpu - cpu_previa > 0.05:
                    cpu_previa, quieto_desde = cpu, ahora
                elif ahora - quieto_desde >= corte_sin_cpu:
                    motivo = ("SIN PROGRESO: la CPU del proceso no se movió en %d s (llevaba %.1f s de"
                              " CPU en total)" % (corte_sin_cpu, cpu))
                    if cpu < 1:
                        # `import arcpy` solo ya gasta ~6 s de CPU: con menos de 1 el
                        # hijo se paró al arrancar, no en el documento ni en la red.
                        motivo += (". Con menos de 1 s de CPU el proceso no llegó ni a importar"
                                   " arcpy: el bloqueo es al arrancar, no del documento")
                    break
        try:
            # wait y no sleep: un hijo que acaba en 1 s devolvía a los 5 (6 min de más en un
            # lote de 150 planos, medido en la revisión del 2026-10-01).
            proc.wait(timeout=min(intervalo, max(0.05, timeout - (ahora - inicio))))
        except subprocess.TimeoutExpired:
            pass
    if motivo:
        proc.kill()
    proc.wait()
    for h in hilos:
        h.join(5)
    return proc.returncode, salida["out"], salida["err"], motivo


@mcp.tool()
def export_mxd_lote(mxds: list, formato: str = "jpg", dpi: int = 230,
                    carpeta_salida: str = None, sobrescribir: bool = True,
                    timeout_por_documento: int = 600, corte_sin_cpu: int = 120,
                    si_capas_rotas: str = "exportar") -> dict:
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
    - `corte_sin_cpu`: segundos con la CPU del proceso quieta tras los que se da el
      documento por colgado y se corta (120; 0 = no cortar así). Un arcpy colgado no
      consume nada (visto: 10 min al 0 %), y sin esto cada uno cuesta el timeout entero.
      Súbelo si los planos esperan mucho a fuentes de red lentas: esa espera también
      tiene la CPU quieta.

    Presupuesto: ~6 s de `import arcpy` por documento más lo que cueste dibujarlo;
    planos de ~100 capas con WMS salen a ~25 s cada uno. Un lote de 150 son una hora
    larga: pártelo si el cliente MCP corta antes.

    - `si_capas_rotas`: cada fila trae `capas_rotas`, las capas rotas que SALEN en
      el plano (la capa, sus grupos y su marco en la hoja; el criterio de
      `audit_folder`): una rota dentro de un grupo apagado no cuenta. Con "exportar"
      (por defecto) se exporta igual y se avisa; con "fallar", ese documento no se
      exporta y el plano que ya había se conserva. Un export con «ok» y una capa
      rota visible es un plano sin esa capa: mira `capas_rotas` antes de entregar.

    Excepción a «abierto o cerrado»: un .mxd con **marcos OLE** (tablas de Word en el
    layout) NO se lanza con ArcMap abierto, porque el arcpy de fuera se cuelga al
    cargarlo. Sale como fallido con `marcos_ole` y el motivo; repítelo con ArcMap cerrado.

    Devuelve, POR DOCUMENTO, el .mxd, la salida, los bytes y si pisó algo. Mira los
    bytes: un plano que pesa la mitad que sus vecinos es el primer síntoma de capas
    rotas o de una vista que no es la que crees.
    """
    import shutil
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
    try:
        corte_sin_cpu = int(corte_sin_cpu or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "corte_sin_cpu debe ser un entero (segundos; 0 = sin corte)."}
    if corte_sin_cpu < 0:
        return {"ok": False, "error": "corte_sin_cpu no puede ser negativo (0 = sin corte)."}
    if isinstance(mxds, str):
        mxds = [mxds]
    if not mxds:
        return {"ok": False, "error": "mxds está vacía: no hay nada que exportar."}
    if carpeta_salida and not os.path.isdir(carpeta_salida):
        return {"ok": False, "error": "carpeta_salida no existe: %s" % carpeta_salida}
    si_capas_rotas = (si_capas_rotas or "exportar").lower()
    if si_capas_rotas not in ("exportar", "fallar"):
        return {"ok": False, "error": "si_capas_rotas debe ser 'exportar' o 'fallar' (recibido: %r)."
                                      % si_capas_rotas}

    py27 = _python27_arcgis()
    exportador = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exportar_mxd.py")
    if py27 is None or not os.path.isfile(exportador):
        return {"ok": False, "error": "No se encuentra el Python 2.7 de ArcGIS (define "
                                      "ARCMAP_PYTHON27) o src/exportar_mxd.py."}

    resultados = []
    resumen = {"exportados": 0, "fallidos": 0, "saltados": 0}
    aviso_cpu = None
    if corte_sin_cpu and os.path.basename(py27).lower() != "python.exe":
        # Un lanzador (.cmd, el python.exe de un venv...) arranca el intérprete como hijo y
        # se queda quieto: su CPU no se mueve y el corte mataría TODOS los documentos.
        aviso_cpu = ("corte_sin_cpu desactivado: ARCMAP_PYTHON27 apunta a %s, que no es un python.exe,"
                     " y la CPU que se mediría sería la de un lanzador quieto" % py27)
        corte_sin_cpu = 0
    arcmap_abierto = None        # se consulta una vez, y solo si algún .mxd tiene OLE
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
            n_ole, _err_ole = _mxd_marcos_ole(ruta)
            if n_ole:
                if arcmap_abierto is None:
                    arcmap_abierto = _arcmap_en_marcha()
                if arcmap_abierto:
                    # Con ArcMap abierto el arcpy de fuera se cuelga al cargar un marco OLE
                    # y arrastra a los procesos que abren otros .mxd a la vez (2026-10-06:
                    # cinco matados a mano). Ni se lanza: fallido, porque el plano no sale.
                    fila["marcos_ole"] = n_ole
                    fila["error"] = ("no se exporta: tiene %d marco(s) OLE y ArcMap está abierto, y "
                                     "en ese caso el arcpy de fuera se queda colgado al cargarlo. "
                                     "Repite con ArcMap cerrado." % n_ole)
                    resumen["fallidos"] += 1
                    resultados.append(fila)
                    continue

            trabajo = os.path.join(trabajos, "trabajo_%d.json" % i)
            with open(trabajo, "w", encoding="utf-8") as f:
                json.dump({"mxd": ruta, "salida": destino, "formato": formato, "dpi": dpi,
                           "si_capas_rotas": si_capas_rotas}, f, ensure_ascii=False)
            inicio = time.time()
            try:
                _rc, out, err, corte = _ejecutar_vigilando([py27, exportador, trabajo],
                                                           timeout_por_documento, corte_sin_cpu)
                if corte:
                    datos = {"ok": False, "error": "%s: se corta este documento y el lote sigue. Causa"
                                                   " habitual: fuentes de red que no responden, o un"
                                                   " .mxd que deja colgado al arcpy de fuera."
                                                   % corte}
                else:
                    bruto_salida = out.decode("utf-8", "replace").strip()
                    datos = json.loads(bruto_salida) if bruto_salida else {
                        "ok": False, "error": "sin salida; stderr: %s"
                        % err.decode("utf-8", "replace")[-500:]}
            except (json.JSONDecodeError, OSError) as exc:
                datos = {"ok": False, "error": "no se pudo exportar: %s" % exc}

            # Antes de la comprobación del fichero, que puede reescribir `datos`.
            for clave in ("capas_rotas", "capas_rotas_sin_decidir", "capas_rotas_error"):
                if datos.get(clave):
                    fila[clave] = datos[clave]
            if fila.get("capas_rotas"):
                resumen["con_capas_rotas"] = resumen.get("con_capas_rotas", 0) + 1

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

    salida = {"ok": resumen["fallidos"] == 0, "formato": formato, "dpi": dpi,
              "total": len(mxds), "resumen": resumen, "documentos": resultados}
    if resumen.get("con_capas_rotas"):
        # Arriba y no solo en la fila: el «ok» del lote no dice nada de esto, y fue
        # justo ese «ok» lo que dejó salir cuatro planos sin cantones.
        salida["aviso"] = ("%d documento(s) con capas rotas que salen en el plano: mira "
                           "`capas_rotas` en cada fila antes de entregar."
                           % resumen["con_capas_rotas"])
    if aviso_cpu:
        salida["aviso_corte_sin_cpu"] = aviso_cpu
    return salida


# --------------------------------------------------------------------------- #
# Modo disco (ADR-010): edit_mxd_lote.
#
# Las operaciones son herramientas vivas, con sus nombres y parámetros, que ejecuta
# arcmap-mcp-disco.exe con los MISMOS handlers del add-in sobre un .mxd del disco. El
# ejecutable viaja dentro del .esriaddin: se saca de ahí a una caché propia, así nunca
# va desfasado del add-in instalado.
# --------------------------------------------------------------------------- #

OPERACIONES_LOTE = (
    "set_legend", "set_legend_item", "set_text_element", "set_labels", "edit_symbol",
    "set_single_symbology", "set_unique_values_symbology", "set_graduated_symbology",
    "set_raster_symbology", "apply_symbology_from_layer", "add_layer", "add_group", "remove_layer",
    "rename_layer", "duplicate_layer", "move_layer", "set_layer_visibility", "set_definition_query",
    "set_scale", "set_extent", "set_frame_clip", "replace_data_source", "replace_text",
    "save_layer_as_lyr", "copy_layout_elements",
)

_ADDIN_ID = "{51f4ce63-6bcf-49b2-ae3a-ba2c79ea3e1a}"


def _disco_exe():
    """Ruta de arcmap-mcp-disco.exe, o None. Por orden: ARCMAP_DISCO_EXE; el que trae el
    .esriaddin instalado (extraído a %LOCALAPPDATA%\\arcmap-mcp\\disco\\<firma>, porque quien
    instala con doble clic solo tiene el zip); la compilación del repo."""
    import tempfile
    import zipfile
    forzado = os.environ.get("ARCMAP_DISCO_EXE")
    if forzado and os.path.isfile(forzado):
        return forzado
    base = os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "ArcGIS", "AddIns")
    paquetes = []
    try:
        for version in sorted(os.listdir(base), reverse=True):
            carpeta = os.path.join(base, version, _ADDIN_ID)
            if os.path.isdir(carpeta):
                paquetes += [os.path.join(carpeta, f) for f in os.listdir(carpeta) if f.lower().endswith(".esriaddin")]
    except OSError:
        pass
    for paquete in paquetes:
        st = os.stat(paquete)
        cache = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "arcmap-mcp", "disco",
                             "%d-%d" % (st.st_size, int(st.st_mtime)))
        exe = os.path.join(cache, "arcmap-mcp-disco.exe")
        if os.path.isfile(exe):
            return exe
        try:
            with zipfile.ZipFile(paquete) as z:
                nombres = [n for n in z.namelist() if n.replace("\\", "/").lower().startswith("install/")]
                if not any(n.lower().endswith("arcmap-mcp-disco.exe") for n in nombres):
                    continue                      # add-in anterior al modo disco
                os.makedirs(cache, exist_ok=True)
                for n in nombres:
                    destino = os.path.join(cache, os.path.basename(n.replace("\\", "/")))
                    if os.path.basename(destino):
                        with z.open(n) as src, open(destino + ".tmp", "wb") as dst:
                            dst.write(src.read())
                        os.replace(destino + ".tmp", destino)
            return exe
        except (OSError, zipfile.BadZipFile):
            continue
    repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addin",
                        "ArcmapMcp.Disco", "bin", "Release", "arcmap-mcp-disco.exe")
    return repo if os.path.isfile(repo) else None


def _mxd_abierto_en_arcmap():
    """(ruta normalizada del documento abierto con el puente o None, títulos de ventana de
    ArcMap). Sin puente no se sabe la ruta, y se compara por el nombre del título."""
    abierto = None
    r = _client.send("get_arcmap_info", timeout=15)
    if r.get("ok") and (r.get("result") or {}).get("mxd"):
        abierto = os.path.normcase(os.path.abspath(r["result"]["mxd"]))
    titulos = []
    try:
        out = subprocess.run(["tasklist", "/V", "/FO", "CSV", "/NH", "/FI", "IMAGENAME eq ArcMap.exe"],
                             capture_output=True, timeout=15, **SIN_VENTANA).stdout.decode("mbcs", "replace")
        for linea in out.splitlines():
            campos = [c.strip('"') for c in linea.split('","')]
            if campos and campos[0].lower().startswith("arcmap.exe"):
                titulos.append(campos[-1])
    except (OSError, subprocess.SubprocessError):
        pass
    return abierto, titulos


@mcp.tool()
def edit_mxd_lote(mxds: list = None, operaciones: list = None, bak: bool = True,
                  timeout_por_documento: int = 600, corte_sin_cpu: int = 120,
                  en_paralelo: int = 1, detalle: bool = False,
                  operaciones_por_mxd: dict = None, variables_por_mxd: dict = None,
                  plantilla: str = None, plantilla_por_mxd: dict = None,
                  sobrescribir: bool = False) -> dict:
    """
    Aplica una lista de operaciones a varios .mxd del disco y los guarda, sin abrirlos
    en ArcMap (ArcMap puede estar cerrado o con otro documento). Es la herramienta para
    retocar una serie de planos: leyenda a 8 pt, una entrada fuera, etiquetas, un texto del
    cajetín... en 29 planos con una llamada, en vez de abrir y guardar cada uno.

    Las series reales llevan variaciones por plano (cada monte con su código en el filtro,
    sus clases, sus capas), y hay dos formas de darlas:
    - `variables_por_mxd`: `{ruta: {"MUP": "M0001", "MONTE": "Sierra Ejemplo"}}`. En cualquier
      texto de las operaciones, `{MUP}` se cambia por el valor de ESE plano:
      `{"tool": "set_definition_query", "capa": "Rodales", "query": "\\"ID_MONTE\\" = '{MUP}'"}`.
      Solo se sustituyen los nombres declarados; una llave que no sea una variable se queda tal
      cual. Si un plano no define una variable que sus operaciones usan, no se lanza nada.
    - `operaciones_por_mxd`: `{ruta: [ops]}`, operaciones propias de cada plano, que van
      DESPUÉS de las comunes de `operaciones`. Con ella, `mxds` y `operaciones` son opcionales:
      sin `mxds`, los planos son las claves.

    Para CREAR los planos a partir de una plantilla (una hoja por plano de la serie):
    - `plantilla`: un .mxd del que salen todos, o `plantilla_por_mxd`: `{destino: origen}`.
      Cada destino de `mxds` se crea abriendo su plantilla, aplicando las operaciones en
      memoria y guardando con «Guardar como» en el destino, que reescribe las rutas relativas
      para la carpeta nueva (copiar el fichero a mano las rompe). Sin operaciones, solo copia.
      Si una operación falla, el destino no se crea.
    - `sobrescribir` (False): un destino que ya existe es error salvo con True (y entonces
      se le hace `.bak`). La plantilla con marcos OLE se rechaza, como cualquier origen.

    - `operaciones`: lista de herramientas con sus parámetros, como en vivo:
      `[{"tool": "set_legend", "tamano": 8}, {"tool": "set_legend_item", "capa": "Badén",
      "quitar": true}]`. Admitidas: set_legend, set_legend_item, set_text_element, set_labels,
      edit_symbol, set_single_symbology, set_unique_values_symbology, set_graduated_symbology,
      set_raster_symbology, apply_symbology_from_layer, add_layer, add_group, remove_layer,
      rename_layer, duplicate_layer, move_layer, set_layer_visibility, set_definition_query,
      set_scale, set_extent (`coords` o `capa`), set_frame_clip, replace_data_source,
      replace_text, save_layer_as_lyr y copy_layout_elements. Actúan sobre el data frame
      que el .mxd guardó como activo.
    - **Todo o nada por documento**: si falla una operación, ese .mxd no se guarda (y se dice
      cuál falló); los demás siguen.
    - `bak` (True): copia `<plano>.mxd.bak` antes de guardar.
    - Se guarda conservando el ajuste de rutas relativas del documento y **se verifica
      reabriendo**: las mismas lecturas (TOC, leyendas y lo tocado de cada capa) antes de
      guardar y al reabrir. `verificado: false` y `diferencias` si algo no sobrevivió.
    - Cada .mxd en su propio proceso, con `timeout_por_documento` y `corte_sin_cpu`, como
      `export_mxd_lote`. `en_paralelo` (1-4): cuántos a la vez. Verificar reabriendo cuesta
      lo que una apertura: en un plano pesado, un documento suelto va más lento que
      `open_mxd` + operaciones + `save_mxd`; en una serie, en paralelo, no.
    - La respuesta va RESUMIDA: por documento, `verificado`, `diferencias` y lo que devolvió
      cada operación, con los valores largos (el `orden` de una leyenda, las lecturas de la
      verificación) recortados. Sin recortar pesaba ~60.000 caracteres por plano de 150 capas
      y el cliente no la mostraba. Lo completo se escribe siempre en el fichero de
      `detalle_completo`; `detalle=True` lo devuelve también en la respuesta (solo para pocos
      planos).

    Se NIEGA, sin tocarlos: los .mxd con **marcos OLE** (guardarlos desde fuera los corrompe;
    para esos, `open_mxd` + las herramientas + `save_mxd`) y **el documento abierto en
    ArcMap** (el siguiente save_mxd pisaría lo hecho). No exporta: después, `export_mxd_lote`.
    """
    import shutil
    import tempfile
    import time
    if operaciones_por_mxd is not None and not isinstance(operaciones_por_mxd, dict):
        return {"ok": False, "error": "'operaciones_por_mxd' es un diccionario {ruta del .mxd: [operaciones]}."}
    if variables_por_mxd is not None and not isinstance(variables_por_mxd, dict):
        return {"ok": False, "error": "'variables_por_mxd' es un diccionario {ruta del .mxd: {\"NOMBRE\": \"valor\"}}."}
    if plantilla_por_mxd is not None and not isinstance(plantilla_por_mxd, dict):
        return {"ok": False, "error": "'plantilla_por_mxd' es un diccionario {destino .mxd: plantilla .mxd}."}
    if plantilla and plantilla_por_mxd:
        return {"ok": False, "error": "'plantilla' y 'plantilla_por_mxd' se excluyen."}
    if mxds is None and operaciones_por_mxd:
        mxds = list(operaciones_por_mxd)
    if mxds is None and plantilla_por_mxd:
        mxds = list(plantilla_por_mxd)
    if not isinstance(mxds, list) or not mxds:
        return {"ok": False, "error": "'mxds' debe ser una lista no vacía de rutas absolutas a .mxd."}
    origenes = {}
    if plantilla:
        origenes = {_clave_mxd(m): os.path.abspath(os.path.expandvars(str(plantilla))) for m in mxds}
    elif plantilla_por_mxd:
        origenes = {_clave_mxd(k): os.path.abspath(os.path.expandvars(str(v))) for k, v in plantilla_por_mxd.items()}
        sobran = [k for k in origenes if k not in {_clave_mxd(m) for m in mxds}]
        if sobran:
            return {"ok": False, "error": "plantilla_por_mxd trae destinos que no están en 'mxds': %s." % ", ".join(sobran)}
    operaciones = operaciones or []
    if not isinstance(operaciones, list):
        return {"ok": False, "error": "'operaciones' debe ser una lista de {\"tool\": ..., parámetros}."}
    if not operaciones and not operaciones_por_mxd and not origenes:
        return {"ok": False, "error": "'operaciones' debe ser una lista no vacía de {\"tool\": ..., parámetros}"
                                      " (o da 'operaciones_por_mxd', o una 'plantilla' para solo copiar)."}
    try:
        planes = _planes_lote(mxds, operaciones, operaciones_por_mxd or {}, variables_por_mxd or {},
                              sin_ops_vale=set(origenes))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    exe = _disco_exe()
    if exe is None:
        return {"ok": False, "error": "No se encuentra arcmap-mcp-disco.exe: el add-in instalado es anterior al modo"
                                      " disco. Instala el .esriaddin de esta versión (o define ARCMAP_DISCO_EXE)."}

    if not isinstance(en_paralelo, int) or not 1 <= en_paralelo <= 4:
        return {"ok": False, "error": "'en_paralelo' va de 1 a 4 (recibido: %r)." % (en_paralelo,)}

    abierto, titulos = (None, [])
    if _arcmap_en_marcha():
        abierto, titulos = _mxd_abierto_en_arcmap()
    resultados = []
    resumen = {"guardados": 0, "fallidos": 0, "no_verificados": 0}
    trabajos = tempfile.mkdtemp(prefix="arcmap-mcp-disco_")

    def una(fila, trabajo):
        inicio = time.time()
        try:
            _rc, out, err, corte = _ejecutar_vigilando([exe, trabajo], timeout_por_documento, corte_sin_cpu)
            if corte:
                datos = {"ok": False, "error": "%s: se corta este documento y el lote sigue. Si se cortó a mitad"
                                               " del guardado, revisa el .mxd y su .bak." % corte}
            else:
                texto = out.decode("utf-8", "replace").strip()
                datos = json.loads(texto.splitlines()[-1]) if texto else {
                    "ok": False, "error": "sin salida; stderr: %s" % err.decode("utf-8", "replace")[-500:]}
        except (json.JSONDecodeError, OSError) as exc:
            datos = {"ok": False, "error": "no se pudo ejecutar el modo disco: %s" % exc}
        fila["segundos"] = round(time.time() - inicio, 1)
        for clave in ("guardado", "creado", "bak", "operaciones", "verificacion", "error"):
            if clave in datos:
                fila[clave] = datos[clave]
        return datos

    try:
        # 1. Las negativas, sin lanzar nada.
        pendientes = []
        for i, bruto in enumerate(mxds):
            ruta = os.path.abspath(os.path.expandvars(str(bruto)))
            fila = {"mxd": ruta}
            resultados.append(fila)
            origen = origenes.get(_clave_mxd(ruta))
            if origen is not None:
                fila["origen"] = origen
                if not ruta.lower().endswith(".mxd"):
                    fila["error"] = "el destino tiene que acabar en .mxd"
                    resumen["fallidos"] += 1
                    continue
                if not (os.path.isfile(origen) and origen.lower().endswith(".mxd")):
                    fila["error"] = "la plantilla no existe o no es un .mxd: %s" % origen
                    resumen["fallidos"] += 1
                    continue
                if os.path.normcase(origen) == os.path.normcase(ruta):
                    fila["error"] = "la plantilla y el destino son el mismo fichero"
                    resumen["fallidos"] += 1
                    continue
                if os.path.exists(ruta) and not sobrescribir:
                    fila["error"] = "el destino ya existe; pasa sobrescribir=True para pisarlo (se le hace .bak)"
                    resumen["fallidos"] += 1
                    continue
            elif not (os.path.isfile(ruta) and ruta.lower().endswith(".mxd")):
                fila["error"] = "no existe o no es un .mxd"
                resumen["fallidos"] += 1
                continue
            n_ole, _err = _mxd_marcos_ole(origen or ruta)
            if n_ole:
                fila["marcos_ole"] = n_ole
                fila["error"] = ("no se toca: tiene %d marco(s) OLE, y guardarlo desde fuera de ArcMap lo corrompe."
                                 " Para este plano: open_mxd + las herramientas + save_mxd." % n_ole)
                resumen["fallidos"] += 1
                continue
            nombre = os.path.basename(ruta).lower()
            if os.path.normcase(ruta) == abierto or (abierto is None and any(
                    t.lower().startswith(nombre) or t.lower().startswith(nombre[:-4] + " ") for t in titulos)):
                fila["error"] = ("no se toca: es el documento abierto en ArcMap, y el siguiente save_mxd pisaría lo"
                                 " hecho. Usa las herramientas vivas sobre él, o ciérralo antes.")
                resumen["fallidos"] += 1
                continue
            trabajo = os.path.join(trabajos, "trabajo_%d.json" % i)
            with open(trabajo, "w", encoding="utf-8") as f:
                trabajo_json = {"mxd": ruta, "operaciones": planes[i], "bak": bool(bak)}
                if origen is not None:
                    trabajo_json.update({"origen": origen, "sobrescribir": bool(sobrescribir)})
                json.dump(trabajo_json, f, ensure_ascii=False)
            pendientes.append((fila, trabajo))

        # 2. Un proceso por documento; con en_paralelo > 1, varios a la vez (son independientes).
        if en_paralelo == 1:
            todos = [una(f, t) for f, t in pendientes]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=en_paralelo) as pool:
                todos = list(pool.map(lambda ft: una(*ft), pendientes))
        for datos in todos:
            if datos.get("ok"):
                resumen["guardados"] += 1
            elif datos.get("guardado"):
                resumen["no_verificados"] += 1
            else:
                resumen["fallidos"] += 1
    finally:
        shutil.rmtree(trabajos, ignore_errors=True)
    r = {"ok": resumen["fallidos"] == 0 and resumen["no_verificados"] == 0, "total": len(mxds),
         "resumen": resumen, "documentos": resultados}
    fichero = _guardar_detalle_lote(r)
    if fichero:
        r["detalle_completo"] = fichero
    if not detalle:
        r["documentos"] = [_resumir_fila_lote(f) for f in resultados]
    return r


_VARIABLE_LOTE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _clave_mxd(ruta):
    return os.path.normcase(os.path.abspath(os.path.expandvars(str(ruta))))


def _planes_lote(mxds, comunes, por_mxd, variables, sin_ops_vale=frozenset()):
    """La lista de operaciones de cada plano, en el orden de `mxds`: las comunes y luego las
    suyas, con sus variables sustituidas. Todo se valida ANTES de lanzar nada: una clave que
    no es de ningún plano, una operación que no es del lote, o una variable que el plano usa y
    no define, es error para todo el lote (un lote a medias por una errata es peor)."""
    claves = [_clave_mxd(m) for m in mxds]
    propias = {}
    for k, ops in por_mxd.items():
        if not isinstance(ops, list):
            raise ValueError("operaciones_por_mxd[%r] debe ser una lista de operaciones." % (k,))
        propias[_clave_mxd(k)] = ops
    vars_ = {}
    for k, v in variables.items():
        if not isinstance(v, dict):
            raise ValueError("variables_por_mxd[%r] debe ser un diccionario {\"NOMBRE\": \"valor\"}." % (k,))
        vars_[_clave_mxd(k)] = {str(n): v[n] for n in v}
    for nombre, dicc in (("operaciones_por_mxd", propias), ("variables_por_mxd", vars_)):
        sobran = [k for k in dicc if k not in claves]
        if sobran:
            raise ValueError("%s trae planos que no están en 'mxds': %s." % (nombre, ", ".join(sobran)))
    declaradas = set()
    for v in vars_.values():
        declaradas.update(v)

    def sustituir(valor, mapa, donde, faltan):
        if isinstance(valor, str):
            def cambio(m):
                n = m.group(1)
                if n not in declaradas:
                    return m.group(0)
                if n not in mapa:
                    faltan.add(n)
                    return m.group(0)
                return str(mapa[n])
            return _VARIABLE_LOTE.sub(cambio, valor)
        if isinstance(valor, list):
            return [sustituir(x, mapa, donde, faltan) for x in valor]
        if isinstance(valor, dict):
            return {k: sustituir(x, mapa, donde, faltan) for k, x in valor.items()}
        return valor

    planes = []
    for mxd, clave in zip(mxds, claves):
        ops = list(comunes) + list(propias.get(clave, []))
        if not ops and clave not in sin_ops_vale:
            raise ValueError("El plano %s no tiene ninguna operación (ni comunes ni propias)." % mxd)
        for i, op in enumerate(ops):
            if not isinstance(op, dict) or op.get("tool") not in OPERACIONES_LOTE:
                raise ValueError("operaciones[%d] de %s: %r no es una operación del lote. Admitidas: %s."
                                 % (i, mxd, op.get("tool") if isinstance(op, dict) else op,
                                    ", ".join(OPERACIONES_LOTE)))
        faltan = set()
        ops = sustituir(ops, vars_.get(clave, {}), mxd, faltan)
        if faltan:
            raise ValueError("El plano %s usa %s y no las define en variables_por_mxd."
                             % (mxd, ", ".join("{%s}" % n for n in sorted(faltan))))
        for op in ops:
            if op.get("tool") == "copy_layout_elements":
                error = _origen_con_ole(op.get("origen"))
                if error:
                    raise ValueError("%s: %s" % (mxd, error))
        planes.append(ops)
    return planes


_LOTE_VALOR_MAX = 400


def _recortar_valor_lote(valor):
    """Un valor de la respuesta de una operación, o su aviso si es largo."""
    texto = json.dumps(valor, ensure_ascii=False)
    if len(texto) <= _LOTE_VALOR_MAX:
        return valor
    return "(%d caracteres, recortado: está en detalle_completo)" % len(texto)


def _resumir_fila_lote(fila):
    """La fila de un documento sin lo que hace inmanejable la respuesta de un lote: las
    lecturas de la verificación (la TOC y las leyendas enteras, dos veces) y los valores
    largos de cada operación. Se quedan los veredictos y las diferencias, que es lo que
    decide."""
    f = dict(fila)
    v = f.get("verificacion")
    if isinstance(v, dict):
        v = dict(v)
        lecturas = v.pop("lecturas", None)
        if isinstance(lecturas, list):
            v["lecturas"] = [l.get("lectura") for l in lecturas if isinstance(l, dict)]
        f["verificacion"] = v
    ops = f.get("operaciones")
    if isinstance(ops, list):
        cortas = []
        for op in ops:
            if not isinstance(op, dict):
                cortas.append(op)
                continue
            c = {k: op[k] for k in op if k != "result"}
            if isinstance(op.get("result"), dict):
                c["result"] = {k: _recortar_valor_lote(x) for k, x in op["result"].items()}
            elif "result" in op:
                c["result"] = _recortar_valor_lote(op["result"])
            cortas.append(c)
        f["operaciones"] = cortas
    return f


def _guardar_detalle_lote(r, prefijo="edit_mxd_lote"):
    """Escribe la respuesta completa de un lote en %TEMP%\\arcmap-mcp-lotes y devuelve la
    ruta, o None si no se pudo (no es motivo para que el lote falle)."""
    import tempfile
    import time
    try:
        carpeta = os.path.join(tempfile.gettempdir(), "arcmap-mcp-lotes")
        os.makedirs(carpeta, exist_ok=True)
        ruta = os.path.join(carpeta, "%s_%s_%03d_%d.json" % (
            prefijo, time.strftime("%Y%m%d-%H%M%S"), int(time.time() * 1000) % 1000, os.getpid()))
        with open(ruta + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1)
        os.replace(ruta + ".tmp", ruta)
        return ruta
    except (OSError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Inventario rico y comparación con la plantilla (hueco 14 de la auditoría de scripts,
# 2026-10-10). Una serie de montes se auditó con scripts propios porque audit_folder solo daba capas,
# fuentes y rotas. El inventario lo hace arcmap-mcp-disco.exe en modo lectura, con los
# mismos handlers que la sesión viva: comparar un plano con su plantilla solo es justo si
# los dos se leen con el mismo lector.
# --------------------------------------------------------------------------- #

def _inventario_mxd(ruta, timeout=600, corte_sin_cpu=120):
    """Inventario rico de un .mxd (sin tocarlo), o {"ok": False, "error": ...}."""
    import shutil
    import tempfile
    exe = _disco_exe()
    if exe is None:
        return {"ok": False, "error": "No se encuentra arcmap-mcp-disco.exe: instala el .esriaddin de esta versión"
                                      " (o define ARCMAP_DISCO_EXE)."}
    carpeta = tempfile.mkdtemp(prefix="arcmap-mcp-inventario_")
    try:
        trabajo = os.path.join(carpeta, "trabajo.json")
        with open(trabajo, "w", encoding="utf-8") as f:
            json.dump({"mxd": ruta, "inventario": True}, f, ensure_ascii=False)
        _rc, out, err, corte = _ejecutar_vigilando([exe, trabajo], timeout, corte_sin_cpu)
        if corte:
            return {"ok": False, "error": "%s al leer %s" % (corte, ruta)}
        texto = out.decode("utf-8", "replace").strip()
        if not texto:
            return {"ok": False, "error": "sin salida; stderr: %s" % err.decode("utf-8", "replace")[-500:]}
        return json.loads(texto.splitlines()[-1])
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": "no se pudo leer %s: %s" % (ruta, exc)}
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


def _resumen_inventario(inv):
    """Lo que cabe en la respuesta de audit_folder por documento; lo demás va al fichero."""
    capas = inv.get("capas") or []
    datos = [c for c in capas if not c.get("grupo")]
    return {
        "capas": len(datos),
        "grupos": len(capas) - len(datos),
        "clasificadas": sum(1 for c in datos if (c.get("simbologia") or {}).get("renderer") not in (None, "simple")),
        "etiquetadas": sum(1 for c in datos if c.get("etiquetas")),
        "con_query": sum(1 for c in datos if c.get("definition_query")),
        "textos": len(inv.get("textos") or []),
        "leyendas": len(inv.get("leyendas") or []),
        "entradas_de_leyenda": sum(len(l.get("entradas") or []) for l in inv.get("leyendas") or []),
        "lecturas_con_error": sum(1 for c in datos for k in ("simbologia", "etiquetado")
                                  if "error" in (c.get(k) or {})),
    }


_AMBITOS_COMPARAR = ("general", "capas", "simbologia", "etiquetado", "textos", "leyendas")


def _distinto(a, b):
    return json.dumps(a, sort_keys=True, ensure_ascii=False) != json.dumps(b, sort_keys=True, ensure_ascii=False)


def _diferencias_dict(a, b, ignorar=()):
    """{campo: [plano, referencia]} de los campos que difieren entre dos dicts."""
    a, b = a or {}, b or {}
    return {k: [a.get(k), b.get(k)] for k in sorted(set(a) | set(b))
            if k not in ignorar and _distinto(a.get(k), b.get(k))}


def _clave_clase(c, i):
    if c.get("categoria") is not None:
        return str(c.get("categoria"))
    return str(c.get("etiqueta") or "#%d" % (i + 1))


def _error_de_lectura(p, r):
    """Si alguna de las dos lecturas falló: None si fallan igual, o la diferencia."""
    if not _distinto(p.get("error"), r.get("error")):
        return None
    return {"lectura": [p.get("error"), r.get("error")]}


def _comparar_simbologia(p, r):
    """Cambios entre dos lecturas de edit_symbol: renderer, encabezado y clase a clase."""
    p, r = p or {}, r or {}
    if "error" in p or "error" in r:
        return _error_de_lectura(p, r)
    cambios = _diferencias_dict(p, r, ignorar=("capa", "clases", "aviso", "antes"))
    cp = {_clave_clase(c, i): c for i, c in enumerate(p.get("clases") or [])}
    cr = {_clave_clase(c, i): c for i, c in enumerate(r.get("clases") or [])}
    if set(cp) - set(cr):
        cambios["clases_solo_en_plano"] = sorted(set(cp) - set(cr))
    if set(cr) - set(cp):
        cambios["clases_solo_en_referencia"] = sorted(set(cr) - set(cp))
    por_clase = {k: _diferencias_dict(cp[k], cr[k]) for k in sorted(set(cp) & set(cr))}
    por_clase = {k: v for k, v in por_clase.items() if v}
    if por_clase:
        cambios["clases"] = por_clase
    return cambios or None


def _comparar_etiquetado(p, r):
    p, r = p or {}, r or {}
    if "error" in p or "error" in r:
        return _error_de_lectura(p, r)
    cambios = _diferencias_dict(p, r, ignorar=("capa", "clases", "clase_creada", "aviso"))
    cp = {str(c.get("clase")): c for c in p.get("clases") or []}
    cr = {str(c.get("clase")): c for c in r.get("clases") or []}
    if set(cp) ^ set(cr):
        cambios["clases"] = {"solo_en_plano": sorted(set(cp) - set(cr)),
                             "solo_en_referencia": sorted(set(cr) - set(cp))}
    por_clase = {k: _diferencias_dict(cp[k], cr[k]) for k in sorted(set(cp) & set(cr))}
    por_clase = {k: v for k, v in por_clase.items() if v}
    if por_clase:
        cambios["por_clase"] = por_clase
    return cambios or None


def _emparejar_textos(tp, tr, tolerancia):
    """Parejas (i, j) de textos del layout: por nombre si lo tienen y es único (con su grupo) en
    los dos; si no, el más cercano por el centro dibujado, a menos de `tolerancia`. Devuelve
    (parejas, sin_pareja_en_plano, sin_pareja_en_referencia)."""
    parejas, libres_p, libres_r = [], list(range(len(tp))), list(range(len(tr)))

    def nombre_unico(lista, t):
        return bool(t.get("nombre")) and sum(1 for x in lista if x.get("nombre") == t.get("nombre")
                                             and x.get("grupo") == t.get("grupo")) == 1

    for i in list(libres_p):
        t = tp[i]
        if not nombre_unico(tp, t):
            continue
        j = next((j for j in libres_r if tr[j].get("nombre") == t.get("nombre")
                  and tr[j].get("grupo") == t.get("grupo") and nombre_unico(tr, tr[j])), None)
        if j is not None:
            parejas.append((i, j))
            libres_p.remove(i)
            libres_r.remove(j)
    candidatas = []
    for i in libres_p:
        for j in libres_r:
            a, b = tp[i].get("centro"), tr[j].get("centro")
            if a and b:
                d = ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
                if d <= tolerancia:
                    candidatas.append((d, i, j))
    for _d, i, j in sorted(candidatas):
        if i in libres_p and j in libres_r:
            parejas.append((i, j))
            libres_p.remove(i)
            libres_r.remove(j)
    return parejas, libres_p, libres_r


def _comparar_leyenda(a, b):
    c = {}
    ea = [e.get("capa") for e in a.get("entradas") or []]
    eb = [e.get("capa") for e in b.get("entradas") or []]
    if set(ea) - set(eb):
        c["entradas_solo_en_plano"] = [e for e in ea if e not in eb]
    if set(eb) - set(ea):
        c["entradas_solo_en_referencia"] = [e for e in eb if e not in ea]
    if [e for e in ea if e in eb] != [e for e in eb if e in ea]:
        c["orden"] = [ea, eb]
    interruptores = {}
    for ent_a in a.get("entradas") or []:
        ent_b = next((e for e in b.get("entradas") or [] if e.get("capa") == ent_a.get("capa")), None)
        if ent_b is not None:
            x = _diferencias_dict(ent_a, ent_b, ignorar=("capa",))
            if x:
                interruptores[str(ent_a.get("capa"))] = x
    if interruptores:
        c["interruptores"] = interruptores
    f = _diferencias_dict(a.get("formato"), b.get("formato"))
    if f:
        c["formato"] = f
    c.update(_diferencias_dict({k: a.get(k) for k in ("leyenda", "data_frame", "caja")},
                               {k: b.get(k) for k in ("leyenda", "data_frame", "caja")}))
    return c


def _comparar_inventarios(plano, ref, ambitos=None, tolerancia_posicion=0.5):
    """Diferencias entre dos inventarios (los de arcmap-mcp-disco.exe en modo inventario),
    por ámbito; cada cambio como [plano, referencia]. Vacío = iguales en lo comparado."""
    ambitos = list(ambitos or _AMBITOS_COMPARAR)
    dif = {}
    if "general" in ambitos:
        g = _diferencias_dict({k: plano.get(k) for k in ("data_frame", "escala")},
                              {k: ref.get(k) for k in ("data_frame", "escala")})
        if g:
            dif["general"] = g
    cp = {c["ruta"]: c for c in plano.get("capas") or []}
    cr = {c["ruta"]: c for c in ref.get("capas") or []}
    comunes = [k for k in cp if k in cr]
    if "capas" in ambitos:
        d = {}
        if set(cp) - set(cr):
            d["solo_en_plano"] = [k for k in cp if k not in cr]
        if set(cr) - set(cp):
            d["solo_en_referencia"] = [k for k in cr if k not in cp]
        if comunes != [k for k in cr if k in cp]:
            d["orden_distinto"] = True
        cambios = {}
        for k in comunes:
            c = _diferencias_dict(cp[k], cr[k], ignorar=("ruta", "simbologia", "etiquetado"))
            if c:
                cambios[k] = c
        if cambios:
            d["cambios"] = cambios
        if d:
            dif["capas"] = d
    for ambito, comparar in (("simbologia", _comparar_simbologia), ("etiquetado", _comparar_etiquetado)):
        if ambito not in ambitos:
            continue
        d = {}
        for k in comunes:
            if ambito in cp[k] or ambito in cr[k]:
                c = comparar(cp[k].get(ambito), cr[k].get(ambito))
                if c:
                    d[k] = c
        if d:
            dif[ambito] = d
    if "textos" in ambitos:
        tp, tr = plano.get("textos") or [], ref.get("textos") or []
        parejas, solo_p, solo_r = _emparejar_textos(tp, tr, tolerancia_posicion)
        d = {}
        cambiados = []
        for i, j in parejas:
            c = _diferencias_dict(tp[i], tr[j], ignorar=("centro",))
            a, b = tp[i].get("centro"), tr[j].get("centro")
            if a and b and max(abs(a[0] - b[0]), abs(a[1] - b[1])) > 0.05:
                c["centro"] = [a, b]
            if c:
                cambiados.append({"en_referencia": tr[j].get("texto"), "centro": b, "cambios": c})
        if cambiados:
            d["cambiados"] = cambiados
        campos = ("nombre", "grupo", "texto", "centro")
        if solo_p:
            d["solo_en_plano"] = [{k: tp[i].get(k) for k in campos} for i in solo_p]
        if solo_r:
            d["solo_en_referencia"] = [{k: tr[j].get(k) for k in campos} for j in solo_r]
        if d:
            dif["textos"] = d
    if "leyendas" in ambitos:
        lp, lr = plano.get("leyendas") or [], ref.get("leyendas") or []
        d = {}
        if len(lp) != len(lr):
            d["numero"] = [len(lp), len(lr)]
        for n, (a, b) in enumerate(zip(lp, lr), 1):
            c = _comparar_leyenda(a, b)
            if c:
                d["leyenda_%d" % n] = c
        if d:
            dif["leyendas"] = d
    return dif


def _contar_diferencias(dif):
    """Cuántos ELEMENTOS difieren: cada campo general, capa de más o de menos o cambiada, capa
    con simbología o etiquetado distinto, texto cambiado o sin pareja y leyenda distinta (y el
    orden de las capas, si cambia). No cuenta los campos de dentro de cada uno."""
    n = len(dif.get("general") or {})
    capas = dif.get("capas") or {}
    n += len(capas.get("solo_en_plano") or []) + len(capas.get("solo_en_referencia") or [])
    n += len(capas.get("cambios") or {}) + (1 if capas.get("orden_distinto") else 0)
    n += len(dif.get("simbologia") or {}) + len(dif.get("etiquetado") or {})
    textos = dif.get("textos") or {}
    n += sum(len(textos.get(k) or []) for k in ("cambiados", "solo_en_plano", "solo_en_referencia"))
    leyendas = dif.get("leyendas") or {}
    n += sum(1 for k in leyendas if k.startswith("leyenda_")) + (1 if "numero" in leyendas else 0)
    return n


@mcp.tool()
def compare_mxd(mxd: str, referencia: str, ambitos: list = None,
                tolerancia_posicion: float = 0.5, timeout_por_documento: int = 600) -> dict:
    """
    Compara un .mxd con su PLANTILLA (u otro plano de la serie) y dice en qué se separa: lo que
    se hacía a ojo antes de exportar una serie. No necesita ArcMap ni toca ninguno de los dos:
    los lee `arcmap-mcp-disco.exe` con los mismos handlers que la sesión viva.

    - `ambitos` (por defecto todos): `general` (data frame activo y escala), `capas` (las del
      data frame activo por su ruta de grupo: de más, de menos, orden y, por capa, visible,
      fuente, definition query, etiquetas encendidas y si está rota), `simbologia`
      (renderer, encabezado y clase a clase: valor, etiqueta, colores, grosor, patrón,
      trama), `etiquetado` (motor, expresión, fuente, tamaño, Maplex...), `textos` (los del
      layout, también los agrupados del cajetín) y `leyendas` (entradas, orden,
      interruptores, formato y caja).
    - Los textos se emparejan por nombre si lo tienen (único) y, si no, por la posición de
      lo dibujado, a menos de `tolerancia_posicion` (unidades de página). En una serie los
      del cajetín CAMBIAN a propósito (título, nº de plano): salen en `textos.cambiados` con
      el texto de la referencia y lo que cambia.
    - Un .mxd con marcos OLE y ArcMap abierto no se lee (se quedaría colgado).

    Devuelve `iguales`, `num_diferencias` y `diferencias` por ámbito, cada cambio como
    `[plano, referencia]`. La lectura completa de los dos va a `detalle_completo`.
    """
    ambitos = list(ambitos) if ambitos else list(_AMBITOS_COMPARAR)
    malos = [a for a in ambitos if a not in _AMBITOS_COMPARAR]
    if malos:
        return {"ok": False, "error": "ámbitos desconocidos: %s. Admitidos: %s." % (
            ", ".join(map(str, malos)), ", ".join(_AMBITOS_COMPARAR))}
    rutas = []
    for nombre, valor in (("mxd", mxd), ("referencia", referencia)):
        ruta = os.path.abspath(os.path.expandvars(str(valor or "")))
        if not (os.path.isfile(ruta) and ruta.lower().endswith(".mxd")):
            return {"ok": False, "error": "%s no existe o no es un .mxd: %s" % (nombre, ruta)}
        n_ole, _e = _mxd_marcos_ole(ruta)
        if n_ole and _arcmap_en_marcha():
            return {"ok": False, "error": "%s tiene %d marco(s) OLE y ArcMap está abierto: leerlo desde fuera se"
                                          " queda colgado. Repite con ArcMap cerrado." % (ruta, n_ole)}
        rutas.append(ruta)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        lecturas = list(pool.map(lambda r: _inventario_mxd(r, timeout_por_documento), rutas))
    for ruta, lec in zip(rutas, lecturas):
        if not lec.get("ok"):
            return {"ok": False, "error": "no se pudo leer %s: %s" % (ruta, lec.get("error"))}
    dif = _comparar_inventarios(lecturas[0]["inventario"], lecturas[1]["inventario"], ambitos,
                                float(tolerancia_posicion))
    r = {"ok": True, "mxd": rutas[0], "referencia": rutas[1], "iguales": not dif,
         "num_diferencias": _contar_diferencias(dif), "diferencias": dif}
    completo = dict(r, inventarios={"mxd": lecturas[0]["inventario"], "referencia": lecturas[1]["inventario"]})
    fichero = _guardar_detalle_lote(completo, prefijo="compare_mxd")
    if fichero:
        r["detalle_completo"] = fichero
    return r


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
