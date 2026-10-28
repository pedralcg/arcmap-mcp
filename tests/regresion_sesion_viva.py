# -*- coding: utf-8 -*-
"""regresion_sesion_viva.py  --  Barrido MANUAL del catalogo contra ArcMap vivo.

NO es una suite automatica (esa es test_client_protocol.py, que corre sin ArcMap):
esto EXIGE ArcMap abierto con el puente arrancado, y por eso no lleva prefijo
"test_" ni lo recoge `unittest discover`.

Uso:  python tests/regresion_sesion_viva.py     (sale 1 si algo falla)

Para que sirve: despues de tocar el add-in y reinstalarlo, confirmar que no se ha
roto nada por el camino. Cubre unas 70 comprobaciones; el ultimo bloque, "revision
2026-09-20", es ademas la PRIMERA prueba real de unos arreglos que se escribieron
con ArcMap cerrado.

Solo tools de LECTURA o de efecto acotado y reversible sobre un documento de
pruebas: nada de exports masivos, nada de guardar el .mxd, nada de tocar datos.

QUE DOCUMENTO ABRIR: una COPIA recien hecha de un .mxd de pruebas sin las capas de
los fixtures (p. ej. C:\\temp\\2026-09-25_etiquetas\\maplex_ligero.mxd, que ademas
usa Maplex). NO `copia_regresion.mxd`: es una SALIDA de este script (save_mxd_as),
trae ya real_NUEVO.tif y el script la borra al final. Abrirla da una cascada de
falsos fallos por capas duplicadas (2026-10-01).
El objetivo es detectar si algo se rompio al reinstalar el add-in cinco veces.
"""
import json
import os
import socket
import sys

# Con la salida redirigida a un fichero, Windows usa cp1252 y el primer «≤» de una
# etiqueta tumbaba el barrido a medias, dejando el documento de pruebas tocado (2026-10-10).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Mismas variables que el servidor y el add-in (ver README, "Cambiar el puerto").
HOST = os.environ.get("ARCMAP_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ARCMAP_BRIDGE_PORT", "27179"))

SEP = chr(92)
RASTER = "C:" + SEP + "temp" + SEP + "20260715_verif_qml2lyr" + SEP + "real_NUEVO.tif"
VEC = ("C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP
       + "Áreas Naturales de Interés Turístico.shp")
NOMBRE_VEC = "Áreas Naturales de Interés Turístico"

# Raster CATEGORICO (mascara de visibilidad 0/1/2 con NoData 255) y su .lyr de
# valores unicos: el caso que el modo clasificado no puede cubrir.
#
# Todos los fixtures viven bajo C:\temp a proposito: este repo es PUBLICO y aqui habia
# una ruta con el nombre de un cliente y el arbol de carpetas de la empresa. Si cambias
# de datos de prueba, copialos a C:\temp y apunta aqui; no pongas rutas de proyecto.
VIS_DIR = "C:" + SEP + "temp" + SEP + "20260911_lyr_visibilidad_rcd"
VIS_TIF = VIS_DIR + SEP + "Vis_plataformas_acumulado_sin_pantalla.tif"
VIS = "Vis_plataformas_acumulado_sin_pantalla.tif"
LYR_VIS = VIS_DIR + SEP + "Vis_plataformas_acumulado_sin_pantalla.lyr"
LYR_GRUPO = VIS_DIR + SEP + "05_Analisis_de_visibilidad.lyr"

ok_n = 0
fallos = []


REINTENTOS_DIBUJO = []  # (comando, reintentos) de los export que llegaron con el mapa dibujando


def enviar(tipo, params=None, timeout=180):
    """Un comando al puente. Los export que vuelven con "dibujando: " (E_PENDING) se
    reintentan cada 3 s, igual que hace el servidor MCP (_exportar): este script habla
    al socket sin pasar por el, y sin esto contaria como fallo lo que el servidor
    resuelve solo. Se anota cuantas veces pasa: es el E_PENDING intermitente."""
    import time
    for reintento in range(40):
        r = _enviar_una(tipo, params, timeout)
        if r.get("ok") or not str(r.get("error", "")).startswith("dibujando: "):
            if reintento:
                REINTENTOS_DIBUJO.append((tipo, reintento))
            return r
        time.sleep(3)
    return r


def _enviar_una(tipo, params, timeout):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((HOST, PORT))
    s.sendall(json.dumps({"type": tipo, "params": params or {}}).encode("utf-8"))
    buf = b""
    while True:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.decode("utf-8"))


def t(tipo, params=None, espera_ok=True, nota=""):
    global ok_n
    try:
        r = enviar(tipo, params)
    except Exception as exc:
        fallos.append((tipo, "EXCEPCION: " + str(exc)[:90]))
        sys.stdout.write("  [EXC] " + tipo + chr(10))
        return None
    real = bool(r.get("ok"))
    bien = (real == espera_ok)
    if bien:
        ok_n += 1
    else:
        fallos.append((tipo, str(r.get("error", r))[:110]))
    sys.stdout.write("  [%s] %-30s %s%s" % ("OK " if bien else "FALLO", tipo, nota, chr(10)))
    return r


sys.stdout.write("--- preparar documento de pruebas ---" + chr(10))
t("add_layer", {"fuente": RASTER})
t("add_layer", {"fuente": VEC})

sys.stdout.write("--- estado y catalogo ---" + chr(10))
t("ping")
t("get_arcmap_info")
t("list_layers")
t("list_data_frames")
t("get_workspace")
t("list_layout_elements")

sys.stdout.write("--- consultas sobre capa vectorial ---" + chr(10))
t("list_fields", {"capa": NOMBRE_VEC})
t("get_layer_info", {"capa": NOMBRE_VEC})
t("count_features", {"capa": NOMBRE_VEC})
t("get_unique_values", {"capa": NOMBRE_VEC, "campo": "Municipio"})
t("get_layer_features", {"capa": NOMBRE_VEC, "limite": 3})
t("select_by_attribute", {"capa": NOMBRE_VEC, "where": "1=1"})
t("clear_selection", {"capa": NOMBRE_VEC})
t("describe_data", {"ruta": VEC})

sys.stdout.write("--- simbologia (las nuevas y la vieja) ---" + chr(10))
t("set_unique_values_symbology", {"capa": NOMBRE_VEC, "campo": "Municipio"})
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "num_clases": 4})
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "estirado"})

sys.stdout.write("--- raster categorico y .lyr (2.11.0) ---" + chr(10))
# Un raster categorico NO se clasifica: 'unico' es su unica via, y el valor de
# fondo va en 'transparentes' o tapa el mapa.
t("add_layer", {"fuente": VIS_TIF, "nombre": "Acumulado sin pantalla"}, nota="(add_layer con nombre)")
t("set_raster_symbology", {"capa": "Acumulado sin pantalla", "modo": "unico", "valores": [1, 2],
                           "colores": [[255, 176, 0], [122, 0, 0]],
                           "etiquetas": ["una", "las dos"],
                           "transparentes": [0], "transparencia": 30})
t("apply_symbology_from_layer", {"capa": "Acumulado sin pantalla", "lyr_file": LYR_VIS},
  nota="(.lyr sobre capa RASTER)")
t("add_layer", {"fuente": LYR_GRUPO}, nota="(.lyr de GRUPO)")
t("remove_layer", {"capa": "5. Análisis de visibilidad"})
t("remove_layer", {"capa": "Acumulado sin pantalla"})
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "unico"},
  espera_ok=False, nota="(modo unico sin 'valores')")

sys.stdout.write("--- marcadores ---" + chr(10))
t("add_bookmark", {"nombre": "regresion"})
t("get_bookmarks")
t("goto_bookmark", {"nombre": "regresion"})
t("remove_bookmark", {"nombre": "regresion"})

sys.stdout.write("--- navegacion y visibilidad ---" + chr(10))
t("set_layer_visibility", {"capa": "real_NUEVO.tif", "visible": False})
t("set_layer_visibility", {"capa": "real_NUEVO.tif", "visible": True})
t("set_scale", {"escala": 50000})
t("refresh")

sys.stdout.write("--- arcpy fuera de proceso ---" + chr(10))
t("execute_code", {"code": "RESULT = 2 + 2", "usar_documento": False})

sys.stdout.write("--- errores que DEBEN fallar bien ---" + chr(10))
t("list_fields", {"capa": "no_existe"}, espera_ok=False, nota="(capa inexistente)")
t("set_raster_symbology", {"capa": NOMBRE_VEC}, espera_ok=False, nota="(raster sobre vectorial)")
t("comando_inventado", {}, espera_ok=False, nota="(comando desconocido)")

def comprobar(etiqueta, condicion, detalle=""):
    """Comprobacion sobre el CONTENIDO de una respuesta, no solo sobre su `ok`."""
    global ok_n
    if condicion:
        ok_n += 1
    else:
        fallos.append((etiqueta, detalle[:110]))
    sys.stdout.write("  [%s] %-30s %s" % ("OK " if condicion else "FALLO", etiqueta, chr(10)))


def res(r):
    return (r or {}).get("result") or {}


def rotas_en_sesion():
    """Cuantas capas tiene ROTAS la sesion viva. Es la LINEA BASE: una copia con las
    mismas rotas que el original no es una regresion, y exigir cero solo funciona en un
    documento impecable -- el de pruebas no lo es (tiene 3 rotas de origen), asi que
    'capas_rotas_en_copia == 0' daba FALLO con el arreglo funcionando perfectamente.
    Comparar los NOMBRES seria mas fuerte que el recuento (tres rotas distintas dan el
    mismo numero), pero el testigo solo trae el numero; los nombres van en la prosa de
    'aviso_capas_rotas' y parsearla es mas fragil que lo que aporta."""
    try:
        return int((enviar("list_broken_data_sources", {}).get("result") or {}).get("num") or 0)
    except Exception:
        return None


def borrar_fichero(ruta):
    """Para salidas que NO son datasets (un PDF): management.Delete no las toca."""
    try:
        if os.path.exists(ruta):
            os.remove(ruta)
    except Exception:
        pass


def limpiar(*rutas):
    """Borra salidas de pases anteriores SIN puntuar. El script se relanza a menudo, y
    un CopyFeatures contra un destino que ya existe falla por eso, no por lo que se
    esta probando: un fallo asi se lee como regresion y no lo es."""
    for ruta in rutas:
        try:
            enviar("run_geoprocessing", {"tool": "management.Delete", "params": [ruta]})
        except Exception:
            pass


sys.stdout.write("--- revision 2026-09-20 (2.12.0) ---" + chr(10))
# Nada de esto se pudo probar al escribirlo (los arreglos se hicieron con ArcMap
# cerrado): ESTE bloque es su primera prueba real. Las salidas van a una carpeta
# propia de C:\temp y son copias o exports de un solo plano.
SALIDAS = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion"
if not os.path.isdir(SALIDAS):
    os.makedirs(SALIDAS)

r = t("list_layers")
comprobar("list_layers trae 'ruta'",
          bool(res(r).get("capas")) and all("ruta" in c for c in res(r)["capas"]), str(r)[:110])

# La MISMA capa dos veces: homonimas en el mismo contenedor. El nombre pelado debe
# FALLAR (no se elige por el usuario) y la ruta numerada debe resolver a una sola.
t("add_layer", {"fuente": VEC})
t("list_fields", {"capa": NOMBRE_VEC}, espera_ok=False, nota="(nombre ambiguo: debe fallar)")
t("list_fields", {"capa": NOMBRE_VEC + "#2"}, nota="(ruta numerada)")
t("remove_layer", {"capa": NOMBRE_VEC + "#2"}, nota="(deshacer el duplicado)")
t("list_fields", {"capa": NOMBRE_VEC}, nota="(vuelve a ser unica, sin sufijo)")
t("run_geoprocessing", {"tool": "management.GetCount", "params": [NOMBRE_VEC]},
  nota="(entero como int y capa suelta resuelta)")

r = t("get_unique_values", {"capa": NOMBRE_VEC, "campo": "Municipio", "max_valores": 2})
comprobar("max_valores corta y lo dice",
          res(r).get("truncado") is True and len(res(r).get("valores", [])) == 2, str(r)[:110])
t("get_layer_features", {"capa": NOMBRE_VEC, "limite": 0}, espera_ok=False, nota="(limite 0)")
t("get_layer_features", {"capa": NOMBRE_VEC, "limite": 99999}, espera_ok=False, nota="(limite enorme)")

t("set_graduated_symbology", {"capa": NOMBRE_VEC, "campo": "Superficie", "color_desde": "#FFFFB2",
                              "color_hasta": "#BD0026"}, nota="(colores hex, rampa CIE Lab)")
t("set_graduated_symbology", {"capa": NOMBRE_VEC, "campo": "Superficie", "algoritmo": "hsv"},
  nota="(algoritmo hsv explicito)")
t("set_graduated_symbology", {"capa": NOMBRE_VEC, "campo": "Superficie", "color_desde": [255, 0]},
  espera_ok=False, nota="(color mal formado)")

# El bug de la 2.4.3: un parametro invalido NO debe dejar el renderer cambiado.
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "num_clases": 3})
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "estirado", "transparencia": 150},
  espera_ok=False, nota="(transparencia 150: debe fallar SIN mutar)")
r = t("get_layer_info", {"capa": "real_NUEVO.tif"})
sys.stdout.write("      -> comprobar A OJO que real_NUEVO.tif sigue CLASIFICADO en 3, no estirado" + chr(10))

t("get_canvas_screenshot", {"dpi": None}, nota="(dpi null)")
t("export_pdf", {"salida": "plano.pdf"}, espera_ok=False, nota="(ruta relativa)")
t("export_pdf", {"salida": SALIDAS + SEP + "p.pdf", "dpi": 1200}, espera_ok=False, nota="(dpi 1200)")
r = t("export_jpg", {"salida": SALIDAS + SEP + "plano.jpeg", "dpi": 72})
comprobar("'.jpeg' no acaba en '.jpeg.jpg'",
          str(res(r).get("salida", "")).lower().endswith("plano.jpeg"), str(r)[:110])
r = t("export_jpg", {"salida": SALIDAS + SEP + "plano.jpeg", "dpi": 72})
comprobar("segundo export dice 'sobrescrito'", res(r).get("sobrescrito") is True, str(r)[:110])
t("export_jpg", {"salida": SALIDAS + SEP + "plano.jpeg", "dpi": 72, "sobrescribir": False},
  espera_ok=False, nota="(sobrescribir=false con destino existente)")

COPIA = SALIDAS + SEP + "copia_regresion.mxd"
if os.path.exists(COPIA):
    os.remove(COPIA)
t("save_mxd_as", {"salida": COPIA})
t("save_mxd_as", {"salida": COPIA}, espera_ok=False, nota="(ya existe y no se pide pisar)")
r = t("save_mxd_as", {"salida": COPIA, "sobrescribir": True})
comprobar("save_mxd_as dice 'sobrescrito'", res(r).get("sobrescrito") is True, str(r)[:110])

# Merge RECHAZA la misma entrada dos veces (ERROR 000400), asi que el fixture necesita
# dos datasets DISTINTOS. Con la capa duplicada, el 000400 quedaba TAPADO detras del
# 000732 de la extension: se arreglo la extension y aparecio el siguiente error, que
# llevaba ahi desde que se escribio el caso (2026-09-21).
MV_A = SALIDAS + SEP + "mv_a.shp"
MV_B = SALIDAS + SEP + "mv_b.shp"
MV_OUT = SALIDAS + SEP + "mv_merge.shp"
limpiar(MV_OUT, MV_A, MV_B)
t("run_geoprocessing", {"tool": "management.CopyFeatures", "resolver_capas": False,
                        "params": [VEC, MV_A]})
