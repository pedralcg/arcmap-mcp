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
TIMEOUT = int(os.environ.get("ARCMAP_BRIDGE_TIMEOUT", "60"))  # tools rápidas
# Timeout amplio para geoprocesos pesados (análisis ambiental, run_geoprocessing, LiDAR/TIN):
# el puente los corre en el hilo principal de ArcMap y pueden tardar minutos. Si se
# corta antes, el server reporta "timeout" pero el proceso sigue vivo en ArcMap.
GP_TIMEOUT = int(os.environ.get("ARCMAP_GP_TIMEOUT", "1800"))  # 30 min
# execute_arcpy es interactivo y NO debe esperar 30 min: a los 120 s el cliente MCP
# ya ha dado la llamada por colgada, así que un techo alto solo sirve para que el
# fallo no tenga forma de error (nos costó las sesiones del 27-jul y del 29-jul).
# Va por encima del ARCMAP_EXEC_TIMEOUT del add-in (900 s) a propósito: así vence
# primero el timeout del add-in, que mata el subproceso y devuelve un error que dice
# en qué FASE se quedó el runner, en vez de un corte mudo de socket que además deja
# el runner huérfano vivo (y un runner huérfano impide cerrar ArcMap).
EXEC_TIMEOUT = int(os.environ.get("ARCMAP_EXEC_TIMEOUT_CLIENTE", "930"))


