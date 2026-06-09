# -*- coding: utf-8 -*-
"""
arcmap_bridge.py  ──  PUENTE que corre DENTRO de ArcMap 10.5 (Python 2.7).

MODELO: timer en el HILO PRINCIPAL (equivalente al QTimer de QGIS MCP), NO un
hilo de fondo. ArcMap (intérprete embebido) NO ejecuta hilos de Python en
segundo plano para atender el socket — verificado empíricamente: con
hilo de fondo el puerto abre pero las conexiones se encolan sin atender.

Solución: socket NO bloqueante + un timer nativo de Windows (user32.SetTimer)
que dispara una función de sondeo en el hilo principal de ArcMap cada 100 ms.
Cada tick: acepta conexiones pendientes, lee comandos completos y los despacha
(arcpy y MapDocument("CURRENT") se ejecutan AQUÍ, en el hilo principal -> OK).
La GUI no se congela porque cada tick procesa un comando rápido y devuelve.
Un geoproceso pesado SÍ bloqueará la GUI mientras dura (igual que ejecutarlo a mano).

Cómo se usa:
    1. Abre tu .mxd en ArcMap 10.5.
    2. Geoprocessing > Python.
    3. Ejecuta:  execfile(r"C:\\mcp\\arcmap-mcp\\src\\arcmap_bridge.py")
       Debe imprimir: [arcmap-bridge] activo en 127.0.0.1:27179 ...
    4. Para parar:  stop()
"""

import os
import sys
import json
import errno
import socket
import ctypes
import base64
import fnmatch
import tempfile
import traceback
import StringIO

import arcpy
import arcpy.mapping as MAP

HOST = os.environ.get("ARCMAP_BRIDGE_BIND", "127.0.0.1")
PORT = int(os.environ.get("ARCMAP_BRIDGE_PORT", "27179"))

# Estado global del puente.
_server = {"sock": None, "clients": None, "running": False,
           "timerproc": None, "timer_id": None, "busy": False}

# --- Win32 SetTimer (timer en el hilo principal de ArcMap) ----------------- #
user32 = ctypes.windll.user32
TIMERPROC = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint,
                               ctypes.c_void_p, ctypes.c_ulong)
user32.SetTimer.restype = ctypes.c_void_p
user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, TIMERPROC]
user32.KillTimer.restype = ctypes.c_int
user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

_WOULDBLOCK = (errno.EWOULDBLOCK, errno.EAGAIN, 10035)  # 10035 = WSAEWOULDBLOCK


def _u(x):
    """Todo a unicode, tolerante a bytes con acentos (rutas con tildes, ñ...)."""
    if isinstance(x, unicode):
        return x
    if isinstance(x, str):
        try:
            return x.decode("utf-8")
        except Exception:
            return x.decode("latin-1", "replace")
    return unicode(x)


def _get_layer(mxd, df, nombre):
    """Localiza una capa por nombre en el df; error accionable con la lista si no existe."""
    capas = MAP.ListLayers(mxd, nombre, df)
    if not capas:
        disponibles = [l.name for l in MAP.ListLayers(mxd, "*", df)]
        raise ValueError(u"Capa no encontrada: %s. Disponibles: %s"
                         % (_u(nombre), u", ".join(_u(n) for n in disponibles)))
    return capas[0]


def _get_ddp(mxd):
    """Devuelve el objeto Data Driven Pages o error accionable si el mxd no las tiene."""
    try:
        ddp = mxd.dataDrivenPages
    except Exception:
        ddp = None
    if ddp is None:
        raise ValueError(u"El documento no tiene Data Driven Pages habilitadas "
                         u"(Vista > Data Driven Pages en ArcMap).")
    return ddp


def _contar_paginas_rango(s):
    """Cuenta páginas en un page_range_string tipo '1-3,5' (expande los guiones)."""
    n = 0
    for parte in s.split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "-" in parte:
            a, b = parte.split("-", 1)
            try:
                n += int(b) - int(a) + 1
            except ValueError:
                n += 1
        else:
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Handlers de comandos — se ejecutan en el HILO PRINCIPAL de ArcMap.
# --------------------------------------------------------------------------- #

def h_echo(params):
    """Diagnóstico: NO toca arcpy. Si responde, el sondeo del socket funciona."""
    return {"echo": True, "recibido": params}


def h_ping(params):
    info = arcpy.GetInstallInfo()
    return {"pong": True, "producto": "arcmap-bridge",
            "arcgis": info.get("Version"), "build": info.get("BuildNumber")}


def h_get_arcmap_info(params):
    mxd = MAP.MapDocument("CURRENT")
    df = mxd.activeDataFrame
    # df.scale puede lanzar ("error durante obtención de escala") según el estado
    # del documento (p. ej. vista de layout activa); que no tumbe toda la llamada.
    try:
        escala = df.scale
    except Exception:
        escala = None
    # Vista activa: nombre del data frame en vista de datos, o "PAGE_LAYOUT".
    try:
        vista_activa = mxd.activeView
    except Exception:
        vista_activa = None
    out = {
        "mxd": mxd.filePath,
        "titulo": mxd.title,
        "data_frames": [d.name for d in MAP.ListDataFrames(mxd)],
        "df_activo": df.name,
        "escala_activa": escala,
        "vista_activa": vista_activa,
    }
    del mxd
    return out


def h_list_layers(params):
    mxd = MAP.MapDocument("CURRENT")
    df = mxd.activeDataFrame
    capas = []
    for lyr in MAP.ListLayers(mxd, "*", df):
        item = {"nombre": lyr.name,
                "visible": bool(lyr.visible),
                "es_grupo": bool(lyr.isGroupLayer)}
        if lyr.supports("DATASOURCE"):
            item["fuente"] = lyr.dataSource
        if lyr.supports("DEFINITIONQUERY"):
            item["definition_query"] = lyr.definitionQuery
        capas.append(item)
    del mxd
    return {"data_frame": df.name, "num": len(capas), "capas": capas}


def h_zoom_to_layer(params):
    nombre = params.get("nombre")
    mxd = MAP.MapDocument("CURRENT")
    df = mxd.activeDataFrame
    capas = MAP.ListLayers(mxd, nombre, df)
    if not capas:
        del mxd
        raise ValueError("Capa no encontrada: %s" % nombre)
    df.extent = capas[0].getExtent()
    arcpy.RefreshActiveView()
    escala = df.scale
    del mxd
    return {"capa": nombre, "escala": escala}


def h_export_pdf(params):
    mxd = MAP.MapDocument("CURRENT")
    salida = params["salida"]
    dpi = int(params.get("dpi", 300))
    if not salida.lower().endswith(".pdf"):
        salida += ".pdf"
    MAP.ExportToPDF(mxd, salida, resolution=dpi)
    del mxd
    return {"salida": salida, "dpi": dpi}


def h_refresh(params):
    arcpy.RefreshActiveView()
    arcpy.RefreshTOC()
    return {"refrescado": True}


def h_execute_code(params):
    """Ejecuta código arcpy arbitrario en el ArcMap vivo.
    Dispone de: arcpy, MAP/mapping, mxd (=CURRENT), df (df activo). Asigna RESULT."""
    code = params.get("code", "")
    buff = StringIO.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buff
    mxd = MAP.MapDocument("CURRENT")
    ns = {"arcpy": arcpy, "MAP": MAP, "mapping": MAP,
          "mxd": mxd, "df": mxd.activeDataFrame, "RESULT": None}
    try:
        exec(code, ns)
    finally:
        sys.stdout = old_stdout
    try:
        arcpy.RefreshActiveView()
        arcpy.RefreshTOC()
    except Exception:
        pass
    del mxd
    return {"result": ns.get("RESULT"), "stdout": buff.getvalue()}