t("run_geoprocessing", {"tool": "management.CopyFeatures", "resolver_capas": False,
                        "params": [VEC, MV_B]})
# gp.AddOutputsToMap: las dos salidas entran solas en la TOC como 'mv_a' y 'mv_b'.
t("run_geoprocessing", {"tool": "management.Merge",
                        "params": [["mv_a", "mv_b"], MV_OUT]},
  nota="(multivalor: lista dentro de params)")
t("run_geoprocessing", {"tool": "management.Merge", "params": [{"a": 1}, "x"]},
  espera_ok=False, nota="(objeto JSON en params)")

# --- multivalor: la extension del dataset (arreglo 2026-09-21) ---
# El name object de un shapefile da "parcelas", no "parcelas.shp", y la ruta
# compuesta NO se podia abrir: ERROR 000732 en Merge/Union/Intersect con capas del
# mapa. Se comprueba sobre el ECO del geoprocesador ('mensajes'), que trae las
# rutas ya resueltas: que la lista lleva ';' y que cada parte apunta a un dataset
# de verdad. Sin mirar el eco, un Merge que casca por otro motivo daria igual.
sys.stdout.write("--- multivalor y extension del dataset (2026-09-21) ---" + chr(10))
MERGE_EXT = SALIDAS + SEP + "merge_ext.shp"
limpiar(MERGE_EXT)
r = t("run_geoprocessing", {"tool": "management.Merge",
                            "params": [["mv_a", "mv_b"], MERGE_EXT]},
      nota="(capas de la TOC en multivalor)")
# Solo el tramo de ENTRADAS del eco: la ruta de salida tambien acaba en .shp y
# contarla daria el verde sin que ninguna entrada se hubiera resuelto bien.
eco = str(res(r).get("mensajes", ""))
entradas = eco.split(MERGE_EXT)[0]
comprobar("el multivalor resuelve con '.shp'", entradas.count(".shp") >= 2, eco[:110])
comprobar("el multivalor va unido por ';'", ";" in entradas, eco[:110])

# La 'fuente' de list_layers se copia y se pega en 'params': si vuelve sin
# extension, el multivalor falla y la culpa parece del geoproceso.
r = t("list_layers")
fuentes = [c.get("fuente", "") for c in res(r).get("capas", [])]
comprobar("list_layers da la fuente del .shp con extension",
          any(f.lower().endswith(".shp") for f in fuentes), str(fuentes)[:110])

# Una ruta SIN extension escrita a mano sigue fallando, y debe seguir fallando: es
# la entrada explicita de quien llama, y adivinarle la extension seria inventar.
t("run_geoprocessing", {"tool": "management.Merge", "resolver_capas": False,
                        "params": [[VEC[:-4], VEC[:-4]], SALIDAS + SEP + "no.shp"]},
  espera_ok=False, nota="(ruta sin extension a mano: debe fallar)")

# Geodatabase: aqui NO hay extension que anadir (C:\x.gdb\fc no es un fichero), asi
# que es el caso que un arreglo mal hecho rompe sin que el de shapefile se entere.
GDB = SALIDAS + SEP + "multivalor.gdb"
if not os.path.isdir(GDB):
    t("run_geoprocessing", {"tool": "management.CreateFileGDB",
                            "params": [SALIDAS, "multivalor.gdb"]})
limpiar(GDB + SEP + "gfc_merge", GDB + SEP + "gfc_a", GDB + SEP + "gfc_b")
t("run_geoprocessing", {"tool": "management.CopyFeatures", "resolver_capas": False,
                        "params": [VEC, GDB + SEP + "gfc_a"]})
t("run_geoprocessing", {"tool": "management.CopyFeatures", "resolver_capas": False,
                        "params": [VEC, GDB + SEP + "gfc_b"]})
# Las tres tools que pasan por AbrirWorkspace con una GEODATABASE. Fallaban las tres
# con el mismo InvalidCastException de FileGDBWorkspaceFactory, en sesion limpia y a la
# primera, desde siempre: ninguna version lo vio porque la regresion no cubria gdb.
# Arreglado el 2026-09-21 activando la factory por ProgID en vez de con `new`.
t("describe_data", {"ruta": GDB + SEP + "gfc_a"}, nota="(fc de geodatabase)")
t("list_feature_classes", {"workspace": GDB}, nota="(workspace .gdb)")
t("add_layer", {"fuente": GDB + SEP + "gfc_a", "nombre": "gdb_capa"},
  nota="(add_layer de fc de geodatabase)")
t("remove_layer", {"capa": "gdb_capa"})
r = t("run_geoprocessing", {"tool": "management.Merge",
                            "params": [["gfc_a", "gfc_b"], GDB + SEP + "gfc_merge"]},
      nota="(feature class de .gdb en multivalor)")
eco_gdb = str(res(r).get("mensajes", ""))
comprobar("la fc de gdb no gana extension",
          "gfc_a" in eco_gdb and "gfc_a.shp" not in eco_gdb, eco_gdb[:110])

# Raster: su name YA trae el ".tif", asi que tiene que quedarse igual.
COMPUESTO = SALIDAS + SEP + "compuesto.tif"
limpiar(COMPUESTO)
r = t("run_geoprocessing", {"tool": "management.CompositeBands",
                            "params": [["real_NUEVO.tif", "real_NUEVO.tif"], COMPUESTO]},
      nota="(raster en multivalor)")
# Exigir que el eco traiga las DOS entradas: un "no contiene .tif.tif" a secas se
# cumple tambien con el eco vacio de un geoproceso que ni arranco (verde vacuo).
eco_ras = str(res(r).get("mensajes", ""))
entradas_ras = eco_ras.split(COMPUESTO)[0]
comprobar("el raster conserva su .tif una sola vez",
          entradas_ras.count(".tif") >= 2 and ".tif.tif" not in entradas_ras,
          eco_ras[:110])

# CONTRATO documentado, no un bug: dentro de una lista viaja una cadena, no el
# objeto Layer, asi que la definition query se PIERDE. Si el arreglo empezara a
# honrarla sin querer, el docstring de la tool mentiria al reves.
t("add_layer", {"fuente": VEC, "nombre": "conquery"})
# count_features devuelve 'num'. Con la clave equivocada salian dos None, y
# 'None == None' daba el contrato por bueno sin haber contado nada: verde falso.
r = t("count_features", {"capa": "conquery"})
total = res(r).get("num")
# El parametro es 'query', NO 'where': con 'where' la tool ignora lo que le mandas y
# aplica el defecto de 'query', que es null = LIMPIAR el filtro. Devolvia ok sin
# filtrar nada y el test se lo creia. Y 'Lorca' existe en el dato (5 de 24); con
# 'Murcia', que no existe, la comprobacion no distinguia un filtro vacio de uno no
# aplicado.
t("set_definition_query", {"capa": "conquery", "query": "\"Municipio\" = 'Lorca'"})
r = t("count_features", {"capa": "conquery"})
filtrado = res(r).get("num")
comprobar("la def query filtra en la capa",
          total is not None and filtrado is not None and 0 < filtrado < total,
          "total=%s filtrado=%s" % (total, filtrado))
MERGE_DQ = SALIDAS + SEP + "merge_dq.shp"
limpiar(MERGE_DQ)
t("run_geoprocessing", {"tool": "management.Merge", "params": [["conquery"], MERGE_DQ]})
t("add_layer", {"fuente": MERGE_DQ, "nombre": "salida_dq"})
r = t("count_features", {"capa": "salida_dq"})
obtenido = res(r).get("num")
comprobar("en multivalor la def query se pierde (contrato)",
          total is not None and obtenido == total,
          "esperado=%s obtenido=%s" % (total, obtenido))
t("remove_layer", {"capa": "salida_dq"})
t("remove_layer", {"capa": "conquery"})

# La copia del documento: que via se tomo y si sus capas resuelven. Si esto da
# capas rotas que la sesion NO tiene, ha vuelto el fallo de las rutas relativas.
r = t("execute_code", {"code": "RESULT = len(MAP.ListLayers(mxd))", "usar_documento": True})
sys.stdout.write("      -> snapshot_via=%s  capas_rotas_en_copia=%s%s"
                 % (res(r).get("snapshot_via"), res(r).get("capas_rotas_en_copia"), chr(10)))
base_rotas = rotas_en_sesion()
rotas_copia = res(r).get("capas_rotas_en_copia")
# El testigo 'capas_rotas_en_copia' NACE en la 2.12.0: en versiones anteriores no viene, y
# comparar None con un int revienta el script a mitad. Si no esta, no hay nada que medir.
# Si el recuento no cuadra, se decide por NOMBRE y solo con capas con fuente en disco:
# arcpy y ArcObjects no cuentan lo mismo. El 2026-09-23, sobre un plano con servicio web,
# arcpy daba por rota `StereoWebMap` (sin dataSource) y ArcObjects no, y ArcObjects
# contaba la tabla `Hoja1$` que ListLayers no ve: 45 contra 44 con la copia resolviendo
# exactamente igual que la sesion. El recuento solo cuadraba en documentos sin servicios.
if rotas_copia is not None and base_rotas is not None and rotas_copia > base_rotas:
    # ListBrokenDataSources devuelve tambien TableView, que no tiene `supports`.
    rc = t("execute_code", {"code": "RESULT = [l.name for l in MAP.ListBrokenDataSources(mxd) "
                                    "if not hasattr(l, 'supports') or "
                                    "(l.supports('DATASOURCE') and l.dataSource)]",
                            "usar_documento": True}, nota="(rotas de la copia con fuente en disco)")
    nombres_copia = res(rc).get("result")
    nombres_sesion = [x.get("nombre") for x in
                      (enviar("list_broken_data_sources", {}).get("result") or {}).get("rotos", [])]
    # Sin lista no hay verde: una llamada fallida daba [] y pasaba (verde vacio).
    sobran = ([n for n in nombres_copia if n not in nombres_sesion]
              if isinstance(nombres_copia, list) else None)
    comprobar("la copia no ANADE capas rotas (por nombre, con fuente en disco)",
              sobran == [], "rotas solo en la copia: %s" % sobran)
else:
    comprobar("la copia no ANADE capas rotas",
              rotas_copia is None
              or (base_rotas is not None and rotas_copia <= base_rotas),
              "copia=%s sesion=%s" % (rotas_copia, base_rotas))
if rotas_copia is None:
    sys.stdout.write("      -> sin testigo 'capas_rotas_en_copia': add-in anterior a la "
                     "2.12.0, NO se ha medido" + chr(10))
t("execute_code", {"code": "print 'N\\xc3\\xbamero'\nprint u'Operaci\\xf3n'\nRESULT = 1",
                   "usar_documento": False}, nota="(print str + unicode mezclados)")
t("execute_code", {"code": "import sys\nsys.exit(3)", "usar_documento": False},
  espera_ok=False, nota="(sys.exit sin RESULT: error claro, no salida vacia)")

# Las tres tools de Data Driven Pages (list_ddp, goto_ddp_page, export_ddp) NO se prueban
# aqui: necesitan un documento con atlas habilitado, que es siempre uno de PRODUCCION, y
# este script ANADE capas, cambia simbologia y lanza geoprocesos sobre el documento
# abierto. Mezclarlo invitaria a lanzar todo esto contra un proyecto real. Viven en
# tests/regresion_ddp.py, que es de solo lectura sobre el documento.

sys.stdout.write("--- leyenda y grupos (2.14.0) ---" + chr(10))
# Solo con una leyenda de AutoAdd en el layout tiene sentido: el control es la capa
# vectorial del principio, que entro con los valores por defecto. Si esa NO esta en
# la leyenda, "no entra con en_leyenda=False" no probaria nada.
leyendas = (enviar("list_layout_elements", {"tipo": "LEGEND_ELEMENT"}).get("result") or {}).get("num", 0)
control = enviar("set_legend_item", {"capa": NOMBRE_VEC}) if leyendas else {"ok": False}
if control.get("ok"):
    t("add_group", {"nombre": "regresion_grupo", "visible": False, "en_leyenda": False})
    t("add_layer", {"fuente": VEC, "grupo": "regresion_grupo", "nombre": "regresion_sin_leyenda",
                    "en_leyenda": False}, nota="(en_leyenda=False)")
    t("set_legend_item", {"capa": "regresion_sin_leyenda"}, espera_ok=False,
      nota="(no ha entrado en la leyenda)")
    estado = control["result"]
    r = t("set_legend_item", {"capa": NOMBRE_VEC,
                              "mostrar_nombre": not estado["ahora"]["mostrar_nombre"]})
    fuentes_iguales = bool(r and r.get("ok") and r["result"]["fuentes"] == estado["fuentes"])
    if fuentes_iguales:
        ok_n += 1
    else:
        fallos.append(("set_legend_item", "las fuentes han cambiado"))
    sys.stdout.write("  [%s] set_legend_item no toca las fuentes%s" % ("OK " if fuentes_iguales else "FALLO", chr(10)))
    t("set_legend_item", {"capa": NOMBRE_VEC, "mostrar_nombre": estado["ahora"]["mostrar_nombre"]},
      nota="(revertir)")
    t("remove_layer", {"capa": "regresion_grupo/regresion_sin_leyenda"})
    t("remove_layer", {"capa": "regresion_grupo"})

    # 3.0: quitar y mover entradas. Con un add-in anterior los parametros se ignoran y
    # la respuesta no trae 'orden': las comprobaciones de abajo fallan (control).
    sys.stdout.write("--- leyenda: quitar y ordenar entradas (3.0) ---" + chr(10))
    t("add_layer", {"fuente": VEC, "nombre": "regresion_orden"})
    r = t("set_legend_item", {"capa": "regresion_orden", "posicion": 1}, nota="(posicion=1)")
    orden = ((r or {}).get("result") or {}).get("orden") or []
    comprobar("posicion=1: la entrada queda la primera, leido de la leyenda",
              orden[:1] == ["regresion_orden"], "orden=%s" % orden)
    r = t("set_legend_item", {"capa": "regresion_orden", "despues_de": NOMBRE_VEC}, nota="(despues_de)")
    orden = ((r or {}).get("result") or {}).get("orden") or []
    comprobar("despues_de: justo detras de la referencia",
              NOMBRE_VEC in orden and "regresion_orden" in orden
              and orden.index("regresion_orden") == orden.index(NOMBRE_VEC) + 1,
              "orden=%s" % orden)
    t("set_legend_item", {"capa": "regresion_orden", "posicion": 1, "despues_de": NOMBRE_VEC},
      espera_ok=False, nota="(posicion y despues_de a la vez)")
    t("set_legend_item", {"capa": "regresion_orden", "posicion": 0}, espera_ok=False,
      nota="(posicion fuera de rango)")
    t("set_legend_item", {"capa": "regresion_orden", "despues_de": "regresion_no_existe"},
      espera_ok=False, nota="(despues_de inexistente)")
    t("set_legend_item", {"capa": "regresion_orden", "quitar": True, "posicion": 1},
      espera_ok=False, nota="(quitar va solo)")
    r = t("set_legend_item", {"capa": "regresion_orden", "quitar": True}, nota="(quitar)")
    orden = ((r or {}).get("result") or {}).get("orden")
    comprobar("quitar: la entrada ya no esta en la leyenda",
              orden is not None and "regresion_orden" not in orden, "orden=%s" % orden)
    # Un cambio de contenido del mapa (ContentsChanged) no debe devolverla.
    t("set_layer_visibility", {"capa": "regresion_orden", "visible": False})
    t("set_layer_visibility", {"capa": "regresion_orden", "visible": True})
    t("set_legend_item", {"capa": "regresion_orden"}, espera_ok=False,
      nota="(sigue fuera tras cambiar el mapa)")
    t("count_features", {"capa": "regresion_orden"}, nota="(la capa sigue en el mapa)")
    t("remove_layer", {"capa": "regresion_orden"})
