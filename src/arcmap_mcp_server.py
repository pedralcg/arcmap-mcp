# -*- coding: utf-8 -*-
"""
arcmap_mcp_server.py  ──  Servidor MCP EXTERNO (Python 3, 64 bits) + FastMCP.

Es la pieza que registras en Claude Code / OpenCode. Habla por stdio con el
cliente IA y reenvía cada herramienta, por socket TCP local, al puente que
corre dentro de ArcMap (arcmap_bridge.py, Python 2.7).

    Claude Code ──stdio──► este servidor ──socket 127.0.0.1:27179──► ArcMap vivo

Arranque manual de prueba:
    python arcmap_mcp_server.py        (requiere ArcMap abierto con el puente)
"""

import os
import json
import base64
import socket

from mcp.server.fastmcp import FastMCP, Image

# Configurable por entorno: en local apunta a 127.0.0.1; para ArcMap en otra
# máquina (p.ej. vía Tailscale/VPN) exporta ARCMAP_BRIDGE_HOST=<IP-de-tu-equipo>.
HOST = os.environ.get("ARCMAP_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARCMAP_BRIDGE_PORT", "27179"))
TIMEOUT = int(os.environ.get("ARCMAP_BRIDGE_TIMEOUT", "60"))  # tools rápidas
# Timeout amplio para geoprocesos pesados (análisis ambiental, run_geoprocessing, LiDAR/TIN):
# el puente los corre en el hilo principal de ArcMap y pueden tardar minutos. Si se
# corta antes, el server reporta "timeout" pero el proceso sigue vivo en ArcMap.
GP_TIMEOUT = int(os.environ.get("ARCMAP_GP_TIMEOUT", "1800"))  # 30 min


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
            return {
                "ok": False,
                "error": (
                    "No hay puente en %s:%s. Abre ArcMap, ejecuta arcmap_bridge.py "
                    "en la ventana de Python y reintenta." % (self.host, self.port)
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
                    return {"ok": False, "error": (
                        "Timeout esperando a ArcMap (%ss). Si es un geoproceso pesado, "
                        "sigue corriendo dentro de ArcMap; sube ARCMAP_GP_TIMEOUT." % eff)}
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


@mcp.tool()
def ping() -> dict:
    """Comprueba que el puente dentro de ArcMap responde (y versión de ArcGIS)."""
    return _client.send("ping")


@mcp.tool()
def get_arcmap_info() -> dict:
    """Info del documento ArcMap abierto: ruta del .mxd, data frames, escala activa."""
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
def refresh() -> dict:
    """Refresca la vista activa y la tabla de contenidos de ArcMap."""
    return _client.send("refresh")


@mcp.tool()
def execute_arcpy(code: str) -> dict:
    """
    Ejecuta código arcpy ARBITRARIO dentro del ArcMap vivo (Python 2.7).

    El código dispone de estas variables ya preparadas:
      - arcpy            : el módulo arcpy
      - MAP / mapping    : arcpy.mapping
      - mxd              : MapDocument("CURRENT")  (el documento abierto)
      - df               : el data frame activo
    Debe ser Python 2.7 válido (sin f-strings) y asignar el resultado a `RESULT`.

    Ejemplo de `code`:
        RESULT = [l.name for l in MAP.ListLayers(mxd)]
    """
    return _client.send("execute_code", {"code": code})


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


if __name__ == "__main__":
    mcp.run()