# --------------------------------------------------------------------------- #
# Series de planos (Data Driven Pages + layout).
# --------------------------------------------------------------------------- #

def h_list_ddp(params):
    """¿Hay atlas (Data Driven Pages)? nº páginas, campo índice, capa índice y valores.
    Lee los valores con un SearchCursor sobre la capa índice: NO toca currentPageID."""
    mxd = MAP.MapDocument("CURRENT")
    try:
        ddp = mxd.dataDrivenPages
    except Exception:
        ddp = None
    if ddp is None:
        del mxd
        return {"habilitado": False}
    campo = ddp.pageNameField.name if ddp.pageNameField else None
    capa_idx = ddp.indexLayer.name if ddp.indexLayer else None
    maximo = int(params.get("max_valores", 500))
    valores = []
    truncado = False
    if campo and ddp.indexLayer is not None:
        try:
            for row in arcpy.da.SearchCursor(ddp.indexLayer, [campo]):
                if len(valores) >= maximo:
                    truncado = True
                    break
                valores.append(row[0])
        except Exception:
            valores = []
    out = {"habilitado": True,
           "num_paginas": ddp.pageCount,
           "campo_nombre": campo,
           "capa_indice": capa_idx,
           "valores": valores,
           "valores_truncados": truncado}
    del mxd
    return out


def h_export_ddp(params):
    """Exporta el atlas a PDF: ALL / CURRENT / rango de IDs / lista de valores del campo
    índice. un_pdf_por_pagina=True -> un PDF por página (nombre = valor del índice)."""
    mxd = MAP.MapDocument("CURRENT")
    try:
        ddp = _get_ddp(mxd)
        salida = params["salida"]
        if not salida.lower().endswith(".pdf"):
            salida += ".pdf"
        dpi = int(params.get("dpi", 300))
        modo = (params.get("modo") or "ALL").upper()
        rango = params.get("rango")
        valores = params.get("valores")
        un_por_pagina = bool(params.get("un_pdf_por_pagina", False))

        page_range_string = u""
        if valores:
            ids = []
            for v in valores:
                pid = ddp.getPageIDFromName(_u(v))
                if pid:
                    ids.append(pid)
            if not ids:
                raise ValueError(u"Ningún valor coincide con páginas del atlas: %s"
                                 % u", ".join(_u(v) for v in valores))
            page_range_string = u",".join(str(i) for i in ids)
            range_type = "RANGE"
        elif rango:
            page_range_string = _u(rango)
            range_type = "RANGE"
        elif modo == "CURRENT":
            range_type = "CURRENT"
        else:
            range_type = "ALL"

        multiple = "PDF_MULTIPLE_FILES_PAGE_NAME" if un_por_pagina else "PDF_SINGLE_FILE"
        ddp.exportToPDF(salida, range_type, page_range_string, multiple, dpi)

        if range_type == "ALL":
            num = ddp.pageCount
        elif range_type == "CURRENT":
            num = 1
        else:
            num = _contar_paginas_rango(page_range_string)
        out = {"salida": salida, "modo_efectivo": range_type,
               "page_range": page_range_string, "num": num,
               "un_pdf_por_pagina": un_por_pagina, "dpi": dpi}
    finally:
        del mxd
    return out


def h_list_layout_elements(params):
    """Lista elementos del layout (nombre + tipo de clase). Filtra por tipo arcpy
    (TEXT_ELEMENT, LEGEND_ELEMENT, PICTURE_ELEMENT...) y/o por patrón wildcard."""
    mxd = MAP.MapDocument("CURRENT")
    tipo = params.get("tipo")
    patron = params.get("patron") or "*"
    if tipo:
        elems = MAP.ListLayoutElements(mxd, tipo, patron)
    else:
        elems = MAP.ListLayoutElements(mxd)
    salida = []
    for el in elems:
        nombre = getattr(el, "name", u"")
        if (not tipo) and patron != "*" and not fnmatch.fnmatch(nombre, patron):
            continue
        item = {"nombre": nombre, "tipo": type(el).__name__}
        try:
            item["texto"] = el.text          # solo TextElement lo expone
        except Exception:
            pass
        salida.append(item)
    del mxd
    return {"num": len(salida), "elementos": salida}


def h_set_text_element(params):
    """Cambia el texto de un elemento de texto del layout (título, fecha, expediente).

    Selector (uno de):
      - nombre: por el .name del elemento (si está nombrado en ArcMap).
      - buscar: por su texto ACTUAL (find-and-replace). Coincidencia exacta primero;
        si no, por subcadena. Si hay varios candidatos -> error listándolos.
    """
    nombre = params.get("nombre")
    buscar = params.get("buscar")
    texto = params.get("texto", u"")
    mxd = MAP.MapDocument("CURRENT")
    elems = MAP.ListLayoutElements(mxd, "TEXT_ELEMENT")

    objetivo = None
    if nombre:
        for el in elems:
            if el.name == nombre:
                objetivo = el
                break
        if objetivo is None:
            disponibles = [el.name for el in elems if el.name]
            del mxd
            raise ValueError(u"Elemento de texto no encontrado por nombre: %s. "
                             u"Nombrados disponibles: %s"
                             % (_u(nombre), u", ".join(_u(n) for n in disponibles) or u"(ninguno)"))
    elif buscar is not None:
        objetivo_buscar = _u(buscar)
        exactos = [el for el in elems if el.text == objetivo_buscar]
        candidatos = exactos or [el for el in elems if objetivo_buscar in (el.text or u"")]
        if not candidatos:
            del mxd
            raise ValueError(u"Ningún elemento de texto coincide con: %s" % objetivo_buscar)
        if len(candidatos) > 1:
            textos = [el.text for el in candidatos]
            del mxd
            raise ValueError(u"Varios elementos coinciden con %s (%d). Afina 'buscar' "
                             u"o nombra el elemento. Coincidencias: %s"
                             % (objetivo_buscar, len(candidatos),
                                u" | ".join(_u(t) for t in textos)))
        objetivo = candidatos[0]
    else:
        del mxd
        raise ValueError(u"Indica 'nombre' (name del elemento) o 'buscar' (su texto actual).")

    anterior = objetivo.text
    objetivo.text = _u(texto)
    arcpy.RefreshActiveView()
    nombre_el = objetivo.name
    del mxd
    return {"nombre": nombre_el, "texto_anterior": anterior, "texto_nuevo": texto}


def h_goto_ddp_page(params):
    """Sitúa el atlas en una página por ID (pagina) o por valor del campo índice (valor)."""
    mxd = MAP.MapDocument("CURRENT")
    try:
        ddp = _get_ddp(mxd)
        pagina = params.get("pagina")
        valor = params.get("valor")
        if valor is not None:
            pid = ddp.getPageIDFromName(_u(valor))
            if not pid:
                raise ValueError(u"Valor de índice no encontrado en el atlas: %s" % _u(valor))
        elif pagina is not None:
            pid = int(pagina)
        else:
            raise ValueError(u"Indica 'pagina' (ID 1-based) o 'valor' (campo índice).")
        ddp.currentPageID = pid
        arcpy.RefreshActiveView()
        try:
            nombre_pagina = ddp.pageRow.getValue(ddp.pageNameField.name)
        except Exception:
            nombre_pagina = None
        out = {"page_id": pid, "valor": nombre_pagina,
               "escala": mxd.activeDataFrame.scale}
    finally:
        del mxd
    return out