else:
    sys.stdout.write("  [---] sin leyenda con AutoAdd en este layout: bloque saltado" + chr(10))

sys.stdout.write("--- etiquetas (2.15.0) ---" + chr(10))
# El fallo que hay que cazar es SILENCIOSO: con Maplex, sustituir las clases de
# etiquetas deja la capa sin etiquetas y con ok=true. Por eso el control no es la
# respuesta sino la IMAGEN: la vista con etiquetas tiene que diferir de la vista sin
# ellas. Si set_labels dijera ok y no pintara, los dos PNG saldrian iguales.
import hashlib  # noqa: E402

ETQ_DIR = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion"


def huella(ruta):
    with open(ruta, "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


campos = ((enviar("list_fields", {"capa": NOMBRE_VEC}).get("result") or {}).get("campos") or [])
texto_campo = next((c["nombre"] for c in campos if c.get("tipo") == "String"), None)
inicial = enviar("set_labels", {"capa": NOMBRE_VEC})
if texto_campo and inicial.get("ok"):
    ini = inicial["result"]
    sys.stdout.write("  motor del mapa: %s | clases: %d | campo: %s%s"
                     % (ini["motor"], len(ini["clases"]), texto_campo, chr(10)))
    # UNA entidad a escala de plano, no la capa entera: zoom_to_layer la encuadra a
    # escala regional, y en un plano real (WMS, curvas de nivel etiquetadas, Maplex)
    # el export que viene despues se quedo 20 min dibujando sin dejarse cancelar
    # (2026-09-25). set_extent con una seleccion encuadra la seleccion.
    t("select_by_attribute", {"capa": NOMBRE_VEC, "where": "FID = 0"})
    t("set_extent", {"capa": NOMBRE_VEC}, nota="(una entidad)")
    t("clear_selection", {"capa": NOMBRE_VEC})
    r = t("set_labels", {"capa": NOMBRE_VEC, "expresion": "[" + texto_campo + "]", "tamano": 11,
                         "color": "#1E5C2E", "halo": 1.5, "color_halo": [255, 255, 255]})
    if r and r.get("ok"):
        res = r["result"]
        leido = res["clases"][0]
        coincide = (leido["tamano"] == 11 and leido["halo"] == 1.5
                    and leido["color"] == "#1E5C2E" and leido["color_halo"] == "#FFFFFF"
                    and leido["expresion"] == "[" + texto_campo + "]" and res["etiquetas_activas"])
        # Modificar, no sustituir: mismo numero de clases y ninguna creada.
        intactas = (len(res["clases"]) == len(ini["clases"]) and not res["clase_creada"])
        for nombre, bien in (("set_labels lee de vuelta lo pedido", coincide),
                             ("set_labels modifica, no sustituye", intactas)):
            if bien:
                ok_n += 1
            else:
                fallos.append(("set_labels", nombre + ": " + json.dumps(res)[:200]))
            sys.stdout.write("  [%s] %s%s" % ("OK " if bien else "FALLO", nombre, chr(10)))
    # Export INMEDIATO tras cambiar etiquetas, a proposito: es el caso que colgo ArcMap
    # tres veces el 2026-09-25 (E_PENDING y una espera con DoEvents dentro del add-in
    # que no dejaba terminar el dibujado). Desde la 2.15.0 el add-in devuelve
    # "dibujando: " al momento y el que espera es el SERVIDOR (arcmap_mcp_server._exportar);
    # aqui lo hace enviar(), igual.
    on = ETQ_DIR + SEP + "etiquetas_on.jpg"
    off = ETQ_DIR + SEP + "etiquetas_off.jpg"
    t("export_jpg", {"salida": on, "dpi": 72}, nota="(inmediato tras set_labels)")
    t("set_labels", {"capa": NOMBRE_VEC, "activar": False}, nota="(apagar)")
    t("export_jpg", {"salida": off, "dpi": 72}, nota="(inmediato tras apagar)")
    try:
        pinta = huella(on) != huella(off)
    except OSError:
        pinta = False
    if pinta:
        ok_n += 1
    else:
        fallos.append(("set_labels", "la vista con etiquetas es identica a la vista sin ellas"))
    sys.stdout.write("  [%s] las etiquetas se dibujan (JPG on != off, motor %s)%s"
                     % ("OK " if pinta else "FALLO", ini["motor"], chr(10)))
    r = t("set_labels", {"capa": NOMBRE_VEC, "halo": 0}, nota="(halo 0 lo quita)")
    if r and r.get("ok") and r["result"]["clases"][0]["halo"] != 0:
        fallos.append(("set_labels", "halo=0 no quito el halo"))
    t("set_labels", {"capa": NOMBRE_VEC, "clase": "no_existe_esta_clase", "tamano": 9},
      espera_ok=False, nota="(clase inexistente)")
    t("set_labels", {"capa": NOMBRE_VEC, "color_halo": "#FFFFFF"}, espera_ok=False,
      nota="(color_halo sin halo)")
    t("set_labels", {"capa": NOMBRE_VEC, "tamano": 0}, espera_ok=False, nota="(tamano fuera de rango)")
    t("set_labels", {"capa": NOMBRE_VEC, "activar": ini["etiquetas_activas"]}, nota="(revertir)")
else:
    sys.stdout.write("  [---] sin campo de texto en %s o set_labels no responde: bloque saltado%s"
                     % (NOMBRE_VEC, chr(10)))
    if not inicial.get("ok"):
        fallos.append(("set_labels", str(inicial.get("error"))[:110]))

sys.stdout.write("--- set_labels: Maplex, copiar_de y por_valor (3.0) ---" + chr(10))
# Con un add-in anterior los parametros nuevos se ignoran con ok=true y las lecturas de
# 'maplex', 'copiado_de' y 'por_valor' no llegan (control). Lo aplicado se comprueba
# LEIDO de la capa: un try/except escondio una vez que LabelLargestPolygon no se ponia.
if texto_campo and inicial.get("ok") and inicial["result"].get("maplex"):
    EXPR = "[" + texto_campo + "]"
    t("add_layer", {"fuente": VEC, "nombre": "regresion_maplex"})
    t("set_labels", {"capa": "regresion_maplex", "expresion": EXPR})
    r = t("set_labels", {"capa": "regresion_maplex", "parte_mayor": True, "sin_duplicados_m": 500,
                         "tamano_minimo_m": 50, "repetir_cada_mm": 60}, nota="(opciones Maplex)")
    mx = ((((r or {}).get("result") or {}).get("clases") or [{}])[0]).get("maplex") or {}
    comprobar("Maplex: lo aplicado se lee de la capa",
              mx.get("parte_mayor") is True and mx.get("sin_duplicados") is True
              and mx.get("distancia_duplicados") == 500 and mx.get("unidad_duplicados") == "Map"
              and mx.get("tamano_minimo") == 50 and mx.get("repetir") is True
              and mx.get("intervalo_repeticion") == 60 and mx.get("unidad_repeticion") == "MM",
              json.dumps(mx)[:110])
    r = t("set_labels", {"capa": "regresion_maplex", "sin_duplicados_m": 0, "repetir_cada_mm": 0},
          nota="(0 apaga)")
    mx = ((((r or {}).get("result") or {}).get("clases") or [{}])[0]).get("maplex") or {}
    comprobar("Maplex: 0 apaga sin tocar lo demas",
              mx.get("sin_duplicados") is False and mx.get("repetir") is False
              and mx.get("parte_mayor") is True, json.dumps(mx)[:110])

    t("add_layer", {"fuente": VEC, "nombre": "regresion_copia"})
    r = t("set_labels", {"capa": "regresion_copia", "copiar_de": "regresion_maplex"}, nota="(copiar_de)")
    res_c = (r or {}).get("result") or {}
    c0 = (res_c.get("clases") or [{}])[0]
    comprobar("copiar_de: expresion y colocacion copiadas, leidas de la capa",
              res_c.get("copiado_de") == "regresion_maplex" and c0.get("expresion") == EXPR
              and (c0.get("maplex") or {}).get("parte_mayor") is True and res_c.get("etiquetas_activas"),
              json.dumps(c0)[:110])
    t("set_labels", {"capa": "regresion_copia", "copiar_de": "regresion_no_existe"}, espera_ok=False,
      nota="(copiar_de inexistente)")
    t("remove_layer", {"capa": "regresion_copia"})

    # por_valor: una clase por categoria, CLONADA. El riesgo es el silencioso de Maplex
    # (clases nuevas = capa sin etiquetas), asi que el control es la imagen.
    t("set_unique_values_symbology", {"capa": "regresion_maplex", "campo": texto_campo})
    t("set_labels", {"capa": "regresion_maplex", "clase": "x", "por_valor": {"campo": texto_campo}},
      espera_ok=False, nota="(por_valor con clase)")
    t("set_labels", {"capa": "regresion_maplex", "por_valor": {"campo": "CampoQueNoExiste"}},
      espera_ok=False, nota="(por_valor campo inexistente)")
    t("set_labels", {"capa": "regresion_maplex", "por_valor": {"campo": "FID"}}, espera_ok=False,
      nota="(por_valor sin colores en campo no simbolizado)")
    r = t("set_labels", {"capa": "regresion_maplex", "por_valor": {"campo": texto_campo}}, nota="(por_valor)")
    res_v = (r or {}).get("result") or {}
    pv = res_v.get("por_valor") or []
    clases_v = res_v.get("clases") or []
    colores_v = set(c.get("color") for c in clases_v)
    comprobar("por_valor: una clase por valor, con su filtro y colores distintos",
              len(pv) > 1 and len(clases_v) == len(pv) and all(c.get("filtro") for c in clases_v)
              and len(colores_v) > 1, "valores=%d clases=%d colores=%d" % (len(pv), len(clases_v), len(colores_v)))
    on = ETQ_DIR + SEP + "por_valor_on.jpg"
    off = ETQ_DIR + SEP + "por_valor_off.jpg"
    t("set_layer_visibility", {"capa": NOMBRE_VEC, "visible": False})
    t("export_jpg", {"salida": on, "dpi": 72}, nota="(por_valor encendidas)")
    t("set_labels", {"capa": "regresion_maplex", "activar": False}, nota="(apagar)")
    t("export_jpg", {"salida": off, "dpi": 72}, nota="(por_valor apagadas)")
    try:
        pinta = huella(on) != huella(off)
    except OSError:
        pinta = False
    comprobar("por_valor: las etiquetas clonadas se dibujan con Maplex (JPG on != off)", pinta)
    # por_valor + where: antes el where pisaba el «campo = valor» de cada clase y quedaban N
    # etiquetas por entidad, con la respuesta diciendo el filtro viejo (revision 2026-10-25, medio 2).
    # Ahora se compone con AND; y por_valor + color, que dejaba todas del mismo color, es error.
    t("set_labels", {"capa": "regresion_maplex", "por_valor": {"campo": texto_campo}, "color": "#FF0000"},
      espera_ok=False, nota="(por_valor con color)")
    r = t("set_labels", {"capa": "regresion_maplex", "por_valor": {"campo": texto_campo}, "where": "FID >= 0"},
          nota="(por_valor + where)")
    clases_w = ((r or {}).get("result") or {}).get("clases") or []
    filtros_w = [c.get("filtro") or "" for c in clases_w]
    comprobar("por_valor + where: cada clase con su valor Y el where",
              len(clases_w) == len(pv) and len(set(filtros_w)) == len(pv)
              and all(f.startswith("(" + texto_campo) and f.endswith("AND (FID >= 0)") for f in filtros_w),
              str(filtros_w)[:110])
    t("set_layer_visibility", {"capa": NOMBRE_VEC, "visible": True})
    t("remove_layer", {"capa": "regresion_maplex"})
else:
    sys.stdout.write("  [---] sin Maplex o sin campo de texto: bloque saltado" + chr(10))

sys.stdout.write("--- simbolo unico y edit_symbol (2.15.0) ---" + chr(10))
# El control de edit_symbol es lo que NO debe cambiar: al tocar el borde, el relleno
# sigue igual; al tocar una categoria, la de al lado sigue igual. Una tool que
# rehiciera la simbologia pasaria "lee de vuelta lo pedido" y fallaria aqui.


def comprobar_sim(nombre, bien, det=""):
    global ok_n
    if bien:
        ok_n += 1
    else:
        fallos.append((nombre, str(det)[:200]))
    sys.stdout.write("  [%s] %s%s" % ("OK " if bien else "FALLO", nombre, chr(10)))


r = t("set_single_symbology", {"capa": NOMBRE_VEC, "color_relleno": "#A8D5A2",
                               "color_borde": [46, 125, 50], "grosor_borde": 1.2,
                               "transparencia": 30, "etiqueta": "ENP"})
if r and r.get("ok"):
    res = r["result"]
    k = res["clases"][0]
    comprobar_sim("set_single_symbology deja UNA clase con lo pedido",
                  res["renderer"] == "simple" and len(res["clases"]) == 1
                  and k["color_relleno"] == "#A8D5A2" and k["color_borde"] == "#2E7D32"
                  and k["grosor_borde"] == 1.2 and res["transparencia"] == 30
                  and k["etiqueta"] == "ENP", res)
    r2 = t("edit_symbol", {"capa": NOMBRE_VEC, "color_borde": "#000000"}, nota="(solo el borde)")
    if r2 and r2.get("ok"):
        k2 = r2["result"]["clases"][0]
        comprobar_sim("edit_symbol no toca el relleno al cambiar el borde",
                      k2["color_borde"] == "#000000" and k2["color_relleno"] == "#A8D5A2"
                      and k2["grosor_borde"] == 1.2, k2)
t("set_single_symbology", {"capa": NOMBRE_VEC, "tamano": 5}, espera_ok=False,
  nota="(tamano en poligonos: error)")
t("set_single_symbology", {"capa": NOMBRE_VEC, "sin_relleno": True, "color_relleno": "#FFFFFF"},
  espera_ok=False, nota="(sin_relleno y color a la vez)")
r = t("set_single_symbology", {"capa": NOMBRE_VEC, "sin_relleno": True}, nota="(hueco)")
if r and r.get("ok"):
    comprobar_sim("sin_relleno deja el poligono hueco", r["result"]["clases"][0]["sin_relleno"] is True,
                  r["result"])

t("set_graduated_symbology", {"capa": NOMBRE_VEC, "campo": "Superficie"})
leido = enviar("edit_symbol", {"capa": NOMBRE_VEC})
if leido.get("ok") and len(leido["result"]["clases"]) >= 2:
    antes = leido["result"]["clases"]
    r = t("edit_symbol", {"capa": NOMBRE_VEC, "categoria": "1", "color_relleno": "#FF0000"},
          nota="(rangos, clase 1)")
    if r and r.get("ok"):
        ahora = r["result"]["clases"]
        comprobar_sim("edit_symbol cambia la clase 1 y deja la 2",
                      ahora[0]["color_relleno"] == "#FF0000"
                      and ahora[1]["color_relleno"] == antes[1]["color_relleno"]
                      and r["result"]["renderer"] == "rangos"
                      and len(ahora) == len(antes), {"antes": antes[:2], "ahora": ahora[:2]})
    t("edit_symbol", {"capa": NOMBRE_VEC, "color_relleno": "#00FF00"}, espera_ok=False,
      nota="(relleno a todas: borraria la clasificacion)")
    r = t("edit_symbol", {"capa": NOMBRE_VEC, "grosor_borde": 0.2}, nota="(borde en todas)")
    if r and r.get("ok"):
        comprobar_sim("el borde cambia en todas y los rellenos se quedan",
                      all(c["grosor_borde"] == 0.2 for c in r["result"]["clases"])
                      and r["result"]["clases"][1]["color_relleno"] == antes[1]["color_relleno"],
                      r["result"]["clases"][:2])
    t("edit_symbol", {"capa": NOMBRE_VEC, "categoria": "no_existe", "color_relleno": "#FF0000"},
      espera_ok=False, nota="(categoria inexistente)")
else:
    fallos.append(("edit_symbol", "no se pudo leer la graduada: " + str(leido)[:150]))

t("set_unique_values_symbology", {"capa": NOMBRE_VEC, "campo": "Municipio"})
leido = enviar("edit_symbol", {"capa": NOMBRE_VEC})
if leido.get("ok") and len(leido["result"]["clases"]) >= 2:
    c0, c1 = leido["result"]["clases"][0], leido["result"]["clases"][1]
    r = t("edit_symbol", {"capa": NOMBRE_VEC, "categoria": c0["categoria"], "color_relleno": "#123456"},
          nota="(valores unicos, por valor)")
    if r and r.get("ok"):
        ahora = r["result"]["clases"]
        comprobar_sim("edit_symbol cambia un valor unico y deja el siguiente",
                      ahora[0]["color_relleno"] == "#123456"
                      and ahora[1]["color_relleno"] == c1["color_relleno"]
                      and r["result"]["categorias_cambiadas"] == [c0["etiqueta"]], ahora[:2])
else:
    fallos.append(("edit_symbol", "no se pudo leer los valores unicos: " + str(leido)[:150]))

sys.stdout.write("--- estilos .style (2.15.0) ---" + chr(10))
# Se usa ESRI.style, que viene con ArcMap y siempre esta cargado, y una COPIA suya en
# el temporal para el camino "estilo que no estaba cargado". El control es la galeria:
# tras usar la copia, la lista de estilos cargados tiene que ser la de antes. Una tool
# que se olvidara de quitarla pasaria "lee los simbolos" y fallaria aqui.
import shutil
import tempfile

r = t("list_style_symbols", {}, nota="(estilos cargados)")
cargados = r["result"]["estilos_cargados"] if r and r.get("ok") else []
esri = [e for e in cargados if e["nombre"].lower() == "esri"]
if esri:
    r = t("list_style_symbols", {"estilo": "ESRI", "clase": "relleno", "limite": 5},
          nota="(ESRI, rellenos)")
    simbolos = r["result"]["simbolos"] if r and r.get("ok") else []
    comprobar_sim("list_style_symbols trae rellenos de ESRI con nombre y tipo",
                  len(simbolos) == 5 and r["result"]["truncado"] is True
                  and all(s["tipo_simbolo"] == "relleno" and s["nombre"] for s in simbolos),
                  simbolos)
    if simbolos:
        elegido = simbolos[0]
        # Por id: el nombre no es unico ni con su categoria (ESRI.style trae dos "Verde"
        # en "Predeterminado"; asi fallo la primera pasada del 2026-09-25).
        r = t("apply_style_symbol", {"capa": NOMBRE_VEC, "estilo": "ESRI",
                                     "id_simbolo": elegido["id"], "etiqueta": "Del estilo"},
              nota="(" + elegido["nombre"] + ", id " + str(elegido["id"]) + ")")
        if r and r.get("ok"):
            res = r["result"]
            comprobar_sim("apply_style_symbol deja un simbolo unico con el del estilo",
                          res["renderer"] == "simple" and len(res["clases"]) == 1
                          and res["clases"][0]["etiqueta"] == "Del estilo"
                          and res["simbolo_de_estilo"]["id"] == elegido["id"], res)
        todos = enviar("list_style_symbols", {"estilo": "ESRI", "clase": "relleno", "limite": 2000})
        nombres = [s["nombre"] for s in (todos.get("result") or {}).get("simbolos", [])]
        repetidos = sorted({n for n in nombres if n and nombres.count(n) > 1})
        if repetidos:
            r = t("apply_style_symbol", {"capa": NOMBRE_VEC, "estilo": "ESRI", "nombre_simbolo": repetidos[0]},
                  espera_ok=False, nota="(" + repetidos[0] + " repetido: pide id)")
            comprobar_sim("un nombre repetido falla listando los id",
                          bool(r) and "id_simbolo" in str(r.get("error", "")), r)
        t("apply_style_symbol", {"capa": NOMBRE_VEC, "estilo": "ESRI", "nombre_simbolo": "no_existe_este"},
          espera_ok=False, nota="(simbolo inexistente)")
    lineas = enviar("list_style_symbols", {"estilo": "ESRI", "clase": "linea", "limite": 1})
    if lineas.get("ok") and lineas["result"]["simbolos"]:
        t("apply_style_symbol", {"capa": NOMBRE_VEC, "estilo": "ESRI", "clase": "linea",
                                 "nombre_simbolo": lineas["result"]["simbolos"][0]["nombre"],
                                 "categoria_estilo": lineas["result"]["simbolos"][0]["categoria"]},
          espera_ok=False, nota="(linea sobre poligonos)")
    t("list_style_symbols", {"estilo": "no_existe_este_estilo"}, espera_ok=False, nota="(estilo no cargado)")

    tmp = tempfile.mkdtemp(prefix="arcmap_mcp_estilo_")
    copia = os.path.join(tmp, "copia_regresion.style")
    shutil.copy(esri[0]["ruta"], copia)
    r = t("list_style_symbols", {"estilo": copia, "clase": "relleno", "limite": 1}, nota="(copia sin cargar)")
    if r and r.get("ok"):
        comprobar_sim("la copia se lee y consta como no cargada",
                      r["result"]["estaba_cargado"] is False and r["result"]["devueltos"] == 1, r["result"])
    despues = enviar("list_style_symbols", {})
    rutas_antes = sorted(e["ruta"].lower() for e in cargados)
    rutas_despues = sorted(e["ruta"].lower() for e in despues.get("result", {}).get("estilos_cargados", []))
    comprobar_sim("la galeria queda como estaba tras usar la copia", rutas_antes == rutas_despues,
                  {"antes": rutas_antes, "despues": rutas_despues})
    shutil.rmtree(tmp, ignore_errors=True)
else:
    fallos.append(("list_style_symbols", "ESRI.style no aparece entre los cargados: " + str(cargados)[:150]))

sys.stdout.write("--- move_layer (2.15.0) ---" + chr(10))
# El control es lo que NO debe cambiar al mover: la simbologia leida de vuelta. Quitar
# y volver a anadir (lo unico que habia) la perdia.
RASTER_N = "real_NUEVO.tif"
GRUPO_M = "Mover (regresion)"
sim_antes = enviar("edit_symbol", {"capa": NOMBRE_VEC})
t("add_group", {"nombre": GRUPO_M, "en_leyenda": False})
r = t("move_layer", {"capa": NOMBRE_VEC, "grupo": GRUPO_M}, nota="(a un grupo)")
if r and r.get("ok"):
    comprobar_sim("move_layer mete la capa en el grupo", r["result"]["orden"] == [NOMBRE_VEC], r["result"])
r = t("move_layer", {"capa": RASTER_N, "referencia": NOMBRE_VEC, "posicion": "AFTER"},
      nota="(AFTER, cambiando de grupo)")
if r and r.get("ok"):
    comprobar_sim("AFTER la deja justo debajo de la referencia",
                  r["result"]["orden"] == [NOMBRE_VEC, RASTER_N], r["result"])
r = t("move_layer", {"capa": NOMBRE_VEC, "posicion": "BOTTOM"}, nota="(BOTTOM en su grupo)")
if r and r.get("ok"):
    comprobar_sim("BOTTOM dentro del mismo grupo",
                  r["result"]["orden"] == [RASTER_N, NOMBRE_VEC] and r["result"]["indice"] == 1, r["result"])
r = t("move_layer", {"capa": NOMBRE_VEC, "referencia": RASTER_N}, nota="(BEFORE por defecto)")
if r and r.get("ok"):
    comprobar_sim("BEFORE dentro del mismo grupo",
                  r["result"]["orden"] == [NOMBRE_VEC, RASTER_N] and r["result"]["indice"] == 0, r["result"])
t("move_layer", {"capa": GRUPO_M, "referencia": NOMBRE_VEC}, espera_ok=False, nota="(grupo dentro de si mismo)")
t("move_layer", {"capa": NOMBRE_VEC, "posicion": "AFTER"}, espera_ok=False, nota="(AFTER sin referencia)")
sim_despues = enviar("edit_symbol", {"capa": NOMBRE_VEC})
if sim_antes.get("ok") and sim_despues.get("ok"):
    comprobar_sim("mover no toca la simbologia",
                  sim_antes["result"]["clases"] == sim_despues["result"]["clases"], sim_despues["result"])
t("move_layer", {"capa": RASTER_N, "grupo": "/", "posicion": "TOP"}, nota="(a la raiz)")
t("move_layer", {"capa": NOMBRE_VEC, "grupo": "/", "posicion": "TOP"})
t("remove_layer", {"capa": GRUPO_M})

sys.stdout.write("--- add_layer WMS (2.15.0) ---" + chr(10))
# Siempre con visible=False: un WMS encendido se redibuja por la red en el hilo de
# ArcMap y aqui solo se prueba el arbol de subcapas, no el dibujo.
WMS_URL = "https://www.ign.es/wms-inspire/mapa-raster"
WMS_N = "IGN (regresion)"
n_antes = len((enviar("list_layers").get("result") or {}).get("capas") or [])
r = enviar("add_layer", {"fuente": WMS_URL, "nombre": WMS_N, "visible": False, "en_leyenda": False})
if r.get("ok"):
    ok_n += 1
    serv = r["result"]["servicio"]
    hojas = [n["ruta"] for n in serv["arbol"] if not n["grupo"]]
    comprobar_sim("WMS sin subcapas: todas las hojas encendidas y la capa apagada",
                  sorted(serv["subcapas_encendidas"]) == sorted(hojas) and r["result"]["visible"] is False, serv)
    t("remove_layer", {"capa": WMS_N})
    if hojas:
        una = hojas[-1]
        r = t("add_layer", {"fuente": WMS_URL, "nombre": WMS_N, "visible": False, "en_leyenda": False,
                            "subcapas": [una]}, nota="(una subcapa)")
        if r and r.get("ok"):
            arbol = r["result"]["servicio"]["arbol"]
            encendidos = [n["ruta"] for n in arbol if n["visible"]]
            padres = [n["ruta"] for n in arbol if n["grupo"] and una.startswith(n["ruta"] + "/")]
            comprobar_sim("solo la pedida y sus grupos quedan encendidos",
                          sorted(encendidos) == sorted([una] + padres), arbol)
            t("remove_layer", {"capa": WMS_N})
    t("add_layer", {"fuente": WMS_URL, "nombre": WMS_N, "visible": False, "subcapas": ["no_existe_esta_subcapa"]},
      espera_ok=False, nota="(subcapa inexistente)")
    n_despues = len((enviar("list_layers").get("result") or {}).get("capas") or [])
    comprobar_sim("una subcapa inexistente no deja nada en la TOC", n_despues == n_antes,
                  {"antes": n_antes, "despues": n_despues})
else:
    sys.stdout.write("  [---] el WMS del IGN no conecta (%s): bloque saltado%s" % (str(r.get("error"))[:90], chr(10)))
t("add_layer", {"fuente": VEC, "subcapas": ["x"]}, espera_ok=False, nota="(subcapas en un shp)")

sys.stdout.write("--- rutas relativas largas (2.15.0) ---" + chr(10))
# Con rutas relativas, ArcMap 10.5 pierde al guardar el workspace de una capa si
# len(carpeta del .mxd) + 1 + len(relativa del workspace) >= 260 (medido 2026-09-25).
# Una copia del shp a la distancia justa (262) contra otra cercana. El control fuerte
# es la COPIA REABIERTA: lo que el add-in predice tiene que ser exactamente lo que
# sale roto al reabrir, ni mas ni menos.
import glob
import re
import subprocess

info = enviar("get_arcmap_info")
doc_mxd = (info.get("result") or {}).get("mxd") if info.get("ok") else None
PY27 = (glob.glob("C:" + SEP + "Python27" + SEP + "ArcGIS*" + SEP + "python.exe") or [None])[0]
AUDITOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "src", "auditor_mxd.py")