class ArcMapClient:
    """Cliente socket hacia el puente dentro de ArcMap. Reconecta por comando."""

    def __init__(self, host=HOST, port=PORT, timeout=TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    def send(self, ctype, params=None, timeout=None):
        """Envía un comando al puente. `timeout` (s) sobrescribe el del cliente para
        esta llamada (los geoprocesos pesados pasan GP_TIMEOUT)."""
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
                    # el add-in está cargado; lo que no contesta es ArcMap, que atiende
                    # el socket en su HILO PRINCIPAL y no puede hacerlo mientras corre
                    # un geoproceso. Es el opuesto exacto de "puente_caido": aquí el
                    # trabajo sigue vivo dentro de ArcMap y matarlo es lo que no hay
                    # que hacer.
                    return {"ok": False, "estado": "puente_ocupado", "error": (
                        "PUENTE VIVO PERO SIN RESPONDER en %ss: la conexión se abrió, "
                        "así que el add-in está cargado y es ArcMap quien está ocupado "
                        "(atiende en su hilo principal). Si era un geoproceso pesado "
                        "SIGUE CORRIENDO dentro de ArcMap: no relances, mira la ventana "
                        "y sube ARCMAP_GP_TIMEOUT si necesitas esperar más." % eff)}
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
    """Lista las capas del data frame activo (nombre, visibilidad, fuente, def. query)."""
    return _client.send("list_layers")


@mcp.tool()
def zoom_to_layer(nombre: str) -> dict:
    """Encuadra el mapa al extent de una capa por nombre y refresca el canvas vivo."""
    return _client.send("zoom_to_layer", {"nombre": nombre})


@mcp.tool()
def export_pdf(salida: str, dpi: int = 300) -> dict:
    """Exporta el layout actual de ArcMap a un PDF en la ruta `salida`."""
    return _client.send("export_pdf", {"salida": salida, "dpi": dpi})


@mcp.tool()
def export_jpg(salida: str, dpi: int = 230) -> dict:
    """
    Exporta el layout actual de ArcMap a un JPG en la ruta `salida` (`dpi` 230 por
    defecto, calidad JPEG 95). Útil para adjuntar planos por correo o incrustarlos
    en documentos sin el peso de un PDF.
    """
    return _client.send("export_jpg", {"salida": salida, "dpi": dpi})


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
    menciona `mxd`, `df` o `mapping`. `usar_documento` fuerza esa decisión: `False`
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

    Lo caro es otra cosa, y es lo que llevaba mal documentado desde el principio:
    **el arcpy standalone se BLOQUEA al abrir un documento mientras otra cosa tiene
    tomada la licencia de Desktop.** El mismo .mxd que abre en 0,7 s con ArcMap
    cerrado seguía bloqueado a los 180 s con ArcMap abierto. El `import arcpy`
    funciona igual en los dos casos, así que el bloqueo no se ve venir. Ahí es donde
    salen los 324,5 s que este docstring dio durante un tiempo como coste normal de
    abrir, y los timeouts de 900 s.

    Dentro del puente esto normalmente no muerde, porque el runner corre bajo la
    propia sesión de ArcMap. Si ves esperas largas abriendo documentos, sospecha de
    **contención de licencia** antes que del tamaño del fichero o de las fuentes de
    datos. Para inspeccionar sin abrir nada, `describe_mxd` (milisegundos, sin arcpy
    y sin licencia).

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
    return _client.send("execute_code", params, timeout=EXEC_TIMEOUT)


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
    """
    return _client.send("list_ddp", {"max_valores": max_valores})


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
    """
    return _client.send("export_ddp", {
        "salida": salida, "modo": modo, "rango": rango, "valores": valores,
        "un_pdf_por_pagina": un_pdf_por_pagina, "dpi": dpi,
    })


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
def set_text_element(texto: str, nombre: str = None, buscar: str = None) -> dict:
    """
    Cambia el contenido de un elemento de texto del layout (título, fecha, nº de
    expediente...). Indica el `texto` nuevo y UN selector:

      - `nombre`: el .name del elemento (si está nombrado en ArcMap).
      - `buscar`: su texto ACTUAL (find-and-replace). Coincidencia exacta y, si no,
        por subcadena. Útil cuando los textos del layout no están nombrados (lo
        habitual). Si hay varias coincidencias, el error las lista para afinar.
    """
    return _client.send("set_text_element", {"nombre": nombre, "buscar": buscar, "texto": texto})


@mcp.tool()
def goto_ddp_page(pagina: int = None, valor: str = None) -> dict:
    """
    Sitúa el atlas en una página y refresca la vista. Indica `pagina` (ID 1-based)
    o `valor` (un valor del campo índice; se resuelve a su página). Devuelve el ID,
    el valor de la página y la escala resultante.
    """
    return _client.send("goto_ddp_page", {"pagina": pagina, "valor": valor})


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
                    alto: int = None, modo: str = "vista") -> dict:
    """
    Exporta a un PNG en disco la vista del mapa (`modo="vista"`, data frame activo)
    o la página de layout completa (`modo="layout"`).

    Genera un ARCHIVO (artefacto reutilizable). `dpi` 150 por defecto; `ancho`/`alto`
    en píxeles opcionales. Para que el agente VEA el mapa al instante sin abrir el
    archivo, usa `get_canvas_screenshot`. Para el entregable final usa `export_pdf` /
    `export_ddp`.
    """
    return _client.send("export_view_png", {
        "salida": salida, "dpi": dpi, "ancho": ancho, "alto": alto, "modo": modo,
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
def get_unique_values(capa: str, campo: str, where: str = None) -> dict:
    """
    Devuelve los valores únicos (ordenados) de un campo de una capa. Respeta la
    definition query. `where` opcional para acotar. Útil para iterar planos por
    categoría (un plano por estrato, por municipio, etc.).
    """
    return _client.send("get_unique_values", {"capa": capa, "campo": campo, "where": where})


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
def add_layer(fuente: str, posicion: str = "TOP", grupo: str = None) -> dict:
    """
    Añade una capa al data frame activo desde una ruta a shapefile, feature class de
    file geodatabase o raster. `posicion`: TOP / BOTTOM / AUTO_ARRANGE. `grupo`
    opcional = nombre de una capa de grupo donde insertarla.
    """
    return _client.send("add_layer", {"fuente": fuente, "posicion": posicion, "grupo": grupo})


@mcp.tool()
def remove_layer(capa: str) -> dict:
    """Quita una capa del data frame activo por nombre."""
    return _client.send("remove_layer", {"capa": capa})


@mcp.tool()
def apply_symbology_from_layer(capa: str, lyr_file: str) -> dict:
    """
    Aplica la simbología de un archivo `.lyr` (estilos canónicos) a una capa
    del mapa. `lyr_file` es la ruta al .lyr de origen.
    """
    return _client.send("apply_symbology_from_layer", {"capa": capa, "lyr_file": lyr_file})


@mcp.tool()
def set_graduated_symbology(capa: str, campo: str, num_clases: int = 5,
                            metodo: str = "natural_breaks",
                            color_desde: list = None, color_hasta: list = None,
                            tamano: float = None) -> dict:
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
    - `color_desde` / `color_hasta`: RGB [r,g,b] 0-255 de los extremos de la rampa
      (defecto amarillo claro -> rojo oscuro).
    - `tamano`: grosor de línea / tamaño de punto / grosor de borde de polígono en
      puntos (defecto según geometría).

    El histograma usa TODOS los valores del campo en la fuente (ignora definition
    query y selección); para clasificar un subconjunto, fíjalo con una definition
    query permanente antes.
    """
    params = {"capa": capa, "campo": campo, "num_clases": num_clases, "metodo": metodo}
    if color_desde is not None:
        params["color_desde"] = color_desde
    if color_hasta is not None:
        params["color_hasta"] = color_hasta
    if tamano is not None:
        params["tamano"] = tamano
    return _client.send("set_graduated_symbology", params)


@mcp.tool()
def set_raster_symbology(capa: str, modo: str = "clasificado", num_clases: int = 5,
                         color_desde: list = None, color_hasta: list = None) -> dict:
    """
    Simboliza una capa RÁSTER de la TOC: clasificada o estirada.

    Es la herramienta para NDVI, FCC, P95, pendientes y demás producto ráster.
    `set_graduated_symbology` NO vale aquí: solo acepta capas de entidades.
    Tampoco vale `execute_arcpy`, que opera sobre una copia y descarta los
    cambios de renderer.

    `modo`:
      - `clasificado` (por defecto): `num_clases` clases (2-32) con rampa de color.
      - `estirado`: rampa continua entre el mínimo y el máximo de la banda 0.

    `color_desde` / `color_hasta` son `[R, G, B]` de 0 a 255. Por defecto va de
    amarillo claro `[255, 255, 178]` a rojo oscuro `[189, 0, 38]`, que es
    secuencial y funciona para casi todo lo continuo.

    Si el ráster no tiene estadísticas calculadas, la clasificación falla: el
    error lo dice y hay que calcularlas sobre la capa antes.
    """
    params: dict = {"capa": capa, "modo": modo, "num_clases": num_clases}
    if color_desde is not None:
        params["color_desde"] = color_desde
    if color_hasta is not None:
        params["color_hasta"] = color_hasta
    return _client.send("set_raster_symbology", params)


@mcp.tool()
def set_unique_values_symbology(capa: str, campo: str, tamano: float = None,
                                color_desde: list = None, color_hasta: list = None) -> dict:
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
    usa esa rampa en su lugar.

    Tope de **100 categorías**: por encima, falla diciendo cuántas hay. Una
    leyenda de miles de entradas no es una leyenda. Los NULL se omiten.
    """
    params: dict = {"capa": capa, "campo": campo}
    if tamano is not None:
        params["tamano"] = tamano
    if color_desde is not None:
        params["color_desde"] = color_desde
    if color_hasta is not None:
        params["color_hasta"] = color_hasta
    return _client.send("set_unique_values_symbology", params)


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
def save_mxd() -> dict:
    """
    Guarda el documento .mxd abierto en su ruta actual. Devuelve la ruta guardada.
    Útil para persistir los cambios que han hecho otras tools (def. query, textos,
    simbología). Para guardar en otra ruta sin tocar el original usa save_mxd_as.
    """
    return _client.send("save_mxd")


@mcp.tool()
def save_mxd_as(salida: str) -> dict:
    """
    Guarda una COPIA del .mxd en `salida` (no cambia el documento activo ni su ruta).
    `salida` es la ruta de destino (.mxd). Equivale a `mxd.saveACopy(...)`.
    """
    return _client.send("save_mxd_as", {"salida": salida})


@mcp.tool()
def list_broken_data_sources() -> dict:
    """
    Lista las capas/tablas con la fuente de datos ROTA (rutas que ArcMap no encuentra,
    muy común con unidades de red X:/Y:/G:). Para cada una devuelve nombre, ruta rota y
    workspace si es accesible. Primer paso antes de `repair_data_source`.
    """
    return _client.send("list_broken_data_sources")


@mcp.tool()
def repair_data_source(capa: str, ruta_antigua: str, ruta_nueva: str,
                       validar: bool = True) -> dict:
    """
    Reapunta la fuente de una capa sustituyendo su workspace (`ruta_antigua` ->
    `ruta_nueva`), p. ej. cuando una carpeta de red ha cambiado de letra/ubicación.
    Con `validar=True` el cambio solo se aplica si la ruta nueva es válida. Devuelve
    si la capa estaba/queda rota.
    """
    return _client.send("repair_data_source", {"capa": capa, "ruta_antigua": ruta_antigua,
                                                "ruta_nueva": ruta_nueva, "validar": validar})


@mcp.tool()
def run_geoprocessing(tool: str, params: list = None,
                      resolver_capas: bool = True) -> dict:
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

    Nota: un geoproceso largo congela la GUI de ArcMap (hilo único, limitación conocida);
    se usa un timeout amplio (ARCMAP_GP_TIMEOUT) para no cortar la espera.
    """
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
    `limite` = máximo de filas (50 por defecto) para no inflar la respuesta. Complementa
    a get_unique_values / count_features cuando necesitas ver registros concretos.
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
# DISCO. Son LENTOS: usan el timeout amplio (ARCMAP_GP_TIMEOUT, 30 min) y, mientras
# corren, CONGELAN la GUI de ArcMap (el puente ejecuta en el hilo principal). Por
# defecto añaden el resultado al data frame activo (anadir_al_mapa=True).
# --------------------------------------------------------------------------- #

@mcp.tool()
def raster_index(indice: str, bandas: dict, salida: str,
                 L: float = 0.5, anadir_al_mapa: bool = True,
                 banda_a: str = None, banda_b: str = None) -> dict:
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
    """
    return _client.send("raster_index", {
        "indice": indice, "bandas": bandas, "salida": salida, "L": L,
        "anadir_al_mapa": anadir_al_mapa, "banda_a": banda_a, "banda_b": banda_b,
    }, timeout=GP_TIMEOUT)


@mcp.tool()
def hydrology(operacion: str, parametros: dict, anadir_al_mapa: bool = True) -> dict:
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
    """
    return _client.send("hydrology", {
        "operacion": operacion, "parametros": parametros,
        "anadir_al_mapa": anadir_al_mapa,
    }, timeout=GP_TIMEOUT)


@mcp.tool()
def contours(mdt: str, salida: str, intervalo: float, base: float = 0,
             dxf: str = None, anadir_al_mapa: bool = True) -> dict:
    """
    Genera curvas de nivel desde un MDT (3D Analyst). `intervalo` = equidistancia
    (en unidades Z del MDT), `base` = cota base (0 por defecto). Si pasas `dxf`
    (ruta), exporta además las curvas a DXF (entrega CAD). `salida` = feature class
    de líneas. Geoproceso pesado.
    """
    return _client.send("contours", {
        "mdt": mdt, "salida": salida, "intervalo": intervalo, "base": base,
        "dxf": dxf, "anadir_al_mapa": anadir_al_mapa,
    }, timeout=GP_TIMEOUT)


@mcp.tool()
def topographic_profile(superficie: str, lineas: str, salida: str,
                        anadir_al_mapa: bool = True) -> dict:
    """
    Perfil topográfico: interpola una capa de LÍNEAS 2D sobre una `superficie`
    (MDT ráster o TIN) y devuelve líneas 3D con la Z del terreno (3D Analyst,
    InterpolateShape). Útil para perfiles longitudinales de caminos, cauces o
    transectos. `salida` = feature class de líneas 3D. Geoproceso pesado.
    """
    return _client.send("topographic_profile", {
        "superficie": superficie, "lineas": lineas, "salida": salida,
        "anadir_al_mapa": anadir_al_mapa,
    }, timeout=GP_TIMEOUT)


@mcp.tool()
def least_cost_path(coste: str, origen: str, destino: str, salida: str,
                    salida_dir: str = None, anadir_al_mapa: bool = True) -> dict:
    """
    Ruta de mínimo coste (Spatial Analyst): calcula CostDistance desde `origen`
    sobre el ráster de fricción `coste` y traza CostPath hasta `destino`.
    `origen`/`destino` = features (puntos/polígonos) o ráster; `coste` = ráster de
    fricción (mayor valor = más difícil de atravesar). `salida` = ráster con la ruta
    óptima. Útil para trazado de pistas, cortafuegos o accesos. Geoproceso pesado.
    """
    return _client.send("least_cost_path", {
        "coste": coste, "origen": origen, "destino": destino, "salida": salida,
        "salida_dir": salida_dir, "anadir_al_mapa": anadir_al_mapa,
    }, timeout=GP_TIMEOUT)


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
    """
    return _client.send("calculate_geometry", {
        "entrada": entrada, "propiedades": propiedades,
        "unidad_longitud": unidad_longitud, "unidad_area": unidad_area, "crs": crs,
    })


def _version_arcmap_local():
    """Versión de ArcMap instalada EN ESTA MÁQUINA, por el nombre de la carpeta.

    Se mira el disco y no el puente porque `ping` no devuelve la versión de
    ArcGIS (su docstring lo prometía y la respuesta real no la trae). Ojo: si el
    servidor corre en una máquina distinta de ArcMap (acceso remoto), esto NO es
    la versión de la sesión; por eso quien lo use lo dice explícitamente.
    """
    for base in (r"C:\Program Files (x86)\ArcGIS", r"C:\Program Files\ArcGIS"):
        try:
            nombres = os.listdir(base)
        except OSError:
            continue
        for nombre in sorted(nombres, reverse=True):
            if nombre.lower().startswith("desktop"):
                return nombre[len("Desktop"):] or None
    return None


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
    local = _version_arcmap_local()
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
    else:
        veredicto = "indeterminado"
        motivo = ("versión del documento: %s; ArcMap local: %s. Falta una de las dos "
                  "para poder comparar." % (version, local))

    return {
        "ok": True,
        "ruta": ruta_abs,
        "tamano_bytes": os.path.getsize(ruta_abs),
        "version_declarada": version,
        "version_arcmap_local": local,
        "veredicto": veredicto,
        "motivo": motivo,
        "aviso_version_local": ("'version_arcmap_local' se deduce de la instalación de "
                                "ESTA máquina, no de la sesión conectada al puente."),
    }


def _arcmap_esta_abierto():
    """True si hay un ArcMap.exe corriendo. None si no se pudo averiguar.

    Importa mucho para `audit_folder`: el arcpy standalone **se bloquea al abrir un
    documento mientras ArcMap tiene la licencia tomada**. Medido el 2026-08-27 sobre
    el mismo .mxd: con ArcMap cerrado abre en 0,7 s; con ArcMap abierto sigue
    bloqueado a los 180 s. El `import arcpy` funciona igual en los dos casos (~6 s),
    así que el bloqueo NO se ve venir: aparece en `MapDocument()`.
    """
    try:
        import subprocess
        salida = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq ArcMap.exe", "/NH"],
            capture_output=True, timeout=20)
        return b"ArcMap.exe" in salida.stdout
    except Exception:
        return None


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

    ⚠️ **CIERRA ARCMAP ANTES DE USAR `con_capas`.** El arcpy standalone se
    **bloquea al abrir un documento mientras ArcMap tiene la licencia tomada**.
    Medido el 2026-08-27 sobre el mismo .mxd: con ArcMap cerrado abre en **0,7 s**;
    con ArcMap abierto sigue bloqueado a los **180 s**. Y no se ve venir, porque
    `import arcpy` funciona igual en ambos casos: el bloqueo aparece en
    `MapDocument()`. Por eso esta herramienta comprueba si ArcMap está corriendo y
    se niega a hacer la pasada 2, salvo que pases
    `forzar_con_arcmap_abierto=True`.

    Presupuesta el tiempo: con ArcMap cerrado, abrir cuesta menos de un segundo por
    documento más ~6 s de `import arcpy` por proceso. Empieza con `con_capas=False`
    para tener el mapa de versiones al instante.

    `max_documentos` corta la lista (por defecto 200) y **lo dice** en la
    respuesta: nunca trunca en silencio.
    """
    import subprocess  # local: solo esta herramienta lo necesita

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
    if con_capas and not forzar_con_arcmap_abierto and _arcmap_esta_abierto():
        con_capas = False
        aviso_capas = (
            "PASADA 2 OMITIDA: ArcMap está abierto. El arcpy standalone se bloquea al "
            "abrir documentos mientras ArcMap tiene la licencia tomada (medido: 0,7 s "
            "con ArcMap cerrado frente a >180 s con ArcMap abierto, mismo .mxd). "
            "Cierra ArcMap y repite, o pasa forzar_con_arcmap_abierto=True si sabes "
            "lo que haces: cada documento agotará su timeout sin dar nada.")

    py27 = _python27_arcgis() if con_capas else None
    auditor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "auditor_mxd.py")
    if con_capas and (py27 is None or not os.path.isfile(auditor)):
        con_capas = False
        aviso_capas = ("No se pudo inspeccionar capas: falta el Python 2.7 de ArcGIS "
                       "(define ARCMAP_PYTHON27) o el auditor. Se devuelve solo la pasada 1.")

    resultados = []
    resumen = {"con_capas": 0, "timeout": 0, "error_al_abrir": 0, "ilegibles": 0}
    versiones: dict = {}

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

        if con_capas:
            try:
                proc = subprocess.run(
                    [py27, auditor, doc],
                    capture_output=True, timeout=timeout_por_documento)
                bruto = proc.stdout.decode("utf-8", "replace").strip()
                datos = json.loads(bruto) if bruto else {"ok": False, "error": "sin salida"}
                if datos.get("ok"):
                    fila["num_capas"] = datos.get("num_capas")
                    fila["num_rotas"] = datos.get("num_rotas")
                    fila["num_con_query"] = datos.get("num_con_query")
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


if __name__ == "__main__":
    mcp.run()