def h_set_definition_query(params):
    """Fija o limpia la definition query de una capa (query vacío/None -> limpia)."""
    capa = params.get("capa")
    query = params.get("query")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        if not lyr.supports("DEFINITIONQUERY"):
            raise ValueError(u"La capa no admite definition query: %s" % _u(capa))
        anterior = lyr.definitionQuery
        lyr.definitionQuery = _u(query) if query else u""
        arcpy.RefreshActiveView()
        out = {"capa": capa, "query_anterior": anterior,
               "query_nueva": lyr.definitionQuery}
    finally:
        del mxd
    return out


def h_set_layer_visibility(params):
    """Enciende o apaga una capa (o grupo) por nombre."""
    capa = params.get("capa")
    visible = bool(params.get("visible", True))
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        lyr.visible = visible
        arcpy.RefreshTOC()
        arcpy.RefreshActiveView()
        out = {"capa": capa, "visible": visible}
    finally:
        del mxd
    return out


def h_export_view_png(params):
    """Exporta a PNG la vista (data frame activo) o el layout completo (modo='layout').

    Escribe un archivo en disco (artefacto). Para que el agente VEA el mapa en línea
    sin abrir el archivo, usa get_canvas_screenshot.
    """
    modo = (params.get("modo") or "vista").lower()
    mxd = MAP.MapDocument("CURRENT")
    try:
        salida = params["salida"]
        if not salida.lower().endswith(".png"):
            salida += ".png"
        dpi = int(params.get("dpi", 150))
        kwargs = {"resolution": dpi}
        ancho = params.get("ancho")
        alto = params.get("alto")
        if ancho:
            kwargs["df_export_width"] = int(ancho)
        if alto:
            kwargs["df_export_height"] = int(alto)
        if modo == "layout":
            MAP.ExportToPNG(mxd, salida, **kwargs)          # página completa
        else:
            MAP.ExportToPNG(mxd, salida, mxd.activeDataFrame, **kwargs)
        out = {"salida": salida, "dpi": dpi, "modo": modo}
    finally:
        del mxd
    return out


# --------------------------------------------------------------------------- #
# Capas y datos (preparar y consultar).
# --------------------------------------------------------------------------- #

def h_select_by_attribute(params):
    """Selección por SQL sobre una capa (NEW_SELECTION). Devuelve nº seleccionado."""
    capa = params.get("capa")
    where = params.get("where", u"")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        arcpy.SelectLayerByAttribute_management(lyr, "NEW_SELECTION", _u(where))
        n = int(arcpy.GetCount_management(lyr).getOutput(0))
        arcpy.RefreshActiveView()
        out = {"capa": capa, "where": where, "seleccionados": n}
    finally:
        del mxd
    return out


def h_clear_selection(params):
    """Limpia la selección de una capa (capa=nombre) o de todas si no se indica capa."""
    capa = params.get("capa")
    mxd = MAP.MapDocument("CURRENT")
    try:
        df = mxd.activeDataFrame
        if capa:
            lyr = _get_layer(mxd, df, capa)
            arcpy.SelectLayerByAttribute_management(lyr, "CLEAR_SELECTION")
            limpiadas = [capa]
        else:
            limpiadas = []
            for lyr in MAP.ListLayers(mxd, "*", df):
                if getattr(lyr, "isFeatureLayer", False):
                    try:
                        arcpy.SelectLayerByAttribute_management(lyr, "CLEAR_SELECTION")
                        limpiadas.append(lyr.name)
                    except Exception:
                        pass
        arcpy.RefreshActiveView()
        out = {"capas": limpiadas, "num": len(limpiadas)}
    finally:
        del mxd
    return out


def h_get_unique_values(params):
    """Valores únicos de un campo de una capa (respeta def. query). 'where' opcional."""
    capa = params.get("capa")
    campo = params.get("campo")
    where = params.get("where")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        vistos = set()
        for row in arcpy.da.SearchCursor(lyr, [campo], _u(where) if where else None):
            vistos.add(row[0])
        try:
            valores = sorted(vistos)
        except Exception:
            valores = list(vistos)
        out = {"capa": capa, "campo": campo, "num": len(valores), "valores": valores}
    finally:
        del mxd
    return out


def h_count_features(params):
    """Conteo de entidades de una capa. Con 'where' cuenta sin alterar la selección."""
    capa = params.get("capa")
    where = params.get("where")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        if where:
            n = 0
            for _row in arcpy.da.SearchCursor(lyr, ["OID@"], _u(where)):
                n += 1
        else:
            n = int(arcpy.GetCount_management(lyr).getOutput(0))
        out = {"capa": capa, "where": where, "num": n}
    finally:
        del mxd
    return out


def h_list_fields(params):
    """Campos de una capa/tabla: nombre, tipo, alias, longitud."""
    capa = params.get("capa")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        campos = [{"nombre": f.name, "tipo": f.type,
                   "alias": f.aliasName, "longitud": f.length}
                  for f in arcpy.ListFields(lyr)]
        out = {"capa": capa, "num": len(campos), "campos": campos}
    finally:
        del mxd
    return out


def h_get_layer_info(params):
    """Detalle de una capa: tipo geom, CRS, extent, nº de entidades y campos."""
    capa = params.get("capa")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        d = arcpy.Describe(lyr)
        crs = None
        try:
            crs = d.spatialReference.name
        except Exception:
            pass
        ext = None
        try:
            e = d.extent
            ext = {"xmin": e.XMin, "ymin": e.YMin, "xmax": e.XMax, "ymax": e.YMax}
        except Exception:
            pass
        tipo_geom = getattr(d, "shapeType", None)
        try:
            num = int(arcpy.GetCount_management(lyr).getOutput(0))
        except Exception:
            num = None
        campos = [{"nombre": f.name, "tipo": f.type} for f in arcpy.ListFields(lyr)]
        out = {"capa": capa, "tipo_geometria": tipo_geom, "crs": crs,
               "extent": ext, "num_features": num, "campos": campos,
               "fuente": d.catalogPath if hasattr(d, "catalogPath") else None}
    finally:
        del mxd
    return out


def h_add_layer(params):
    """Añade una capa desde shp/fgdb/raster al df activo. posicion TOP/BOTTOM/AUTO_ARRANGE.
    grupo opcional = nombre de una capa de grupo donde insertarla."""
    fuente = params.get("fuente")
    posicion = (params.get("posicion") or "TOP").upper()
    grupo = params.get("grupo")
    mxd = MAP.MapDocument("CURRENT")
    try:
        df = mxd.activeDataFrame
        nueva = MAP.Layer(_u(fuente))
        if grupo:
            grp = _get_layer(mxd, df, grupo)
            MAP.AddLayerToGroup(df, grp, nueva, posicion)
        else:
            MAP.AddLayer(df, nueva, posicion)
        arcpy.RefreshTOC()
        arcpy.RefreshActiveView()
        out = {"capa": nueva.name, "fuente": fuente,
               "posicion": posicion, "grupo": grupo}
    finally:
        del mxd
    return out


def h_remove_layer(params):
    """Quita una capa del df activo por nombre."""
    capa = params.get("capa")
    mxd = MAP.MapDocument("CURRENT")
    try:
        df = mxd.activeDataFrame
        lyr = _get_layer(mxd, df, capa)
        MAP.RemoveLayer(df, lyr)
        arcpy.RefreshTOC()
        arcpy.RefreshActiveView()
        out = {"capa": capa, "eliminada": True}
    finally:
        del mxd
    return out