def concatenada(carpeta, ws):
    return len(carpeta) + 1 + len(os.path.relpath(ws, carpeta))


def copiar_como_r(destino):
    if not os.path.isdir(destino):
        os.makedirs(destino)
    base = os.path.splitext(VEC)[0]
    for f in glob.glob(base + ".*"):
        # Fuera los .lock: ArcMap los tiene abiertos mientras la capa esta en el mapa.
        if f.lower().endswith(".lock"):
            continue
        shutil.copy(f, os.path.join(destino, "r" + f[len(base):]))
    return os.path.join(destino, "r.shp")


RAIZ_RUTAS = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "rutas_relativas"
if doc_mxd and doc_mxd.lower().endswith(".mxd") and PY27:
    carpeta_doc = os.path.dirname(doc_mxd)
    shutil.rmtree(RAIZ_RUTAS, ignore_errors=True)
    cerca = RAIZ_RUTAS + SEP + "cerca"
    lejos = RAIZ_RUTAS + SEP + "lejos"
    # Se alarga "lejos" a tramos de <=100 hasta que la suma llegue a 262.
    while concatenada(carpeta_doc, lejos) < 262:
        falta = 262 - concatenada(carpeta_doc, lejos)
        lejos = lejos + "x" if falta == 1 else lejos + SEP + "x" * min(100, falta - 1)
    if concatenada(carpeta_doc, lejos) != 262 or len(lejos) > 245:
        sys.stdout.write("  [---] no se pudo fabricar la ruta a 262 (%d, %d car.): bloque saltado%s"
                         % (concatenada(carpeta_doc, lejos), len(lejos), chr(10)))
    else:
        shp_cerca = copiar_como_r(cerca)
        copiar_como_r(lejos)
        CAPA_R = "Ruta larga (regresion)"
        t("add_layer", {"fuente": shp_cerca, "nombre": CAPA_R, "en_leyenda": False})
        r = t("repair_data_source", {"capa": CAPA_R, "ruta_antigua": cerca, "ruta_nueva": lejos},
              nota="(a 262: avisa)")
        if r and r.get("ok"):
            riesgo = r["result"].get("riesgo_ruta_relativa") or {}
            comprobar_sim("repair_data_source avisa de que se perdera al guardar",
                          r["result"]["aplicado"] and bool(r["result"].get("aviso_ruta_relativa"))
                          and riesgo.get("longitud") == 262, r["result"])
        r = t("repair_data_source", {"capa": CAPA_R, "ruta_antigua": lejos, "ruta_nueva": cerca},
              nota="(cerca: sin aviso)")
        if r and r.get("ok"):
            comprobar_sim("repair_data_source no avisa con una ruta que cabe",
                          r["result"]["aplicado"] and "aviso_ruta_relativa" not in r["result"], r["result"])
        t("repair_data_source", {"capa": CAPA_R, "ruta_antigua": cerca, "ruta_nueva": lejos})

        copia_mxd = os.path.join(carpeta_doc, "regresion_rutas_copia.mxd")
        r = t("save_mxd_as", {"salida": copia_mxd, "sobrescribir": True}, nota="(copia junto al original)")
        if r and r.get("ok"):
            res = r["result"]
            predichas = {(c["data_frame"].lower(), c["ruta"].lower()) for c in res.get("capas_perderan_ruta", [])}
            # El add-in numera los nombres repetidos ("Capa#2"); arcpy no.
            memoria = {(c["data_frame"].lower(), re.sub(r"#[0-9]+(?=/|$)", "", c["ruta"]).lower())
                       for c in res.get("rotas_en_memoria", [])}
            comprobar_sim("save_mxd_as predice la capa lejana",
                          any(ruta == CAPA_R.lower() for _, ruta in predichas), res.get("capas_perderan_ruta"))
            proc = subprocess.run([PY27, AUDITOR, copia_mxd], capture_output=True, timeout=240)
            auditado = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
            if auditado.get("ok"):
                en_disco = {(c["data_frame"].lower(), c["nombre_largo"].replace(SEP, "/").lower())
                            for c in auditado["capas"] if c.get("rota") and not c.get("grupo")}
                nuevas = en_disco - memoria
                comprobar_sim("al reabrir la copia se rompe exactamente lo predicho",
                              nuevas == predichas, {"predichas": sorted(predichas), "nuevas": sorted(nuevas)})
            else:
                fallos.append(("save_mxd_as", "el auditor no abrio la copia: " + str(auditado)[:150]))
            try:
                os.remove(copia_mxd)
            except OSError:
                pass
        t("remove_layer", {"capa": CAPA_R})
        shutil.rmtree(RAIZ_RUTAS, ignore_errors=True)
