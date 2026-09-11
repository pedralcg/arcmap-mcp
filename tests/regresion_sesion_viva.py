# -*- coding: utf-8 -*-
"""regresion_sesion_viva.py  --  Barrido MANUAL del catalogo contra ArcMap vivo.

NO es una suite automatica (esa es test_client_protocol.py, que corre sin ArcMap):
esto EXIGE ArcMap abierto con el puente arrancado, y por eso no lleva prefijo
"test_" ni lo recoge `unittest discover`.

Uso:  python tests/regresion_sesion_viva.py     (sale 1 si algo falla)

Para que sirve: despues de tocar el add-in y reinstalarlo, confirmar que no se ha
roto nada por el camino. Cubre 33 llamadas.

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
VEC = ("C:" + SEP + "temp" + SEP + "20260710_export_coberturas_id2025_024" + SEP
       + "Áreas Naturales de Interés Turístico.shp")
NOMBRE_VEC = "Áreas Naturales de Interés Turístico"

# Raster CATEGORICO (mascara de visibilidad 0/1/2 con NoData 255) y su .lyr de
# valores unicos: el caso que el modo clasificado no puede cubrir.
VIS_DIR = "C:" + SEP + "temp" + SEP + "20260911_lyr_visibilidad_rcd"
VIS_TIF = ("F:" + SEP + "Esteban Dropbox" + SEP + "Pedro Alcoba" + SEP
           + "ID2026_035_Estudio Paisajistico RCD Rosi" + SEP + "01_GIS" + SEP + "01_Datos"
           + SEP + "Visibilidad_plataformas" + SEP + "Vis_plataformas_acumulado_sin_pantalla.tif")
VIS = "Vis_plataformas_acumulado_sin_pantalla.tif"
LYR_VIS = VIS_DIR + SEP + "Vis_plataformas_acumulado_sin_pantalla.lyr"
LYR_GRUPO = VIS_DIR + SEP + "05_Analisis_de_visibilidad.lyr"

ok_n = 0
fallos = []


def enviar(tipo, params=None, timeout=180):
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

sys.stdout.write("--- limpieza ---" + chr(10))
t("remove_layer", {"capa": "real_NUEVO.tif"})
t("remove_layer", {"capa": NOMBRE_VEC})

sys.stdout.write(chr(10) + "RESULTADO: %d correctos, %d fallos" % (ok_n, len(fallos)) + chr(10))
for nombre, det in fallos:
    sys.stdout.write("   FALLO %-28s %s" % (nombre, det) + chr(10))
sys.exit(1 if fallos else 0)