def h_apply_symbology_from_layer(params):
    """Aplica la simbología de un archivo .lyr (estilos canónicos) a una capa."""
    capa = params.get("capa")
    lyr_file = params.get("lyr_file")
    mxd = MAP.MapDocument("CURRENT")
    try:
        df = mxd.activeDataFrame
        lyr = _get_layer(mxd, df, capa)
        origen = MAP.Layer(_u(lyr_file))
        # arcpy.mapping (ArcMap 10.x) NO expone ApplySymbologyFromLayer; la vía
        # correcta es UpdateLayer con symbology_only=True (verificado).
        MAP.UpdateLayer(df, lyr, origen, True)
        arcpy.RefreshTOC()
        arcpy.RefreshActiveView()
        out = {"capa": capa, "lyr_origen": lyr_file}
    finally:
        del mxd
    return out


def h_set_scale(params):
    """Fija la escala del data frame activo (ej. 200000 = 1:200.000) y refresca."""
    escala = params.get("escala")
    mxd = MAP.MapDocument("CURRENT")
    try:
        df = mxd.activeDataFrame
        df.scale = float(escala)
        arcpy.RefreshActiveView()
        out = {"escala": df.scale}
    finally:
        del mxd
    return out


# --------------------------------------------------------------------------- #
# Geoprocesamiento y mantenimiento.
# --------------------------------------------------------------------------- #

def h_save_mxd(params):
    """Guarda el .mxd abierto en su ruta actual."""
    mxd = MAP.MapDocument("CURRENT")
    try:
        mxd.save()
        out = {"guardado": True, "ruta": mxd.filePath}
    finally:
        del mxd
    return out


def h_save_mxd_as(params):
    """Guarda una copia del .mxd en otra ruta (saveACopy; no cambia el doc activo)."""
    salida = params.get("salida")
    if not salida:
        raise ValueError(u"Indica 'salida' (ruta de destino .mxd).")
    if not salida.lower().endswith(".mxd"):
        salida += ".mxd"
    mxd = MAP.MapDocument("CURRENT")
    try:
        mxd.saveACopy(_u(salida))
        out = {"guardado": True, "salida": salida, "origen": mxd.filePath}
    finally:
        del mxd
    return out


def h_list_broken_data_sources(params):
    """Lista capas/tablas con la fuente de datos rota."""
    mxd = MAP.MapDocument("CURRENT")
    try:
        rotos = MAP.ListBrokenDataSources(mxd)
        salida = []
        for lyr in rotos:
            item = {}
            try:
                item["nombre"] = lyr.name
            except Exception:
                item["nombre"] = u"(sin nombre)"
            try:
                item["fuente_rota"] = lyr.dataSource
            except Exception:
                item["fuente_rota"] = None
            try:
                item["workspace"] = lyr.workspacePath
            except Exception:
                item["workspace"] = None
            salida.append(item)
        out = {"num": len(salida), "rotos": salida}
    finally:
        del mxd
    return out


def h_repair_data_source(params):
    """Reapunta el workspace de una capa (ruta_antigua -> ruta_nueva)."""
    capa = params.get("capa")
    ruta_antigua = params.get("ruta_antigua")
    ruta_nueva = params.get("ruta_nueva")
    validar = bool(params.get("validar", True))
    if not (capa and ruta_antigua and ruta_nueva):
        raise ValueError(u"Indica 'capa', 'ruta_antigua' y 'ruta_nueva'.")
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        roto_antes = lyr.isBroken
        aplicado = True
        aviso = None
        try:
            lyr.findAndReplaceWorkspacePath(_u(ruta_antigua), _u(ruta_nueva), validar)
        except Exception, e:  # noqa: E722  (Py2.7)
            # Con validar=True, ArcObjects 10.5 lanza ValueError ("Layer: error
            # inesperado") cuando la ruta nueva no contiene el dataset: su forma
            # de decir "no valida, no aplico". No es un fallo: lo reportamos limpio.
            if validar:
                aplicado = False
                aviso = (u"Ruta nueva no valida (el dataset no existe alli); "
                         u"con validar=True no se aplico el cambio. "
                         u"Detalle: %s" % _u(unicode(e)))
            else:
                raise
        out = {"capa": capa, "roto_antes": roto_antes, "roto_despues": lyr.isBroken,
               "ruta_antigua": ruta_antigua, "ruta_nueva": ruta_nueva,
               "validado": validar, "aplicado": aplicado}
        if aviso:
            out["aviso"] = aviso
    finally:
        del mxd
    return out


def h_run_geoprocessing(params):
    """Ejecuta un geoproceso nominal por nombre + lista de parámetros posicionales.

    'tool' admite forma punteada por toolbox (management.CopyFeatures, analysis.Buffer,
    sa.Slope) o el alias clásico (Buffer_analysis).
    """
    tool = params.get("tool")
    args = params.get("params") or []
    if not tool:
        raise ValueError(u"Indica 'tool' (ej. 'management.CopyFeatures' o 'Buffer_analysis').")
    if u"." in tool:
        modname, toolname = tool.split(u".", 1)
        modulo = getattr(arcpy, modname, None)
        if modulo is None:
            raise ValueError(u"Toolbox/módulo arcpy no encontrado: %s" % _u(modname))
        func = getattr(modulo, toolname, None)
    else:
        func = getattr(arcpy, tool, None)
    if func is None:
        raise ValueError(u"Geoproceso no encontrado en arcpy: %s" % _u(tool))
    # Resolver args string que coincidan con una capa de la TOC al objeto Layer:
    # en el hilo de fondo del puente arcpy NO resuelve nombres de capa por sí solo
    # (ERROR 000732). Pasar el Layer honra ademas su definition query/seleccion.
    # Guard: NO se resuelven strings con pinta de ruta o de SQL (un nombre de
    # campo o keyword que coincida con una capa se sustituiria en silencio).
    # resolver_capas=False desactiva la resolucion por completo.
    _NO_RESOLVER = u"\\/:*?\"<>|='"
    if params.get("resolver_capas", True):
        try:
            mxd = MAP.MapDocument("CURRENT")
            df = mxd.activeDataFrame
            resueltos = []
            for a in args:
                lyr = None
                if isinstance(a, (str, unicode)) and \
                        not any(ch in a for ch in _NO_RESOLVER):
                    try:
                        candidatos = MAP.ListLayers(mxd, a, df)
                        if candidatos:
                            lyr = candidatos[0]
                    except Exception:
                        lyr = None
                resueltos.append(lyr if lyr is not None else a)
            args = resueltos
        except Exception:
            pass  # sin mapa vivo o sin coincidencias: usar args tal cual
    result = func(*args)
    salidas = []
    try:
        for i in range(result.outputCount):
            salidas.append(result.getOutput(i))
    except Exception:
        pass
    return {"tool": tool, "salidas": salidas, "mensajes": arcpy.GetMessages()}


# --------------------------------------------------------------------------- #
# Visualización y datos (feedback visual inline + inspección + multi-data-frame).
# --------------------------------------------------------------------------- #

def h_get_canvas_screenshot(params):
    """Renderiza la vista (o el layout) a PNG y devuelve la imagen EN BASE64.

    El server la convierte en imagen inline para que el agente 'vea' el mapa sin
    abrir un archivo (paridad con get_canvas_screenshot de QGIS). modo='vista'
    (data frame activo) o 'layout' (página completa). dpi bajo por defecto para
    payload pequeño.
    """
    modo = (params.get("modo") or "vista").lower()
    dpi = int(params.get("dpi", 96))
    mxd = MAP.MapDocument("CURRENT")
    try:
        fd, tmp = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            if modo == "layout":
                MAP.ExportToPNG(mxd, tmp, resolution=dpi)
            else:
                MAP.ExportToPNG(mxd, tmp, mxd.activeDataFrame, resolution=dpi)
            f = open(tmp, "rb")
            try:
                raw = f.read()
            finally:
                f.close()
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
        out = {"modo": modo, "dpi": dpi, "bytes": len(raw),
               "imagen_b64": base64.b64encode(raw)}
    finally:
        del mxd
    return out