else:
    sys.stdout.write("  [---] documento sin ruta .mxd o sin Python 2.7 de ArcGIS: bloque saltado" + chr(10))

sys.stdout.write("--- run_geoprocessing: errores propios (2.16.0) ---" + chr(10))
# El sintoma del informe de agosto: una tool INEXISTENTE fallaba antes de arrancar, sin
# mensajes propios, y el error salia con los del geoproceso ANTERIOR (los mensajes del
# geoprocesador son del proceso). Control: la llamada previa deja una marca unica en la
# cola de mensajes, y la siguiente no puede nombrarla.
MARCA_GP = "C:" + SEP + "temp" + SEP + "no_existe_MARCA_GP_2160.shp"
t("run_geoprocessing", {"tool": "management.GetCount", "params": [MARCA_GP]},
  espera_ok=False, nota="(deja la marca en los mensajes)")
r = t("run_geoprocessing", {"tool": "management.NoExisteEstaTool", "params": ["x"]},
      espera_ok=False, nota="(tool inexistente)")
err = str((r or {}).get("error", ""))
comprobar("tool inexistente no trae el error anterior", "MARCA_GP_2160" not in err, err[:110])
comprobar("tool inexistente lo dice", "No existe la herramienta" in err, err[:110])
# Parametros de mas: el geoprocesador los ignoraba y daba ok habiendo hecho otra cosa.
r = t("run_geoprocessing", {"tool": "management.GetCount", "params": [VEC, "sobra", "sobra"]},
      espera_ok=False, nota="(parametros de mas)")
comprobar("parametros de mas: dice cuantos admite", "admite" in str((r or {}).get("error", "")),
          str((r or {}).get("error", ""))[:110])
# Y la misma tool con sus parametros justos sigue funcionando (control del control).
r = t("run_geoprocessing", {"tool": "management.GetCount", "params": [VEC]})
# (r or {}).get("result"), no res(r): mas arriba el script reutiliza `res` como variable.
salida_gp = (r or {}).get("result") or {}
comprobar("GetCount valido tras los errores", salida_gp.get("salidas") == ["24"], str(salida_gp)[:110])

sys.stdout.write("--- run_geoprocessing fuera de ArcMap (2.16.0) ---" + chr(10))
# El servidor manda fuera_de_arcmap=True como 'run_geoprocessing_fuera'. Carpeta nueva en
# cada pasada: ArcMap no suelta un dato que ha cargado aunque se quite la capa, asi que
# reutilizar la de la pasada anterior daria ERROR 000258 por un motivo ajeno a la prueba.
import uuid
DIR_FUERA = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "fuera_" + uuid.uuid4().hex[:8]
os.makedirs(DIR_FUERA)
BUF_FUERA = DIR_FUERA + SEP + "buf.shp"
r = t("run_geoprocessing_fuera", {"tool": "management.GetCount", "params": [VEC]}, nota="(por ruta)")
f = (r or {}).get("result") or {}
comprobar("fuera: GetCount da 24 y no añade nada",
          f.get("salidas") == ["24"] and f.get("capas_salida") == [] and f.get("fuera_de_arcmap") is True,
          str(f)[:110])
r = t("run_geoprocessing_fuera", {"tool": "analysis.Buffer", "params": [NOMBRE_VEC, BUF_FUERA, "100 Meters"]},
      nota="(capa por NOMBRE)")
f = (r or {}).get("result") or {}
comprobar("fuera: el nombre de capa viaja como ruta", NOMBRE_VEC in (f.get("capas_por_ruta") or {}), str(f)[:110])
comprobar("fuera: la salida entra en el mapa", len(f.get("anadidas_al_mapa") or []) == 1, str(f)[:110])
r = t("run_geoprocessing_fuera", {"tool": "analysis.Buffer", "params": [NOMBRE_VEC, BUF_FUERA, "100 Meters"]},
      espera_ok=False, nota="(salida existente, sin sobrescribir)")
r = t("run_geoprocessing_fuera", {"tool": "analysis.Buffer", "params": [NOMBRE_VEC, BUF_FUERA, "200 Meters"],
                                  "sobrescribir": True}, espera_ok=False, nota="(salida cargada en la sesion)")
comprobar("fuera: explica el bloqueo de la sesion", "Usa otra ruta" in str((r or {}).get("error", "")),
          str((r or {}).get("error", ""))[:110])
t("run_geoprocessing_fuera", {"tool": "management.NoExisteEstaTool", "params": ["x"]},
  espera_ok=False, nota="(tool inexistente)")
# Spatial Analyst: por arcpy.sa.Slope (algebra de mapas) no habia ruta de salida; va por
# arcpy.gp.Slope_sa, con la firma del geoprocesador.
PEND_FUERA = DIR_FUERA + SEP + "pend.tif"
t("run_geoprocessing_fuera", {"tool": "sa.Slope", "params": [RASTER, PEND_FUERA], "anadir_al_mapa": False},
  nota="(Spatial Analyst)")
comprobar("fuera: sa.Slope escribe su salida", os.path.exists(PEND_FUERA), PEND_FUERA)
# Multivalor con un NOMBRE de capa dentro de la lista: viaja por la ruta de su fuente.
r = t("run_geoprocessing_fuera", {"tool": "management.Merge",
                                  "params": [[NOMBRE_VEC, BUF_FUERA], DIR_FUERA + SEP + "merge.shp"],
                                  "anadir_al_mapa": False}, nota="(multivalor con nombre de capa)")
r = t("run_geoprocessing_fuera", {"tool": "management.GetCount", "params": [DIR_FUERA + SEP + "merge.shp"]})
comprobar("fuera: el merge tiene 48", ((r or {}).get("result") or {}).get("salidas") == ["48"],
          str((r or {}).get("result"))[:110])
# Dos parametros de mas por arcpy.gp tumbaban Python: los rechaza antes el add-in.
t("run_geoprocessing_fuera", {"tool": "management.GetCount", "params": [VEC, "sobra", "sobra"]},
  espera_ok=False, nota="(dos de mas: antes del runner)")
t("run_geoprocessing_fuera", {"tool": "management.GetCount", "params": [VEC, "sobra"]},
  espera_ok=False, nota="(parametros de mas)")
# Control: con definition query, fuera procesaria la fuente ENTERA. Tiene que negarse,
# y la misma llamada dentro de ArcMap tiene que contar solo lo filtrado.
t("set_definition_query", {"capa": NOMBRE_VEC, "query": "FID < 5"})
r = t("run_geoprocessing_fuera", {"tool": "management.GetCount", "params": [NOMBRE_VEC]},
      espera_ok=False, nota="(capa con definition query)")
comprobar("fuera: se niega con definition query", "definition query" in str((r or {}).get("error", "")),
          str((r or {}).get("error", ""))[:110])
r = t("run_geoprocessing", {"tool": "management.GetCount", "params": [NOMBRE_VEC]}, nota="(dentro, control)")
comprobar("dentro: la misma capa cuenta 5", ((r or {}).get("result") or {}).get("salidas") == ["5"],
          str((r or {}).get("result"))[:110])
t("set_definition_query", {"capa": NOMBRE_VEC, "query": ""})
t("select_by_attribute", {"capa": NOMBRE_VEC, "where": "FID < 3"})
t("run_geoprocessing_fuera", {"tool": "management.GetCount", "params": [NOMBRE_VEC]},
  espera_ok=False, nota="(capa con seleccion)")
t("clear_selection", {"capa": NOMBRE_VEC})
t("remove_layer", {"capa": "buf"})

sys.stdout.write("--- request de mas de 1 MB (2.16.0) ---" + chr(10))
# Por el socket directo, sin el servidor MCP (que ya no lo enviaria). Hasta la 2.15.0 el
# add-in cerraba sin leer el resto y aqui saltaba WinError 10054 (-> [EXC]); ahora tiene
# que llegar su error.
r = t("execute_code", {"code": "x = 1" + chr(10) + "#" + "a" * (1100 * 1024)},
      espera_ok=False, nota="(1,1 MB)")
comprobar("request grande: llega el motivo, no un 10054", "DEMASIADO GRANDE" in str((r or {}).get("error", "")),
          str(r)[:110])
t("ping", nota="(el puente sigue atendiendo)")

sys.stdout.write("--- capa homonima, rename_layer y clases de edit_symbol (2.17.0) ---" + chr(10))
# Una capa de la raiz con una homonima dentro de un grupo era inalcanzable: su ruta es su
# nombre pelado, y al repetirla casaba con las dos (expediente real, 2026-10-01).
G17 = "Grupo_2170"
t("add_group", {"nombre": G17})
t("add_layer", {"fuente": VEC, "grupo": G17})
DENTRO = G17 + "/" + NOMBRE_VEC


def visible(ruta):
    for c in (enviar("list_layers").get("result") or {}).get("capas", []):
        if c.get("ruta") == ruta:
            return c.get("visible")
    return None


t("set_layer_visibility", {"capa": NOMBRE_VEC, "visible": False}, nota="(nombre pelado = la de la raiz)")
comprobar("homonima: se apaga la de la RAIZ, no la del grupo",
          visible(NOMBRE_VEC) is False and visible(DENTRO) is True, "%s / %s" % (visible(NOMBRE_VEC), visible(DENTRO)))
t("set_layer_visibility", {"capa": "/" + NOMBRE_VEC, "visible": True}, nota="('/' delante)")
t("set_layer_visibility", {"capa": DENTRO, "visible": False}, nota="(ruta del grupo)")
comprobar("homonima: la ruta del grupo apaga solo esa",
          visible(NOMBRE_VEC) is True and visible(DENTRO) is False, "%s / %s" % (visible(NOMBRE_VEC), visible(DENTRO)))

# Las destructivas no adivinan (opcion 2, decision de Pedro del 2026-10-10): con homonimas, el
# nombre pelado es error aunque una este en la raiz, y se exige la ruta anclada con '/'.
r = t("remove_layer", {"capa": NOMBRE_VEC}, espera_ok=False, nota="(destructiva: nombre pelado con homonima)")
comprobar("remove_layer homonima: pide la ruta anclada y no borra nada",
          ("/" + NOMBRE_VEC) in str((r or {}).get("error", "")) and visible(NOMBRE_VEC) is True
          and visible(DENTRO) is False, str(r)[:110])
r = t("repair_data_source", {"capa": NOMBRE_VEC, "ruta_antigua": "no_existe_2170", "ruta_nueva": "x"},
      espera_ok=False, nota="(destructiva: nombre pelado con homonima)")
t("remove_layer", {"capa": "/" + DENTRO}, nota="(ruta anclada del grupo)")
comprobar("remove_layer anclada: se va la del grupo y la de la raiz sigue",
          visible(DENTRO) is None and visible(NOMBRE_VEC) is True, "%s / %s" % (visible(NOMBRE_VEC), visible(DENTRO)))
t("add_layer", {"fuente": VEC, "grupo": G17}, nota="(volver a ponerla)")
t("set_layer_visibility", {"capa": DENTRO, "visible": False})

# Prefijos de capa (2026-10-10): 'visible:' elige entre las que se dibujan (la del grupo esta
# apagada), y 'fuente:' por el fichero, que comparten las dos (asi que es ambiguo).
t("set_layer_visibility", {"capa": "visible:" + NOMBRE_VEC, "visible": False}, nota="(visible: con homonima apagada)")
comprobar("visible: apaga la de la raiz, la unica que se dibujaba",
          visible(NOMBRE_VEC) is False and visible(DENTRO) is False, "%s / %s" % (visible(NOMBRE_VEC), visible(DENTRO)))
t("set_layer_visibility", {"capa": "/" + NOMBRE_VEC, "visible": True})
r = t("count_features", {"capa": "fuente:Interés Turístico"}, espera_ok=False, nota="(fuente: compartida por dos)")
comprobar("fuente: con dos candidatas da sus rutas", DENTRO in str((r or {}).get("error", "")), str(r)[:110])

r = t("rename_layer", {"capa": DENTRO, "nombre_nuevo": "Renombrada 2170 ñ"})
comprobar("rename_layer: la TOC lo lee de vuelta", visible(G17 + "/Renombrada 2170 ñ") is False, str(r)[:110])
t("rename_layer", {"capa": G17 + "/Renombrada 2170 ñ", "nombre_nuevo": "a/b"}, espera_ok=False, nota="('/' en el nombre)")
DENTRO = G17 + "/Renombrada 2170 ñ"

t("set_unique_values_symbology", {"capa": DENTRO, "campo": "Municipio"})
clases = ((enviar("edit_symbol", {"capa": DENTRO}).get("result") or {}).get("clases") or [])
if len(clases) >= 3:
    c1, c2 = clases[0]["categoria"], clases[1]["categoria"]
    r = t("edit_symbol", {"capa": DENTRO, "categoria": c1, "etiqueta": c1 + " · 2170"}, nota="(etiqueta)")
    etiq = [k["etiqueta"] for k in ((r or {}).get("result") or {}).get("clases", []) if k["categoria"] == c1]
    comprobar("edit_symbol: etiqueta leida de vuelta", etiq == [c1 + " · 2170"], str(etiq))
    t("edit_symbol", {"capa": DENTRO, "etiqueta": "X"}, espera_ok=False, nota="(etiqueta sin categoria)")
    antes = {k["categoria"]: k.get("color_relleno") for k in clases}
    r = t("edit_symbol", {"capa": DENTRO, "categoria": c2, "quitar": True}, nota="(quitar clase)")
    despues = {k["categoria"]: k.get("color_relleno") for k in ((r or {}).get("result") or {}).get("clases", [])}
    comprobar("quitar: la clase desaparece y las demas NO cambian de color",
              c2 not in despues and len(despues) == len(antes) - 1
              and all(despues[k] == antes[k] for k in despues), str(despues)[:110])
else:
    comprobar("valores unicos con al menos 3 clases", False, str(clases)[:110])

t("set_text_element", {"buscar": "texto que no existe 2170"}, espera_ok=False, nota="(sin nada que cambiar)")
# Un campo que la capa no tiene: ArcMap no dibujaba nada y set_labels decia «activas».
r = t("set_labels", {"capa": DENTRO, "expresion": "[CampoQueNoExiste] & \" m\""}, espera_ok=False,
      nota="(campo inexistente)")
comprobar("set_labels: el error nombra el campo que falta", "CampoQueNoExiste" in str((r or {}).get("error", "")),
          str(r)[:110])
t("remove_layer", {"capa": G17})

sys.stdout.write("--- valores unicos: varios campos, clases, resto; edit_symbol (3.0) ---" + chr(10))
# Con un add-in anterior 'campos'/'clases'/'resto' se ignoraban (o fallaba sin 'campo'), y
# 'valores_agrupados'/'encabezado' no llegaban: el bloque falla (control). Todo se comprueba
# leido del RENDERER; arcpy da classValues vacio con varios campos.


def uv(r):
    return (r or {}).get("result") or {}


