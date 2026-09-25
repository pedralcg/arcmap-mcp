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
El objetivo es detectar si algo se rompio al reinstalar el add-in cinco veces.
"""
import json
import os
import socket
import sys

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