def h_get_layer_features(params):
    """Devuelve filas de atributos de una capa (respeta def. query y selección).

    'campos' = lista de campos (None = todos salvo geometría). 'where' opcional.
    'limite' = máximo de filas (50 por defecto) para no inflar el payload.
    """
    capa = params.get("capa")
    where = params.get("where")
    campos = params.get("campos")
    limite = int(params.get("limite", 50))
    mxd = MAP.MapDocument("CURRENT")
    try:
        lyr = _get_layer(mxd, mxd.activeDataFrame, capa)
        if campos:
            field_names = [_u(c) for c in campos]
        else:
            field_names = [f.name for f in arcpy.ListFields(lyr)
                           if f.type not in ("Geometry", "Blob", "Raster")]
        filas = []
        cur = arcpy.da.SearchCursor(lyr, field_names,
                                    where_clause=_u(where) if where else None)
        try:
            for i, r in enumerate(cur):
                if i >= limite:
                    break
                vals = [(_u(v) if isinstance(v, (str, unicode)) else v) for v in r]
                filas.append(dict(zip(field_names, vals)))
        finally:
            del cur
        out = {"capa": capa, "campos": field_names, "num": len(filas),
               "limite": limite, "filas": filas}
    finally:
        del mxd
    return out


def h_describe_data(params):
    """Describe un dataset en disco (tipo, CRS, geometría, extent, campos)."""
    ruta = params.get("ruta")
    if not ruta:
        raise ValueError(u"Indica 'ruta' (dataset en disco).")
    d = arcpy.Describe(_u(ruta))
    out = {"ruta": ruta}
    for attr in ("dataType", "baseName", "shapeType", "datasetType"):
        out[attr] = getattr(d, attr, None)
    try:
        sr = d.spatialReference
        out["crs"] = sr.name if sr else None
        out["crs_code"] = sr.factoryCode if sr else None
    except Exception:
        out["crs"] = None
    try:
        out["campos"] = [{"nombre": f.name, "tipo": f.type}
                         for f in arcpy.ListFields(_u(ruta))]
    except Exception:
        pass
    try:
        ext = d.extent
        if ext:
            out["extent"] = {"xmin": ext.XMin, "ymin": ext.YMin,
                             "xmax": ext.XMax, "ymax": ext.YMax}
    except Exception:
        pass
    return out


def h_list_data_frames(params):
    """Lista los data frames del .mxd (nombre, escala, cuál es el activo)."""
    mxd = MAP.MapDocument("CURRENT")
    try:
        dfs = MAP.ListDataFrames(mxd)
        activo = mxd.activeDataFrame.name
        out = {"num": len(dfs), "activo": activo,
               "data_frames": [{"nombre": d.name, "escala": d.scale,
                                "activo": d.name == activo} for d in dfs]}
    finally:
        del mxd
    return out


def h_set_active_df(params):
    """Fija el data frame activo por nombre y refresca."""
    nombre = params.get("nombre")
    mxd = MAP.MapDocument("CURRENT")
    try:
        dfs = MAP.ListDataFrames(mxd, _u(nombre)) if nombre else []
        if not dfs:
            disponibles = [d.name for d in MAP.ListDataFrames(mxd)]
            raise ValueError(u"Data frame no encontrado: %s. Disponibles: %s"
                             % (_u(nombre), u", ".join(disponibles)))
        mxd.activeView = dfs[0].name
        arcpy.RefreshActiveView()
        out = {"activo": dfs[0].name}
    finally:
        del mxd
    return out


def h_set_extent(params):
    """Encuadra el data frame activo a unas coords [xmin,ymin,xmax,ymax] o a una capa
    (extent de su selección si la hay; si no, extent total de la capa)."""
    coords = params.get("coords")
    capa = params.get("capa")
    mxd = MAP.MapDocument("CURRENT")
    try:
        df = mxd.activeDataFrame
        if coords:
            ext = arcpy.Extent(float(coords[0]), float(coords[1]),
                               float(coords[2]), float(coords[3]))
        elif capa:
            lyr = _get_layer(mxd, df, capa)
            ext = None
            try:
                ext = lyr.getSelectedExtent(False)
            except Exception:
                ext = None
            if ext is None:
                try:
                    ext = lyr.getExtent()
                except Exception:
                    ext = arcpy.Describe(lyr).extent
        else:
            raise ValueError(u"Indica 'coords' [xmin,ymin,xmax,ymax] o 'capa'.")
        df.extent = ext
        arcpy.RefreshActiveView()
        e = df.extent
        out = {"extent": {"xmin": e.XMin, "ymin": e.YMin,
                          "xmax": e.XMax, "ymax": e.YMax}, "escala": df.scale}
    finally:
        del mxd
    return out


# --------------------------------------------------------------------------- #
# Catálogo y workspace (paridad con ArcGIS Pro MCP).
# --------------------------------------------------------------------------- #

def _listar_en_workspace(ws, fn):
    """Ejecuta fn() con arcpy.env.workspace fijado a ws, restaurando el anterior."""
    old = arcpy.env.workspace
    try:
        if ws:
            arcpy.env.workspace = _u(ws)
        if not arcpy.env.workspace:
            raise ValueError(u"Define 'workspace' o fíjalo antes con set_workspace.")
        return arcpy.env.workspace, fn()
    finally:
        arcpy.env.workspace = old


def h_get_workspace(params):
    """Devuelve el workspace y scratch workspace actuales de arcpy.env."""
    return {"workspace": arcpy.env.workspace,
            "scratchWorkspace": arcpy.env.scratchWorkspace}


def h_set_workspace(params):
    """Fija arcpy.env.workspace (gdb/carpeta de trabajo para listados y geoprocesos)."""
    ws = params.get("workspace")
    if not ws:
        raise ValueError(u"Indica 'workspace' (ruta a gdb o carpeta).")
    arcpy.env.workspace = _u(ws)
    return {"workspace": arcpy.env.workspace}


def h_list_feature_classes(params):
    """Lista feature classes del workspace (incluye las de datasets)."""
    def _fn():
        fcs = list(arcpy.ListFeatureClasses() or [])
        for ds in (arcpy.ListDatasets("", "Feature") or []):
            for fc in (arcpy.ListFeatureClasses("", "", ds) or []):
                fcs.append(ds + "/" + fc)
        return fcs
    ws, fcs = _listar_en_workspace(params.get("workspace"), _fn)
    return {"workspace": ws, "num": len(fcs), "feature_classes": fcs}


def h_list_tables(params):
    """Lista tablas independientes del workspace."""
    ws, tbls = _listar_en_workspace(params.get("workspace"),
                                    lambda: list(arcpy.ListTables() or []))
    return {"workspace": ws, "num": len(tbls), "tablas": tbls}


def h_list_rasters(params):
    """Lista datasets ráster del workspace."""
    ws, rs = _listar_en_workspace(params.get("workspace"),
                                  lambda: list(arcpy.ListRasters() or []))
    return {"workspace": ws, "num": len(rs), "rasters": rs}