t("add_layer", {"fuente": VEC, "nombre": "regresion_uv"})
r = uv(t("set_unique_values_symbology", {"capa": "regresion_uv", "campos": ["Municipio", "Vocacion"]},
         nota="(dos campos)"))
cl = r.get("clases") or []
comprobar("dos campos: cada clase es 'Municipio, Vocacion' y no queda nada sin clase",
          len(cl) > 1 and all(", " in c.get("valor", "") for c in cl) and r.get("valores_sin_clase") == [],
          "clases=%d sin_clase=%s" % (len(cl), r.get("valores_sin_clase")))
e = uv(t("edit_symbol", {"capa": "regresion_uv"}, nota="(estado con dos campos)"))
comprobar("edit_symbol ve las clases de dos campos", e.get("renderer") == "valores_unicos"
          and len(e.get("clases") or []) == len(cl), "edit=%d set=%d" % (len(e.get("clases") or []), len(cl)))
t("set_unique_values_symbology", {"capa": "regresion_uv", "campos": ["Municipio", "Vocacion", "Nombre", "Codigo"]},
  espera_ok=False, nota="(mas de 3 campos)")
t("set_unique_values_symbology", {"capa": "regresion_uv", "campo": "Municipio", "campos": ["Vocacion"]},
  espera_ok=False, nota="(campo y campos a la vez)")
t("set_unique_values_symbology", {"capa": "regresion_uv", "campo": "Municipio",
                                  "clases": [{"valores": ["Aledo"]}, {"valores": ["Aledo", "Lorca"]}]},
  espera_ok=False, nota="(un valor en dos clases)")
CLASES = [{"valores": ["Aledo", "Lorca"], "etiqueta": "Sur", "color": "#AA0000"},
          {"valores": ["Bullas"], "etiqueta": "Noroeste"}]
r = uv(t("set_unique_values_symbology", {"capa": "regresion_uv", "campo": "Municipio", "clases": CLASES},
         nota="(clases explicitas, sin resto)"))
cl = r.get("clases") or []
comprobar("clases: dos valores en una clase, leidos del renderer",
          len(cl) == 2 and cl[0].get("etiqueta") == "Sur" and sorted(cl[0].get("valores") or []) == ["Aledo", "Lorca"],
          json.dumps(cl, ensure_ascii=False)[:110])
comprobar("sin resto: dice que hay valores sin clase y que no se dibujan",
          len(r.get("valores_sin_clase") or []) == 10 and "NO se dibujan" in (r.get("aviso") or ""),
          str(r.get("valores_sin_clase"))[:110])
r = uv(t("set_unique_values_symbology", {"capa": "regresion_uv", "campo": "Municipio", "clases": CLASES,
                                         "resto": "Otros municipios"}, nota="(con resto)"))
comprobar("resto: leido del renderer", r.get("resto") == "Otros municipios", str(r.get("resto")))
e = uv(t("edit_symbol", {"capa": "regresion_uv", "categoria": "Noroeste", "anadir_valores": ["Cehegín", "Moratalla"]},
         nota="(anadir_valores)"))
por_et = dict((c.get("etiqueta"), c) for c in e.get("clases") or [])
comprobar("anadir_valores: entran en la clase y la otra no cambia de color",
          sorted((por_et.get("Noroeste") or {}).get("valores_agrupados") or []) == ["Cehegín", "Moratalla"]
          and (por_et.get("Sur") or {}).get("color_relleno") == "#AA0000",
          json.dumps(e.get("clases"), ensure_ascii=False)[:110])
t("edit_symbol", {"capa": "regresion_uv", "categoria": "Sur", "anadir_valores": ["Bullas"]}, espera_ok=False,
  nota="(valor que ya tiene clase)")
e = uv(t("edit_symbol", {"capa": "regresion_uv", "encabezado": "Comarca"}, nota="(encabezado)"))
comprobar("encabezado: leido de la leyenda del renderer", e.get("encabezado") == "Comarca", str(e.get("encabezado")))
e = uv(t("edit_symbol", {"capa": "regresion_uv", "categoria": "Noroeste", "quitar": True}, nota="(quitar con agrupados)"))
comprobar("quitar una clase se lleva sus valores agrupados",
          [c.get("etiqueta") for c in e.get("clases") or []] == ["Sur"], json.dumps(e.get("clases"), ensure_ascii=False)[:110])
t("remove_layer", {"capa": "regresion_uv"})