# --------------------------------------------------------------------------- #
# Análisis ambiental y teledetección (raster, hidrología, terreno).
#
# Son GEOPROCESOS PESADOS: corren en el hilo principal de ArcMap (como todo en
# este puente), así que CONGELAN la GUI mientras duran — igual que si los
# lanzaras a mano. El servidor MCP usa un timeout amplio (ARCMAP_GP_TIMEOUT,
# 30 min por defecto) para no cortar la espera. Varios requieren las extensiones
# Spatial Analyst o 3D Analyst (se intentan activar y se liberan al terminar).
# Operan sobre datos EN DISCO (rutas a ráster/feature class), no sobre la TOC.
# --------------------------------------------------------------------------- #

def _checkout(ext):
    """Activa una extensión (Spatial/3D). Error accionable si no hay licencia."""
    try:
        estado = arcpy.CheckExtension(ext)
    except Exception:
        estado = u"(desconocido)"
    if estado != "Available":
        raise ValueError(u"Extensión '%s' no disponible (estado: %s). Actívala en "
                         u"Customize > Extensions de ArcMap." % (_u(ext), _u(estado)))
    arcpy.CheckOutExtension(ext)


def _add_to_map(ruta):
    """Añade el resultado al data frame activo (best-effort: nunca rompe la tool)."""
    try:
        mxd = MAP.MapDocument("CURRENT")
        df = mxd.activeDataFrame
        MAP.AddLayer(df, MAP.Layer(_u(ruta)), "TOP")
        arcpy.RefreshTOC()
        arcpy.RefreshActiveView()
        del mxd
        return True
    except Exception:
        return False


# Roles de banda que necesita cada índice (también sirve de ayuda/validación).
# Mapa banda física -> rol, por sensor (documentado para el usuario en TOOLS.md):
#   ROL      | Sentinel-2 | Landsat 8-9 (OLI) | Landsat 4-7 (TM/ETM+)
#   BLUE     | B2         | B2                | B1
#   GREEN    | B3         | B3                | B2
#   RED      | B4         | B4                | B3
#   REDEDGE  | B5         | (no tiene)        | (no tiene)
#   NIR      | B8 / B8A   | B5                | B4
#   SWIR1    | B11        | B6                | B5
#   SWIR2    | B12        | B7                | B7
_INDICE_BANDAS = {
    "NDVI":  ("NIR", "RED"),            # vegetación (verdor)
    "GNDVI": ("NIR", "GREEN"),          # vegetación, sensible a clorofila
    "NDRE":  ("NIR", "REDEDGE"),        # red-edge (solo S2): estrés/vigor
    "NDWI":  ("GREEN", "NIR"),          # agua superficial (McFeeters)
    "MNDWI": ("GREEN", "SWIR1"),        # agua mejorado (Xu)
    "NDMI":  ("NIR", "SWIR1"),          # humedad de la vegetación
    "NBR":   ("NIR", "SWIR2"),          # área quemada / severidad de incendio
    "SAVI":  ("NIR", "RED"),            # vegetación corregido por suelo (L)
    "EVI":   ("NIR", "RED", "BLUE"),    # vegetación mejorado (alta biomasa)
}


def h_raster_index(params):
    """Calcula un índice espectral con nombre desde bandas ráster (Spatial Analyst).

    'indice' (uno de): NDVI, GNDVI, NDRE, NDWI, MNDWI, NDMI, NBR, SAVI, EVI.
    'bandas' = dict {ROL: ruta_raster} con los roles que el índice necesite. Roles:
    BLUE, GREEN, RED, REDEDGE, NIR, SWIR1, SWIR2. La correspondencia rol->banda por
    sensor (S2 / Landsat) está en la cabecera _INDICE_BANDAS y en docs/TOOLS.md.
    SAVI admite 'L' (factor de suelo, 0.5 por defecto). Para un índice arbitrario:
    indice='CUSTOM' + banda_a/banda_b -> (banda_a - banda_b)/(banda_a + banda_b).
    """
    indice = (params.get("indice") or u"").upper()
    bandas = params.get("bandas") or {}
    salida = params.get("salida")
    banda_a = params.get("banda_a")
    banda_b = params.get("banda_b")
    if not salida:
        raise ValueError(u"Indica 'salida' (ruta del ráster de índice).")
    _checkout("Spatial")
    try:
        from arcpy.sa import Raster, Float

        def R(rol):
            ruta = bandas.get(rol)
            if not ruta:
                req = _INDICE_BANDAS.get(indice, ())
                raise ValueError(u"Falta la banda '%s' para %s (requiere: %s). "
                                 u"Pásala en 'bandas', ej. {'NIR': ruta, 'RED': ruta}."
                                 % (_u(rol), _u(indice), u", ".join(req)))
            return Float(Raster(_u(ruta)))

        if indice == "NDVI":
            nir, red = R("NIR"), R("RED"); idx = (nir - red) / (nir + red)
        elif indice == "GNDVI":
            nir, grn = R("NIR"), R("GREEN"); idx = (nir - grn) / (nir + grn)
        elif indice == "NDRE":
            nir, re = R("NIR"), R("REDEDGE"); idx = (nir - re) / (nir + re)
        elif indice == "NDWI":
            grn, nir = R("GREEN"), R("NIR"); idx = (grn - nir) / (grn + nir)
        elif indice == "MNDWI":
            grn, s1 = R("GREEN"), R("SWIR1"); idx = (grn - s1) / (grn + s1)
        elif indice == "NDMI":
            nir, s1 = R("NIR"), R("SWIR1"); idx = (nir - s1) / (nir + s1)
        elif indice == "NBR":
            nir, s2 = R("NIR"), R("SWIR2"); idx = (nir - s2) / (nir + s2)
        elif indice == "SAVI":
            L = float(params.get("L", 0.5))
            nir, red = R("NIR"), R("RED")
            idx = ((nir - red) / (nir + red + L)) * (1 + L)
        elif indice == "EVI":
            nir, red, blue = R("NIR"), R("RED"), R("BLUE")
            idx = 2.5 * ((nir - red) / (nir + 6 * red - 7.5 * blue + 1))
        elif indice in (u"", u"CUSTOM"):
            if not (banda_a and banda_b):
                raise ValueError(u"Índice genérico: indica banda_a y banda_b "
                                 u"(o usa 'indice' con un nombre conocido).")
            a = Float(Raster(_u(banda_a))); b = Float(Raster(_u(banda_b)))
            idx = (a - b) / (a + b)
            indice = u"CUSTOM"
        else:
            raise ValueError(u"Índice no soportado: %s. Disponibles: %s, CUSTOM."
                             % (_u(indice), u", ".join(sorted(_INDICE_BANDAS))))
        idx.save(_u(salida))
    finally:
        arcpy.CheckInExtension("Spatial")
    out = {"indice": indice, "salida": salida}
    if params.get("anadir_al_mapa", True):
        out["anadida_al_mapa"] = _add_to_map(salida)
    return out


def h_hydrology(params):
    """Hidrología sobre un MDT (Spatial Analyst). 'operacion' + 'parametros':

      cuenca       -> Fill > FlowDirection > FlowAccumulation y delimita cuencas.
                      {mdt, salida_dir, salida, pour_points?, snap_dist?}
                      Con pour_points: Watershed (snap opcional). Sin ellos: Basin.
      red_drenaje  -> red de drenaje por umbral de acumulación.
                      {mdt | fdir+facc, umbral, salida}
      inundacion   -> cota de inundación simple (MDT <= nivel).
                      {mdt, nivel, salida}
    """
    op = (params.get("operacion") or u"").lower()
    p = params.get("parametros") or {}
    _checkout("Spatial")
    try:
        from arcpy.sa import (Raster, Fill, FlowDirection, FlowAccumulation,
                              Watershed, Basin, Con, StreamToFeature, SnapPourPoint)
        res = {"operacion": op}
        if op == "cuenca":
            out_dir = p["salida_dir"]
            if not os.path.isdir(out_dir):
                os.makedirs(out_dir)
            relleno = Fill(Raster(_u(p["mdt"])))
            fdir = FlowDirection(relleno)
            facc = FlowAccumulation(fdir)
            fdir.save(os.path.join(_u(out_dir), "fdir"))
            facc.save(os.path.join(_u(out_dir), "facc"))
            if p.get("pour_points"):
                pp = _u(p["pour_points"])
                if p.get("snap_dist"):
                    pp = SnapPourPoint(pp, facc, float(p["snap_dist"]))
                cuencas = Watershed(fdir, pp)
            else:
                cuencas = Basin(fdir)
            cuencas.save(_u(p["salida"]))
            res["salida"] = p["salida"]
            res["fdir"] = os.path.join(_u(out_dir), "fdir")
            res["facc"] = os.path.join(_u(out_dir), "facc")
        elif op == "red_drenaje":
            if p.get("fdir") and p.get("facc"):
                fdir = Raster(_u(p["fdir"]))
                facc = Raster(_u(p["facc"]))
            else:
                fdir = FlowDirection(Fill(Raster(_u(p["mdt"]))))
                facc = FlowAccumulation(fdir)
            rios = Con(facc > float(p["umbral"]), 1)
            StreamToFeature(rios, fdir, _u(p["salida"]), "NO_SIMPLIFY")
            res["salida"] = p["salida"]
        elif op == "inundacion":
            agua = Con(Raster(_u(p["mdt"])) <= float(p["nivel"]), 1)
            agua.save(_u(p["salida"]))
            res["salida"] = p["salida"]
        else:
            raise ValueError(u"Operación hidrológica no soportada: %s "
                             u"(usa cuenca | red_drenaje | inundacion)." % _u(op))
        res["mensajes"] = arcpy.GetMessages()
    finally:
        arcpy.CheckInExtension("Spatial")
    if params.get("anadir_al_mapa", True) and res.get("salida"):
        res["anadida_al_mapa"] = _add_to_map(res["salida"])
    return res


def h_contours(params):
    """Curvas de nivel desde un MDT (3D Analyst). Opcional: exportar también a DXF."""
    mdt = params.get("mdt")
    salida = params.get("salida")
    intervalo = params.get("intervalo")
    base = params.get("base", 0)
    dxf = params.get("dxf")
    if not (mdt and salida and intervalo is not None):
        raise ValueError(u"Indica 'mdt', 'salida' e 'intervalo' (equidistancia).")
    _checkout("3D")
    try:
        arcpy.ddd.Contour(_u(mdt), _u(salida), float(intervalo), float(base))
        out = {"salida": salida, "intervalo": intervalo, "base": base}
        if dxf:
            arcpy.conversion.ExportCAD([_u(salida)], "DXF_R2010", _u(dxf))
            out["dxf"] = dxf
    finally:
        arcpy.CheckInExtension("3D")
    if params.get("anadir_al_mapa", True):
        out["anadida_al_mapa"] = _add_to_map(salida)
    return out


def h_topographic_profile(params):
    """Perfil topográfico: interpola una línea 2D sobre una superficie (MDT/TIN) y
    devuelve una línea 3D con la Z del terreno (3D Analyst, InterpolateShape)."""
    superficie = params.get("superficie")
    lineas = params.get("lineas")
    salida = params.get("salida")
    if not (superficie and lineas and salida):
        raise ValueError(u"Indica 'superficie' (ráster/TIN), 'lineas' (2D) y 'salida'.")
    _checkout("3D")
    try:
        arcpy.ddd.InterpolateShape(_u(superficie), _u(lineas), _u(salida))
    finally:
        arcpy.CheckInExtension("3D")
    out = {"salida": salida, "superficie": superficie, "lineas": lineas}
    if params.get("anadir_al_mapa", True):
        out["anadida_al_mapa"] = _add_to_map(salida)
    return out


def h_least_cost_path(params):
    """Ruta de mínimo coste (Spatial Analyst): CostDistance(origen, coste) y luego
    CostPath(destino). 'coste' = ráster de fricción; 'origen'/'destino' = features o
    ráster. La salida es un ráster con la ruta óptima."""
    coste = params.get("coste")
    origen = params.get("origen")
    destino = params.get("destino")
    salida = params.get("salida")
    salida_dir = params.get("salida_dir")
    if not (coste and origen and destino and salida):
        raise ValueError(u"Indica 'coste' (ráster de fricción), 'origen', 'destino' y 'salida'.")
    _checkout("Spatial")
    try:
        from arcpy.sa import CostDistance, CostPath, Raster
        out_dir = salida_dir or os.path.dirname(_u(salida)) or arcpy.env.scratchFolder
        backlink = os.path.join(_u(out_dir), "lcp_backlink")
        cdist = CostDistance(_u(origen), _u(coste), out_backlink_raster=backlink)
        lcp = CostPath(_u(destino), cdist, Raster(backlink), "EACH_CELL")
        lcp.save(_u(salida))
        out = {"salida": salida, "backlink": backlink}
    finally:
        arcpy.CheckInExtension("Spatial")
    if params.get("anadir_al_mapa", True):
        out["anadida_al_mapa"] = _add_to_map(salida)
    return out


def h_calculate_geometry(params):
    """Calcula atributos de geometría sobre una capa/feature class IN PLACE
    (AddGeometryAttributes, ArcMap 10.2+). NO requiere extensión.

    'entrada' = nombre de capa de la TOC (se resuelve) o ruta a feature class.
    'propiedades' = lista o cadena separada por ';' con una o varias de:
      AREA, AREA_GEODESIC, PERIMETER_LENGTH, LENGTH, LENGTH_GEODESIC, CENTROID,
      CENTROID_INSIDE, POINT_X_Y_Z_M, EXTENT, LINE_BEARING, LINE_START_MID_END...
    'unidad_longitud' / 'unidad_area' opcionales (ej. METERS, SQUARE_METERS).
    """
    entrada = params.get("entrada") or params.get("capa")
    propiedades = params.get("propiedades")
    unidad_longitud = params.get("unidad_longitud", u"")
    unidad_area = params.get("unidad_area", u"")
    crs = params.get("crs", u"")
    if not (entrada and propiedades):
        raise ValueError(u"Indica 'entrada' (capa o feature class) y 'propiedades'.")
    if isinstance(propiedades, (list, tuple)):
        props = u";".join(_u(x) for x in propiedades)
    else:
        props = _u(propiedades)
    # Resolver nombre de capa de la TOC -> objeto Layer (honra def. query/selección).
    objetivo = _u(entrada)
    try:
        mxd = MAP.MapDocument("CURRENT")
        cand = MAP.ListLayers(mxd, _u(entrada), mxd.activeDataFrame)
        if cand:
            objetivo = cand[0]
        del mxd
    except Exception:
        pass
    arcpy.AddGeometryAttributes_management(objetivo, props, _u(unidad_longitud),
                                           _u(unidad_area), _u(crs) if crs else u"")
    return {"entrada": entrada, "propiedades": props,
            "unidad_longitud": unidad_longitud, "unidad_area": unidad_area}