# Raster: encabezado, etiqueta y color de una clase por edit_symbol.
if enviar("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "clasificado", "num_clases": 3}).get("ok"):
    e = uv(t("edit_symbol", {"capa": "real_NUEVO.tif", "encabezado": "Altura (m)"}, nota="(raster: encabezado)"))
    comprobar("raster: encabezado leido de la leyenda", e.get("encabezado") == "Altura (m)", str(e.get("encabezado")))
    e = uv(t("edit_symbol", {"capa": "real_NUEVO.tif", "categoria": "1", "etiqueta": "Bajo",
                             "color_relleno": "#00FF00"}, nota="(raster: clase 1)"))
    c1 = (e.get("clases") or [{}])[0]
    comprobar("raster: etiqueta y color de la clase 1, leidos; la 2 no cambia",
              c1.get("etiqueta") == "Bajo" and c1.get("color_relleno") == "#00FF00"
              and ((e.get("clases") or [{}, {}])[1].get("color_relleno")
                   == ((e.get("antes") or {}).get("clases") or [{}, {}])[1].get("color_relleno")),
              json.dumps(e.get("clases"), ensure_ascii=False)[:110])
    comprobar("raster: el encabezado sobrevive a editar una clase", e.get("encabezado") == "Altura (m)",
              str(e.get("encabezado")))
    t("edit_symbol", {"capa": "real_NUEVO.tif", "grosor_borde": 1}, espera_ok=False, nota="(raster: parametro de vector)")
else:
    comprobar("raster de prueba clasificado para edit_symbol", False, "set_raster_symbology fallo")

# RGB (3.0): real_NUEVO.tif tiene 3 bandas de 8 bits. Con un add-in anterior 'rgb' no es un modo
# valido y falla (control).
r = uv(t("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "rgb", "bandas": [3, 1, 2]}, nota="(rgb)"))
comprobar("rgb: bandas leidas del renderer y sin estirar en 8 bits",
          r.get("bandas") == [3, 1, 2] and r.get("estiramiento") == "NONE", json.dumps(r)[:110])
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "rgb", "bandas": [5, 1, 2]}, espera_ok=False,
  nota="(rgb: banda inexistente)")
t("set_raster_symbology", {"capa": "real_NUEVO.tif", "modo": "rgb", "bandas": [1, 2]}, espera_ok=False,
  nota="(rgb: dos bandas)")

sys.stdout.write("--- set_legend: tamano, columnas, recuadro (3.0) ---" + chr(10))
# Con un add-in anterior el comando no existe y todo el bloque falla (control). Lo que se
# comprueba es el CONTENIDO leido de la leyenda: los getters de ILegendItem devuelven
# copias, y escribir sin reasignar daba ok y no cambiaba nada.
# (No se usa res(): mas arriba el script reutiliza `res` como variable.)


def resultado_de(r):
    return (r or {}).get("result") or {}


t("add_layer", {"fuente": VEC, "nombre": "regresion_leyenda"})
e0 = resultado_de(t("set_legend", {}, nota="(estado)"))
nat0 = e0.get("natural") if isinstance(e0.get("natural"), dict) else None
comprobar("set_legend mide el tamano natural", nat0 is not None
          and nat0["ancho"] > 0 and nat0["alto"] > 0, str(e0.get("natural"))[:110])
r = resultado_de(t("set_legend", {"tamano": 8}, nota="(tamano=8)"))
ahora = r.get("ahora") or {}
comprobar("tamano=8: minimo y maximo leidos son 8",
          ahora.get("tamano_min") == 8 and ahora.get("tamano_max") == 8,
          "min=%s max=%s" % (ahora.get("tamano_min"), ahora.get("tamano_max")))
# Con ajuste automatico ArcMap encoge la letra al dibujar: lo escrito no seria lo dibujado.
comprobar("tamano: apaga el ajuste automatico", ahora.get("ajuste_automatico") is False,
          str(ahora.get("ajuste_automatico")))
nat8 =ahora.get("natural") if isinstance(ahora.get("natural"), dict) else None
if nat8 and ahora.get("marco"):
    x0 = ahora["marco"]["xmin"]
    techo = ahora["marco"]["ymin"] + ahora["marco"]["alto"]
    holgado = [x0, techo - nat8["alto"] - 5, x0 + nat8["ancho"] + 5, techo]
    r = resultado_de(t("set_legend", {"recuadro": holgado}, nota="(recuadro holgado)"))
    m = (r.get("ahora") or {}).get("marco") or {}
    comprobar("recuadro: el marco queda al tamano natural, arriba a la izquierda",
              (r.get("encaje") or {}).get("cabe") is True
              and abs(m.get("ancho", 0) - nat8["ancho"]) < 0.05
              and abs(m.get("alto", 0) - nat8["alto"]) < 0.05
              and abs(m.get("xmin", 0) - x0) < 0.01
              and abs(m.get("ymin", 0) + m.get("alto", 0) - techo) < 0.01,
              "marco=%s natural=%s" % (m, nat8))
    comprobar("recuadro: la letra NO se escala al encajar",
              (r.get("ahora") or {}).get("tamano_max") == 8, str((r.get("ahora") or {}).get("tamano_max")))
    comprobar("recuadro: el marco deja de ser fijo (sigue al contenido)",
              (r.get("ahora") or {}).get("marco_fijo") is False, str((r.get("ahora") or {}).get("marco_fijo")))
    estrecho = [x0, techo - 0.5, x0 + 0.5, techo]
    r = resultado_de(t("set_legend", {"recuadro": estrecho}, nota="(recuadro estrecho)"))
    enc = r.get("encaje") or {}
    comprobar("recuadro estrecho: dice que no cabe y cuanto falta",
              enc.get("cabe") is False and enc.get("sobra_alto", 0) < 0, str(enc)[:110])
else:
    comprobar("set_legend: hay medida para probar el recuadro", False, str(ahora)[:110])
if (e0.get("entradas") or 0) >= 2:
    r = resultado_de(t("set_legend", {"columnas": 2}, nota="(columnas=2)"))
    comprobar("columnas=2: leidas de la leyenda", (r.get("ahora") or {}).get("columnas") == 2,
              str((r.get("ahora") or {}).get("columnas")))
t("set_legend", {"recuadro": [1, 2, 3]}, espera_ok=False, nota="(recuadro mal formado)")
t("set_legend", {"leyenda": "regresion_no_existe"}, espera_ok=False, nota="(leyenda inexistente)")
# La medida no puede depender de la vista activa: en vivo se usa la pantalla del layout
# que ya tiene ArcMap, sin activar otra.
dfs = resultado_de(enviar("list_data_frames"))
activo = dfs.get("activo")
if activo:
    natc = resultado_de(enviar("set_legend", {})).get("natural")  # en la vista con que abrio el documento
    t("set_active_df", {"nombre": activo}, nota="(pasar a vista de datos)")
    e1 = resultado_de(t("set_legend", {}, nota="(estado en vista de datos)"))
    nat1 = e1.get("natural") if isinstance(e1.get("natural"), dict) else None
    comprobar("la medida es la misma en vista de datos",
              nat1 is not None and isinstance(natc, dict)
              and abs(nat1["ancho"] - natc["ancho"]) < 0.01 and abs(nat1["alto"] - natc["alto"]) < 0.01,
              "datos=%s otra=%s" % (nat1, natc))
t("remove_layer", {"capa": "regresion_leyenda"})

sys.stdout.write("--- arreglos de la revision de la 2.17.0 (3.0) ---" + chr(10))
# Cada caso falla con el add-in anterior (control).
t("add_layer", {"fuente": VEC, "nombre": "regresion_rev"})
t("rename_layer", {"capa": "regresion_rev", "nombre_nuevo": "a" + SEP + "b"}, espera_ok=False,
  nota="(barra invertida en el nombre)")
# Lo que va entre comillas es texto: antes pedia un campo `ha`.
t("set_labels", {"capa": "regresion_rev", "expresion": "[Municipio] & \" [ha]\"", "activar": True},
  nota="(corchetes dentro de un texto)")
t("set_labels", {"capa": "regresion_rev", "activar": False})
# Con una tabla unida, ArcMap solo dibuja el campo con su nombre COMPLETO (medido en dos PNG: con
# el nombre corto salen idénticos a sin etiquetas). Antes la comprobación aceptaba la cola.
CSV_UNION = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "union_regresion.csv"
with open(CSV_UNION, "w") as fh:
    fh.write("Municipio,Unico\nAledo,A\nTotana,T\n")
t("add_layer", {"fuente": VEC, "nombre": "regresion_union"})
if enviar("run_geoprocessing", {"tool": "management.AddJoin",
                                "params": ["regresion_union", "Municipio", CSV_UNION, "Municipio"]}).get("ok"):
    r = t("set_labels", {"capa": "regresion_union", "expresion": "[Unico]"}, espera_ok=False,
          nota="(campo unido sin calificar)")
    comprobar("union: el error da el nombre completo", "union_regresion.csv.Unico" in str((r or {}).get("error")),
              str((r or {}).get("error"))[:110])
    t("set_labels", {"capa": "regresion_union", "expresion": "[union_regresion.csv.Unico]"},
      nota="(campo unido con su nombre completo)")
    # where sobre el campo unido: se validaba contra la clase sin la union y se rechazaba un filtro
    # que ArcMap si dibuja (revision 2026-10-25, hipotesis 7). Control: 'A' (Aledo) rotula y 'Z' no,
    # asi que las dos imagenes difieren; y un campo que no existe sigue siendo error.
    t("set_labels", {"capa": "regresion_union", "where": "union_regresion.csv.Unico = 'NoExiste_2170' AND x = 1"},
      espera_ok=False, nota="(where con un campo que no hay)")
    t("set_extent", {"capa": "regresion_union"})
    png_a = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "where_union_A.png"
    png_z = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "where_union_Z.png"
    ok_a = t("set_labels", {"capa": "regresion_union", "where": "union_regresion.csv.Unico = 'A'", "tamano": 20},
             nota="(where sobre el campo unido)")
    t("export_view_png", {"salida": png_a, "dpi": 72})
    t("set_labels", {"capa": "regresion_union", "where": "union_regresion.csv.Unico = 'Z'"}, nota="(no casa)")
    t("export_view_png", {"salida": png_z, "dpi": 72})
    try:
        distintas = huella(png_a) != huella(png_z)
    except OSError:
        distintas = False
    comprobar("where sobre campo unido: se acepta y ArcMap lo dibuja (PNG 'A' != 'Z')",
              (ok_a or {}).get("ok") and distintas)
else:
    comprobar("AddJoin para la prueba de la union", False)
t("remove_layer", {"capa": "regresion_union"})

# Categoria por VALOR antes que por etiqueta: con la clase 1 etiquetada "2", la categoria "2"
# casaba con dos clases y la 2 no se podia alcanzar.
campos = (resultado_de(enviar("list_fields", {"capa": "regresion_rev"})).get("campos") or [])
numerico = next((c.get("nombre") for c in campos if c.get("nombre") not in ("FID", "OBJECTID")
                 and str(c.get("tipo", "")).lower() in ("double", "integer", "smallinteger", "single")), None)
if numerico and enviar("set_graduated_symbology", {"capa": "regresion_rev", "campo": numerico,
                                                   "num_clases": 3}).get("ok"):
    t("edit_symbol", {"capa": "regresion_rev", "categoria": "1", "etiqueta": "2"}, nota="(clase 1 -> «2»)")
    r = resultado_de(t("edit_symbol", {"capa": "regresion_rev", "categoria": "2", "etiqueta": "Dos"},
                       nota="(categoria 2 = valor, no la etiqueta)"))
    et = [c.get("etiqueta") for c in r.get("clases") or []]
    comprobar("categoria ambigua: manda el valor", et[:2] == ["2", "Dos"], str(et))
else:
    comprobar("capa con campo numerico para graduar", False, str([c.get("tipo") for c in campos])[:110])
t("remove_layer", {"capa": "regresion_rev"})

# open_mxd del mismo documento: no se reabre (no habria forma de saber cuando termina).
mxd_abierto = resultado_de(enviar("get_arcmap_info")).get("mxd")
if mxd_abierto:
    t("open_mxd", {"ruta": mxd_abierto, "cambios_sin_guardar": "descartar"}, espera_ok=False,
      nota="(el documento ya abierto)")

# Export sobre un plano viejo ABIERTO por otro programa: no se pierde ninguno de los dos.
import ctypes
import glob
from ctypes import wintypes
DIR_EXP = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion"
JPG = DIR_EXP + SEP + "regresion_sobrescribir.jpg"
borrar_fichero(JPG)
t("export_jpg", {"salida": JPG, "dpi": 30}, nota="(primer plano)")
tam_viejo = os.path.getsize(JPG) if os.path.exists(JPG) else -1
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
for compartir, nombre in ((1 | 2, "visor que bloquea"), (1 | 2 | 4, "visor que deja borrar")):
    h = k32.CreateFileW(JPG, 0x80000000, compartir, None, 3, 0x80, None)
    try:
        r = t("export_jpg", {"salida": JPG, "dpi": 40}, espera_ok=(compartir & 4) != 0, nota="(" + nombre + ")")
    finally:
        k32.CloseHandle(h)
    restos = [f for f in glob.glob(DIR_EXP + SEP + "regresion_sobrescribir*") if f != JPG]
    if compartir & 4:
        comprobar("export (" + nombre + "): el nuevo en su sitio y sin restos",
                  os.path.exists(JPG) and os.path.getsize(JPG) != tam_viejo and not restos,
                  "existe=%s restos=%s" % (os.path.exists(JPG), restos))
    else:
        comprobar("export (" + nombre + "): el viejo intacto y sin restos",
                  os.path.exists(JPG) and os.path.getsize(JPG) == tam_viejo and not restos,
                  "tam=%s viejo=%s restos=%s" % (os.path.getsize(JPG) if os.path.exists(JPG) else None,
                                                tam_viejo, restos))
    for f in restos:
        borrar_fichero(f)
borrar_fichero(JPG)

sys.stdout.write("--- herramientas nuevas: textos por patron, cortes, leyenda, duplicar, recorte (3.0) ---" + chr(10))
# Con el add-in anterior: los parametros nuevos se ignoran o el comando no existe (control).
import re as _re
textos = [e for e in resultado_de(enviar("list_layout_elements", {"tipo": "TEXT_ELEMENT"})).get("elementos", [])
          if len((e.get("texto") or "").strip()) >= 4]
if len(textos) >= 2:
    t("set_text_element", {"patron": "."}, espera_ok=False, nota="(patron que casa con varios, sin todos)")
    r = resultado_de(t("set_text_element", {"patron": "^(.)", "reemplazo": "$1", "todos": True},
                       nota="(todos, sin cambio real)"))
    comprobar("patron + todos: cambia todos los que casan", r.get("cambiados", 0) >= 2
              and all(c["antes"] == c["ahora"] for c in r.get("elementos", [])), str(r)[:110])
    # todos=True con grupo: antes el grupo solo desempataba y con todos se ignoraba, asi que
    # cambiaban los textos de todo el layout (revision 2026-10-25, alto 1). Con el add-in anterior,
    # 'cambiados' son todos los que casan, no solo los del grupo.
    # Se cuenta sobre TODOS los textos no vacios ('^(.)' casa con cualquiera), no sobre `textos`,
    # que deja fuera los de menos de 4 caracteres (un numero de hoja dentro del cajetin).
    no_vacios = [e for e in resultado_de(enviar("list_layout_elements", {"tipo": "TEXT_ELEMENT"}))
                 .get("elementos", []) if (e.get("texto") or "") != ""]
    en_grupo = [e for e in no_vacios if e.get("grupo")]
    sueltos = [e for e in no_vacios if not e.get("grupo")]
    if en_grupo and sueltos:
        g = en_grupo[0]["grupo"]
        del_grupo = [e for e in no_vacios if e.get("grupo") == g]
        r = resultado_de(t("set_text_element", {"patron": "^(.)", "reemplazo": "$1", "todos": True, "grupo": g},
                           nota="(todos + grupo, sin cambio real)"))
        comprobar("patron + todos + grupo: solo los del grupo",
                  r.get("cambiados") == len(del_grupo)
                  and all(c.get("grupo") == g for c in r.get("elementos", [])), str(r)[:110])
        r = resultado_de(t("set_text_element", {"patron": "^(.)", "reemplazo": "$1", "todos": True, "grupo": ""},
                           nota="(todos + grupo=\"\": solo los sueltos)"))
        comprobar("patron + todos + grupo=\"\": solo los sueltos",
                  r.get("cambiados") == len(sueltos)
                  and all(not c.get("grupo") for c in r.get("elementos", [])), str(r)[:110])
    else:
        sys.stdout.write("  [-- ] el layout no tiene textos en grupo Y sueltos: no se prueba todos + grupo" + chr(10))
    unico = next((e for e in textos
                  if sum(1 for o in textos if e["texto"][:4] in (o.get("texto") or "")) == 1), None)
    if unico:
        trozo = unico["texto"][:4]
        r = resultado_de(t("set_text_element", {"patron": _re.escape(trozo), "reemplazo": trozo + "~R"},
                           nota="(sustituye solo una parte)"))
        comprobar("patron: solo cambia la parte que casa",
                  r.get("texto_nuevo") == unico["texto"].replace(trozo, trozo + "~R", 1)
                  or r.get("texto_nuevo") == _re.sub(_re.escape(trozo), trozo + "~R", unico["texto"]),
                  str(r)[:110])
        t("set_text_element", {"patron": "~R", "reemplazo": ""}, nota="(deshacer)")
        t("set_text_element", {"buscar": unico["texto"][:4], "patron": "NO-CASA-2170", "reemplazo": "x"},
          espera_ok=False, nota="(el patron no casa con el elegido)")
    t("set_text_element", {"patron": "a", "texto": "x"}, espera_ok=False, nota="(patron y texto a la vez)")
else:
    comprobar("layout con al menos dos textos", False, str(textos)[:110])

# Cortes explicitos y etiquetas automaticas.
t("add_layer", {"fuente": VEC, "nombre": "regresion_cortes"})
campos = resultado_de(enviar("list_fields", {"capa": "regresion_cortes"})).get("campos") or []
numerico = next((c.get("nombre") for c in campos if c.get("nombre") not in ("FID", "OBJECTID")
                 and str(c.get("tipo", "")).lower() in ("double", "integer", "smallinteger", "single")), None)
valores = sorted(float(v) for v in resultado_de(enviar("get_unique_values", {"capa": "regresion_cortes",
                                                                             "campo": numerico or "FID"}))
                 .get("valores", []) if isinstance(v, (int, float)))
if numerico and len(valores) >= 3:
    c1, c2, c3 = valores[len(valores) // 3], valores[2 * len(valores) // 3], valores[-1]
    if c1 < c2 < c3:
        r = resultado_de(t("set_graduated_symbology", {"capa": "regresion_cortes", "campo": numerico,
                                                       "cortes": [c1, c2, c3], "etiquetas": "auto"},
                           nota="(cortes + etiquetas auto)"))
        comprobar("cortes: leidos del renderer", [round(x, 6) for x in r.get("cortes", [])[1:]]
                  == [round(c1, 6), round(c2, 6), round(c3, 6)], str(r.get("cortes")))
        et = r.get("etiquetas") or []
        comprobar("etiquetas auto: la primera con «≤» y la ultima con «>»",
                  len(et) == 3 and et[0].startswith("≤") and et[2].startswith(">"), str(et))
    t("set_graduated_symbology", {"capa": "regresion_cortes", "campo": numerico, "cortes": [c1, c3],
                                  "num_clases": 4}, espera_ok=False, nota="(cortes y num_clases)")
    t("set_graduated_symbology", {"capa": "regresion_cortes", "campo": numerico, "cortes": [c3, c1]},
      espera_ok=False, nota="(cortes desordenados)")
else:
    comprobar("campo numerico con valores para los cortes", False, str(valores)[:110])
r = resultado_de(t("set_raster_symbology", {"capa": "real_NUEVO.tif", "cortes": [64, 128, 255], "etiquetas": "auto"},
                   nota="(raster: cortes)"))
comprobar("raster: cortes leidos del renderer", r.get("cortes") == [64, 128, 255], str(r.get("cortes")))

# duplicate_layer: la copia queda debajo, con simbologia PROPIA.
r = resultado_de(t("duplicate_layer", {"capa": "regresion_cortes", "nombre_nuevo": "regresion_copia",
                                       "etiquetas": False}, nota="(duplicar)"))
orden = r.get("orden") or []
comprobar("duplicar: la copia justo debajo y con simbologia propia",
          "regresion_copia" in orden and orden.index("regresion_copia") == orden.index("regresion_cortes") + 1
          and r.get("simbologia_propia") is True and r.get("etiquetas") is False, str(r)[:110])
t("set_single_symbology", {"capa": "regresion_copia", "color_relleno": "#123456"})
orig = resultado_de(enviar("edit_symbol", {"capa": "regresion_cortes"}))
comprobar("duplicar: cambiar la copia no toca la original", orig.get("renderer") != "simple"
          and "#123456" not in json.dumps(orig), str(orig.get("renderer")))
t("duplicate_layer", {"capa": "regresion_cortes", "nombre_nuevo": "regresion_copia"}, espera_ok=False,
  nota="(nombre que ya existe)")

# move_layer(en_leyenda): la entrada sigue a la capa en la TOC.
t("add_layer", {"fuente": VEC, "nombre": "regresion_ley_a"})
t("add_layer", {"fuente": VEC, "nombre": "regresion_ley_b"})
r = resultado_de(t("move_layer", {"capa": "regresion_ley_a", "referencia": "regresion_ley_b", "posicion": "BEFORE",
                                  "en_leyenda": True}, nota="(en_leyenda)"))
ley = (r.get("leyendas") or [{}])[0]
o = ley.get("orden") or []
comprobar("en_leyenda: la entrada queda justo encima de la de la capa de debajo",
          "regresion_ley_a" in o and "regresion_ley_b" in o
          and o.index("regresion_ley_a") == o.index("regresion_ley_b") - 1, str(ley)[:110])

# set_frame_clip: recorte a la forma de una capa, con una capa fuera del recorte.
JPG_CLIP = DIR_EXP + SEP + "regresion_recorte.jpg"
e0 = resultado_de(t("set_frame_clip", {}, nota="(estado)"))
t("set_frame_clip", {"capa": "real_NUEVO.tif"}, espera_ok=False, nota="(capa que no es de poligonos)")
r = resultado_de(t("set_frame_clip", {"capa": "regresion_cortes", "where": "FID = 0",
                                      "excluir": ["regresion_ley_b"]}, nota="(recortar)"))
comprobar("recorte: Shape, con la forma y la excluida leidas", r.get("recorte") == "Shape"
          and (r.get("area_forma") or 0) > 0 and r.get("excluidas") == ["regresion_ley_b"], str(r)[:110])
t("export_jpg", {"salida": JPG_CLIP, "dpi": 40}, nota="(recortado)")
tam_recortado = os.path.getsize(JPG_CLIP) if os.path.exists(JPG_CLIP) else -1
r = resultado_de(t("set_frame_clip", {"quitar": True}, nota="(quitar)"))
comprobar("quitar: sin recorte y sin excluidas", r.get("recorte") == "None" and r.get("excluidas") == [],
          str(r)[:110])
t("export_jpg", {"salida": JPG_CLIP, "dpi": 40}, nota="(sin recorte)")
comprobar("el recorte se ve en el export (el JPG cambia)",
          tam_recortado > 0 and os.path.getsize(JPG_CLIP) != tam_recortado,
          "%s / %s" % (tam_recortado, os.path.getsize(JPG_CLIP) if os.path.exists(JPG_CLIP) else None))
borrar_fichero(JPG_CLIP)
for c in ("regresion_ley_a", "regresion_ley_b", "regresion_copia", "regresion_cortes"):
    enviar("remove_layer", {"capa": c})

sys.stdout.write("--- huecos 11-15 de la auditoria de scripts (2026-10-10) ---" + chr(10))
# add_layer(si_existe): relanzar una receta no duplica la capa.
t("add_layer", {"fuente": VEC, "nombre": "regresion_h"})
r = resultado_de(t("add_layer", {"fuente": VEC, "nombre": "regresion_h", "si_existe": "omitir"}, nota="(omitir)"))
comprobar("si_existe=omitir: no la anade y dice cual hay", r.get("omitida") is True
          and len(r.get("existentes") or []) == 1, str(r)[:110])
t("add_layer", {"fuente": VEC, "nombre": "regresion_h", "si_existe": "error"}, espera_ok=False, nota="(error)")
# set_definition_query(anadir): AND con la que hay, sin repetirla.
t("set_definition_query", {"capa": "regresion_h", "query": "FID >= 0"})
r = resultado_de(t("set_definition_query", {"capa": "regresion_h", "query": "FID < 100000", "anadir": True},
                   nota="(anadir)"))
comprobar("anadir: (anterior) AND (nueva)", r.get("query_nueva") == "(FID >= 0) AND (FID < 100000)", str(r)[:110])
r = resultado_de(t("set_definition_query", {"capa": "regresion_h", "query": "FID < 100000", "anadir": True},
                   nota="(anadir otra vez)"))
comprobar("anadir otra vez: ya_estaba, sin repetir", r.get("ya_estaba") is True
          and r.get("query_nueva") == "(FID >= 0) AND (FID < 100000)", str(r)[:110])
# Simbolos: trama y borde discontinuo, leidos de vuelta.
r = resultado_de(t("set_single_symbology", {"capa": "regresion_h", "trama": {"angulo": 45, "separacion": 4,
                                                                             "color": "#00AA00"},
                                            "patron_linea": "discontinua"}, nota="(trama + discontinua)"))
c0 = (r.get("clases") or [{}])[0]
comprobar("trama y patron del borde leidos de vuelta", (c0.get("trama") or {}).get("color") == "#00AA00"
          and c0.get("patron_borde") == [9.0, 4.5], str(c0)[:110])
r = resultado_de(t("edit_symbol", {"capa": "regresion_h", "trama": False}, nota="(quitar trama)"))
c0 = (r.get("clases") or [{}])[0]
comprobar("sin trama: relleno liso con el color de la trama", c0.get("tipo_simbolo") == "relleno_simple"
          and c0.get("color_relleno") == "#00AA00", str(c0)[:110])
t("edit_symbol", {"capa": "regresion_h", "estilo_marcador": "rombo"}, espera_ok=False, nota="(estilo en poligonos)")
# save_layer_as_lyr: se escribe y se reabre.
LYR_H = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "regresion_h.lyr"
borrar_fichero(LYR_H)
r = resultado_de(t("save_layer_as_lyr", {"capa": "regresion_h", "ruta": LYR_H}, nota="(guardar .lyr)"))
comprobar("save_layer_as_lyr: verificado, con la query dentro", r.get("verificado") is True and os.path.isfile(LYR_H)
          and (r.get("leido") or {}).get("definition_query") == "(FID >= 0) AND (FID < 100000)", str(r)[:110])
t("save_layer_as_lyr", {"capa": "regresion_h", "ruta": LYR_H}, espera_ok=False, nota="(ya existe)")
borrar_fichero(LYR_H)
# set_legend: formato.
r = resultado_de(t("set_legend", {"parche": [20, 10], "separaciones": {"entre_entradas": 5},
                                  "negrita_nombres": True}, nota="(formato)"))
f = (r.get("ahora") or {}).get("formato") or {}
comprobar("set_legend formato: parche, separacion y negrita leidos", "20x10" in (f.get("parches") or [])
          and (f.get("separaciones") or {}).get("entre_entradas") == 5 and f.get("negrita_nombres") is True,
          str(f)[:110])
t("set_legend", {"separaciones": {"vertical": 3}}, espera_ok=False, nota="(separacion desconocida)")
# Fuente y tamano juntos: el IFontDisp redondeaba el tamano a pixeles (8,5 -> 8,25, por debajo
# de lo pedido) y una negrita sola movia el tamano. Prueba desde el cliente, 2026-10-10.
r = resultado_de(t("set_legend", {"fuente": "Arial", "tamano": 8.5, "negrita_nombres": True},
                   nota="(fuente + tamano 8,5)"))
ahora = r.get("ahora") or {}
comprobar("set_legend fuente + tamano 8,5: se lee 8,5", ahora.get("tamano_min") == 8.5
          and ahora.get("tamano_max") == 8.5, "min=%s max=%s" % (ahora.get("tamano_min"), ahora.get("tamano_max")))
r = resultado_de(t("set_legend", {"negrita_nombres": False}, nota="(solo negrita)"))
ahora = r.get("ahora") or {}
comprobar("set_legend solo negrita: el tamano no se mueve", ahora.get("tamano_min") == 8.5
          and ahora.get("tamano_max") == 8.5, "min=%s max=%s" % (ahora.get("tamano_min"), ahora.get("tamano_max")))
r = resultado_de(t("set_labels", {"capa": "regresion_h", "expresion": "[Municipio]", "fuente": "Arial",
                                  "tamano": 8.5}, nota="(etiquetas fuente + tamano 8,5)"))
tams = [k.get("tamano") for k in (r.get("clases") or [])]
comprobar("set_labels fuente + tamano 8,5: se lee 8,5", bool(tams) and all(x == 8.5 for x in tams), tams)
r = resultado_de(t("set_labels", {"capa": "regresion_h", "negrita": True}, nota="(etiquetas solo negrita)"))
tams = [k.get("tamano") for k in (r.get("clases") or [])]
comprobar("set_labels solo negrita: el tamano no se mueve", bool(tams) and all(x == 8.5 for x in tams), tams)
t("set_labels", {"capa": "regresion_h", "activar": False}, nota="(apagar)")
# Valores unicos SIN clases (en maplex_ligero.mxd): edit_symbol respondia ok sin cambiar nada.
SIN_CLASES = ("Plano 02 - Emplazamiento General/Afección a figuras de protección/"
              "Afección a figuras de protección")
e = resultado_de(enviar("edit_symbol", {"capa": SIN_CLASES}))
if e.get("renderer") == "valores_unicos" and e.get("clases") == []:
    r = t("edit_symbol", {"capa": SIN_CLASES, "patron_linea": "punteada"}, espera_ok=False,
          nota="(valores unicos sin clases)")
    comprobar("sin clases: el error remite a set_single_symbology",
              "set_single_symbology" in str((r or {}).get("error")), str(r)[:110])
else:
    sys.stdout.write("  [-- ] sin la capa de valores unicos sin clases en este documento: no se prueba" + chr(10))
# Simbolos de varias capas: la lectura decia «relleno» a secas y luego trama fallaba (2026-10-10).
# En maplex_ligero.mxd, ZEPA tiene relleno multicapa y LIC - ZEC el borde multicapa.
clases_h = resultado_de(enviar("edit_symbol", {"capa": "regresion_h"})).get("clases") or [{}]
comprobar("control: un simbolo simple no trae capas_simbolo ni capas_borde",
          "capas_simbolo" not in clases_h[0] and "capas_borde" not in clases_h[0], str(clases_h[0])[:110])
zepa = resultado_de(enviar("edit_symbol", {"capa": "ZEPA"}))
lic = resultado_de(enviar("edit_symbol", {"capa": "LIC - ZEC"}))
if zepa.get("clases") and lic.get("clases"):
    comprobar("multicapa: ZEPA trae capas_simbolo", (zepa["clases"][0].get("capas_simbolo") or 0) >= 1,
              str(zepa["clases"][0])[:110])
    comprobar("multicapa: LIC - ZEC trae capas_borde", (lic["clases"][0].get("capas_borde") or 0) >= 1,
              str(lic["clases"][0])[:110])
else:
    sys.stdout.write("  [-- ] sin ZEPA / LIC - ZEC en este documento: no se prueba el multicapa" + chr(10))
# copy_layout_elements: el logo del cajetin de la plantilla.
PLANTILLA = "C:" + SEP + "temp" + SEP + "2026-09-25_etiquetas" + SEP + "maplex_ligero.mxd"
n0 = len(resultado_de(enviar("list_layout_elements", {"tipo": "PICTURE_ELEMENT"})).get("elementos") or [])
r = resultado_de(t("copy_layout_elements", {"origen": PLANTILLA, "nombres": ["FEADER"]}, nota="(copiar logo)"))
n1 = len(resultado_de(enviar("list_layout_elements", {"tipo": "PICTURE_ELEMENT"})).get("elementos") or [])
comprobar("copy_layout_elements: una imagen mas en el layout", len(r.get("copiados") or []) == 1 and n1 == n0 + 1,
          "%s -> %s" % (n0, n1))
r = resultado_de(t("copy_layout_elements", {"origen": PLANTILLA, "nombres": ["FEADER"], "quitar": ["FEADER"]},
                   nota="(reemplazar)"))
n2 = len(resultado_de(enviar("list_layout_elements", {"tipo": "PICTURE_ELEMENT"})).get("elementos") or [])
comprobar("copy_layout_elements con quitar: las mismas imagenes", r.get("quitados") == 1 and n2 == n1, "%s / %s" % (n2, n1))
r = t("copy_layout_elements", {"origen": PLANTILLA, "nombres": ["no_existe_2170"]}, espera_ok=False,
      nota="(nombre que no hay)")
# Las cajas del error, con punto decimal: «[36,09, 26, 39,47, 27,2]» no se podia leer.
import re  # noqa: E402
cajas = re.findall(r"\[[^\]]*\]", str((r or {}).get("error")))
comprobar("copy_layout_elements: las cajas del error con punto decimal", bool(cajas)
          and not any(re.search(r"\d,\d", c) for c in cajas), cajas[:2])
enviar("remove_layer", {"capa": "regresion_h"})

sys.stdout.write("--- un error no deja la capa ni el texto cambiados (2026-10-28) ---" + chr(10))
# Revision 2026-10-25, medio 4: set_text_element cambiaba el texto y luego CambiarAspecto lanzaba;
# set_labels(copiar_de) sustituia las clases y luego la expresion copiada no valia. Con el add-in
# anterior, el error llega igual pero el texto y la expresion quedan cambiados.
# (El tercero, set_raster_symbology con cortes que ArcMap no respeta, no se sabe provocar.)
textos_antes = [e.get("texto") for e in resultado_de(enviar("list_layout_elements", {"tipo": "TEXT_ELEMENT"}))
                .get("elementos", [])]
unicos = [x for x in textos_antes if x and textos_antes.count(x) == 1 and "<" not in x]
if unicos:
    t("set_text_element", {"buscar": unicos[0], "texto": "ZZ-2170", "encajar_en": [0, 0, 1, 1], "margen": 1},
      espera_ok=False, nota="(encajar_en sin sitio, tras cambiar el texto)")
    t("set_text_element", {"patron": "^(.)", "reemplazo": "ZZ$1", "todos": True, "encajar_en": [0, 0, 1, 1],
                           "margen": 1}, espera_ok=False, nota="(patron + todos, idem)")
    textos_despues = [e.get("texto") for e in resultado_de(enviar("list_layout_elements",
                                                                  {"tipo": "TEXT_ELEMENT"})).get("elementos", [])]
    comprobar("set_text_element: tras el error, los textos como estaban", textos_despues == textos_antes,
              str([d for a, d in zip(textos_antes, textos_despues) if a != d])[:110])
else:
    sys.stdout.write("  [-- ] sin un texto unico en el layout: no se prueba set_text_element" + chr(10))
PUNTOS_RDS = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "disco" + SEP + "puntos_rds.shp"
etq0 = resultado_de(enviar("set_labels", {"capa": NOMBRE_VEC}))
if os.path.exists(PUNTOS_RDS) and etq0.get("clases"):
    # La capa de origen rotula [Id], que la de destino no tiene. puntos_rds.shp lo crea regresion_disco.py.
    t("add_layer", {"fuente": PUNTOS_RDS, "nombre": "regresion_origen_id"})
    t("set_labels", {"capa": "regresion_origen_id", "expresion": "[Id]"})
    t("set_labels", {"capa": NOMBRE_VEC, "copiar_de": "regresion_origen_id"}, espera_ok=False,
      nota="(copiar_de con un campo que no hay)")
    etq1 = resultado_de(enviar("set_labels", {"capa": NOMBRE_VEC}))
    comprobar("set_labels(copiar_de): tras el error, las clases como estaban",
              [c.get("expresion") for c in etq1.get("clases", [])] == [c.get("expresion") for c in etq0["clases"]]
              and etq1.get("etiquetas_activas") == etq0.get("etiquetas_activas"),
              str([c.get("expresion") for c in etq1.get("clases", [])])[:110])
    t("remove_layer", {"capa": "regresion_origen_id"})
else:
    sys.stdout.write("  [-- ] sin puntos_rds.shp (lo crea regresion_disco.py) o sin clases: no se prueba copiar_de"
                     + chr(10))

sys.stdout.write("--- issue #8: extension tras add/remove_layer y lectura de sin_relleno ---" + chr(10))
# 1.1 (la extension tras add/remove_layer): con el add-in 2.16.0 y un documento vacio, quitar y
# volver a anadir la capa movio la escala x1,10 (356.873 -> 392.560, 2026-10-28). Prueba el arreglo.
t("add_layer", {"fuente": VEC, "nombre": "regresion_ext"})
t("set_extent", {"capa": "regresion_ext"})
escala0 = (resultado_de(enviar("get_arcmap_info")) or {}).get("escala_activa")
t("remove_layer", {"capa": "regresion_ext"})
r_add = t("add_layer", {"fuente": VEC, "nombre": "regresion_ext"})
escala1 = (resultado_de(enviar("get_arcmap_info")) or {}).get("escala_activa")
comprobar("add/remove_layer: la escala no se mueve",
          escala0 and escala1 and abs(escala1 - escala0) <= 0.005 * escala0,
          "%s -> %s (add_layer: extension_restaurada=%s)" % (escala0, escala1,
                                                                (resultado_de(r_add) or {}).get("extension_restaurada")))
# 1.2 (el relleno hueco): el Hollow de ESRI.style NO sirve, trae Color.NullColor = true y la
# lectura sale bien sin el arreglo (aviso de HeyElowen en el issue). El caso real es
# Style = esriSFSNull con un Color negro VIVO, que ninguna tool deja: viene de un .lyr hecho con
# ArcObjects (tests/generar_fixture_hueco.py). Con 2.16.0 este bloque debe FALLAR en las dos.
# El simetrico NO se puede juzgar con edit_symbol: con 2.16.0 responde #FF0000 solido y debajo
# Style sigue en esriSFSNull (no se dibuja). Se lee aparte, por arcpy + ArcObjects sobre la sesion.
LEER_ESTILO = (
    "import os, tempfile\n"
    "import comtypes.client as cc\n"
    "lyr = [l for l in MAP.ListLayers(mxd) if l.name == 'regresion_ext'][0]\n"
    "dest = os.path.join(tempfile.mkdtemp(), 'estilo.lyr')\n"
    "lyr.saveACopy(dest)\n"
    "COM = r'C:\\Program Files (x86)\\ArcGIS\\Desktop10.5\\com'\n"
    "m = lambda n: cc.GetModule(os.path.join(COM, n + '.olb'))\n"
    "sysm = m('esriSystem'); carto = m('esriCarto'); disp = m('esriDisplay')\n"
    "cc.CreateObject(sysm.AoInitialize, interface=sysm.IAoInitialize).Initialize(60)\n"
    "lf = cc.CreateObject(carto.LayerFile, interface=carto.ILayerFile)\n"
    "lf.Open(dest)\n"
    "s = lf.Layer.QueryInterface(carto.IGeoFeatureLayer).Renderer.QueryInterface("
    "carto.ISimpleRenderer).Symbol.QueryInterface(disp.ISimpleFillSymbol)\n"
    "c = s.Color; rgb = c.QueryInterface(disp.IRgbColor)\n"
    "RESULT = {'style': s.Style, 'null_color': bool(c.NullColor), "
    "'rgb': [rgb.Red, rgb.Green, rgb.Blue]}\n"
    "lf.Close()\n")
LYR_HUECO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                         "relleno_hueco_color_negro.lyr")
t("apply_symbology_from_layer", {"capa": "regresion_ext", "lyr_file": LYR_HUECO},
  nota="(esriSFSNull con Color negro no nulo)")
leido = resultado_de(enviar("edit_symbol", {"capa": "regresion_ext"})) or {}
clase = (leido.get("clases") or [{}])[0]
comprobar("edit_symbol lee el esriSFSNull de color vivo como sin_relleno",
          clase.get("sin_relleno") is True and clase.get("color_relleno") is None, str(clase))
r = resultado_de(t("edit_symbol", {"capa": "regresion_ext", "color_relleno": "#FF0000"},
                   nota="(color sobre el hueco: pasa a solido)")) or {}
clase = (r.get("clases") or [{}])[0]
comprobar("edit_symbol: un color sobre el hueco responde solido",
          clase.get("sin_relleno") is False and clase.get("color_relleno") == "#FF0000", str(clase))
real = resultado_de(enviar("execute_code", {"code": LEER_ESTILO, "serializar_sesion": True},
                           timeout=300))
real = real.get("result") if isinstance(real.get("result"), dict) else real
comprobar("edit_symbol: un color sobre el hueco se dibuja (Style solido leido por ArcObjects)",
          real.get("style") == 0 and real.get("null_color") is False and real.get("rgb") == [255, 0, 0],
          str(real))
t("remove_layer", {"capa": "regresion_ext"})

sys.stdout.write("--- limpieza ---" + chr(10))
t("remove_layer", {"capa": "real_NUEVO.tif"})
t("remove_layer", {"capa": NOMBRE_VEC})
# Las salidas de geoproceso entran solas en la TOC (AddOutputsToMap), asi que sin
# esto cada pase deja ocho capas mas en el documento. Sin puntuar: que una no este
# es normal si su geoproceso fallo, y contarlo como fallo seria ruido.
for sobra in ("mv_a", "mv_b", "mv_merge", "merge_ext", "compuesto.tif",
              "gfc_a", "gfc_b", "gfc_merge", "merge_dq"):
    try:
        enviar("remove_layer", {"capa": sobra})
    except Exception:
        pass

sys.stdout.write(chr(10) + "RESULTADO: %d correctos, %d fallos" % (ok_n, len(fallos)) + chr(10))
sys.stdout.write("E_PENDING ('dibujando: ') resuelto con reintento: %s%s"
                 % (REINTENTOS_DIBUJO or "ninguno", chr(10)))
for nombre, det in fallos:
    sys.stdout.write("   FALLO %-28s %s" % (nombre, det) + chr(10))
sys.exit(1 if fallos else 0)