HANDLERS = {
    "echo": h_echo,
    "ping": h_ping,
    "get_arcmap_info": h_get_arcmap_info,
    "list_layers": h_list_layers,
    "zoom_to_layer": h_zoom_to_layer,
    "export_pdf": h_export_pdf,
    "refresh": h_refresh,
    "execute_code": h_execute_code,
    # Series de planos
    "list_ddp": h_list_ddp,
    "export_ddp": h_export_ddp,
    "list_layout_elements": h_list_layout_elements,
    "set_text_element": h_set_text_element,
    "goto_ddp_page": h_goto_ddp_page,
    "set_definition_query": h_set_definition_query,
    "set_layer_visibility": h_set_layer_visibility,
    "export_view_png": h_export_view_png,
    # Capas y datos
    "select_by_attribute": h_select_by_attribute,
    "clear_selection": h_clear_selection,
    "get_unique_values": h_get_unique_values,
    "count_features": h_count_features,
    "list_fields": h_list_fields,
    "get_layer_info": h_get_layer_info,
    "add_layer": h_add_layer,
    "remove_layer": h_remove_layer,
    "apply_symbology_from_layer": h_apply_symbology_from_layer,
    "set_scale": h_set_scale,
    # Geoprocesamiento y mantenimiento
    "save_mxd": h_save_mxd,
    "save_mxd_as": h_save_mxd_as,
    "list_broken_data_sources": h_list_broken_data_sources,
    "repair_data_source": h_repair_data_source,
    "run_geoprocessing": h_run_geoprocessing,
    # Visualización y datos
    "get_canvas_screenshot": h_get_canvas_screenshot,
    "get_layer_features": h_get_layer_features,
    "describe_data": h_describe_data,
    "list_data_frames": h_list_data_frames,
    "set_active_df": h_set_active_df,
    "set_extent": h_set_extent,
    # Catálogo y workspace
    "get_workspace": h_get_workspace,
    "set_workspace": h_set_workspace,
    "list_feature_classes": h_list_feature_classes,
    "list_tables": h_list_tables,
    "list_rasters": h_list_rasters,
    # Análisis ambiental (geoprocesos pesados; timeout amplio en el server)
    "raster_index": h_raster_index,
    "hydrology": h_hydrology,
    "contours": h_contours,
    "topographic_profile": h_topographic_profile,
    "least_cost_path": h_least_cost_path,
    "calculate_geometry": h_calculate_geometry,
}


def _dispatch(command):
    ctype = command.get("type")
    params = command.get("params", {}) or {}
    handler = HANDLERS.get(ctype)
    if handler is None:
        return {"ok": False, "error": u"Comando desconocido: %s" % _u(ctype)}
    try:
        return {"ok": True, "result": handler(params)}
    except Exception as e:
        return {"ok": False,
                "error": _u(getattr(e, "message", None) or str(e)),
                "traceback": _u(traceback.format_exc())}


# --------------------------------------------------------------------------- #
# Sondeo del socket (no bloqueante) — corre en el hilo principal vía SetTimer.
# --------------------------------------------------------------------------- #

def _close_client(item):
    try:
        _server["clients"].remove(item)
    except ValueError:
        pass
    try:
        item[0].close()
    except Exception:
        pass


def _pump():
    srv = _server.get("sock")
    if srv is None:
        return
    # 1) aceptar conexiones pendientes (no bloqueante)
    while True:
        try:
            conn, _addr = srv.accept()
        except socket.error:
            break
        conn.setblocking(False)
        _server["clients"].append([conn, b""])
    # 2) servir clientes con datos
    for item in list(_server["clients"]):
        conn = item[0]
        # Drenar TODO lo disponible en este tick: leer solo 8 KB por tick
        # hacía que un comando grande tardara ~100 ms por cada 8 KB en llegar.
        cerrado = False
        recibido = False
        while True:
            try:
                data = conn.recv(65536)
            except socket.error as e:
                code = e.args[0] if e.args else None
                if code not in _WOULDBLOCK:
                    _close_client(item)
                    cerrado = True
                break
            if not data:
                _close_client(item)
                cerrado = True
                break
            item[1] += data
            recibido = True
        if cerrado or not recibido:
            continue
        try:
            command = json.loads(item[1].decode("utf-8"))
        except ValueError:
            continue  # JSON incompleto (o multibyte cortado), esperar al siguiente tick
        item[1] = b""
        response = _dispatch(command)            # <-- arcpy en el hilo principal
        try:
            conn.settimeout(30)                  # un cliente muerto no congela ArcMap
            conn.sendall(json.dumps(response, ensure_ascii=False,
                                    default=_u).encode("utf-8"))
        except Exception:
            pass
        _close_client(item)                      # un comando por conexión


def _tick(hwnd, msg, id_event, dw_time):
    # Nunca dejar que una excepción rompa el message loop de ArcMap.
    # Guard de reentrada: un geoproceso largo dentro de _dispatch bombea
    # mensajes de Windows (diálogo de progreso), que re-entregan WM_TIMER y
    # ejecutarían un SEGUNDO comando arcpy a mitad del primero.
    if _server.get("busy"):
        return
    _server["busy"] = True
    try:
        _pump()
    except Exception:
        try:
            sys.stderr.write(traceback.format_exc())
        except Exception:
            pass
    finally:
        _server["busy"] = False


# --------------------------------------------------------------------------- #
# Arranque / parada
# --------------------------------------------------------------------------- #

def start():
    if _server.get("running"):
        print("[arcmap-bridge] ya activo en %s:%s (stop() para reiniciar)" % (HOST, PORT))
        return
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # En Windows SO_REUSEADDR permite que un SEGUNDO proceso se ate al mismo
    # puerto y robe conexiones de forma no determinista (p. ej. dos ArcMap con
    # el puente arrancado). SO_EXCLUSIVEADDRUSE hace que el segundo bind falle
    # con error claro. En otros SO el atributo no existe: se omite.
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    except AttributeError:
        pass
    try:
        srv.bind((HOST, PORT))
        srv.listen(5)
    except Exception as e:
        try:
            srv.close()
        except Exception:
            pass
        print("[arcmap-bridge] ERROR abriendo socket %s:%s -> %s" % (HOST, PORT, e))
        raise
    srv.setblocking(False)
    _server["sock"] = srv
    _server["clients"] = []
    _server["running"] = True
    # Timer en el hilo principal de ArcMap (modelo QGIS, sin hilo de fondo).
    cb = TIMERPROC(_tick)
    tid = user32.SetTimer(None, 0, 100, cb)
    _server["timerproc"] = cb          # mantener viva la referencia (no GC)
    _server["timer_id"] = tid
    if not tid:
        print("[arcmap-bridge] AVISO: SetTimer devolvió 0 (el sondeo no arrancará).")
    print("[arcmap-bridge] activo en %s:%s (SetTimer id=%s, hilo principal)" % (HOST, PORT, tid))
    print("[arcmap-bridge] deja ArcMap abierto. Para parar: stop()")


def stop():
    _server["running"] = False
    tid = _server.get("timer_id")
    if tid:
        try:
            user32.KillTimer(None, tid)
        except Exception:
            pass
    _server["timer_id"] = None
    _server["timerproc"] = None
    for item in list(_server.get("clients") or []):
        _close_client(item)
    try:
        _server["sock"].close()
    except Exception:
        pass
    _server["sock"] = None
    print("[arcmap-bridge] detenido")


# Arranque automático solo si se ejecuta directamente (execfile en la ventana
# de Python). Si lo importa el Add-In como módulo, NO arranca solo.
if __name__ == "__main__":
    start()
