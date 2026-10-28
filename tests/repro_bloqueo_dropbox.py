# -*- coding: utf-8 -*-
# Repro del bloqueo de Dropbox (pendiente arcmap-mcp-reintento-bloqueo-dropbox).
# Uso (Python 2.7 de ArcGIS, con ArcMap CERRADO):
#   C:\Python27\ArcGIS10.5\python.exe tests\repro_bloqueo_dropbox.py <carpeta> <shapefile fuente> [N]
#   <carpeta>: carpeta de prueba en el Dropbox con una COPIA del plano llamada copia_Plano02.mxd
#   (sin marcos OLE: guardarlo desde arcpy lo corromperia).
# Resultado del 2026-10-10 en el Dropbox de trabajo: 40 pasos, 0 fallos (no se reproduce). Junto con
# edit_mxd_lote y export_mxd_lote encadenados, 54 pasos sin un fallo.
# Solo trabaja sobre COPIAS en la carpeta de prueba que recibe como argumento.
# 1) arcpy: guardar el .mxd y abrirlo en seguida, N veces.
# 2) shapefile: CopyFeatures a la carpeta y SearchCursor en seguida, N veces.
import os, sys, time, traceback, json
import arcpy

carpeta = sys.argv[1]
mxd = os.path.join(carpeta, "copia_Plano02.mxd")
fuente_shp = sys.argv[2]
N = int(sys.argv[3]) if len(sys.argv) > 3 else 10
salida = []

def anotar(prueba, i, fase, ok, seg, err=None):
    fila = {"prueba": prueba, "i": i, "fase": fase, "ok": ok, "s": round(seg, 2)}
    if err:
        fila["error"] = err
    salida.append(fila)
    sys.stdout.write(json.dumps(fila) + "\n")
    sys.stdout.flush()

for i in range(N):
    t = time.time()
    try:
        m = arcpy.mapping.MapDocument(mxd)
        m.save()
        del m
        anotar("mxd", i, "guardar", True, time.time() - t)
    except Exception as e:
        anotar("mxd", i, "guardar", False, time.time() - t, "%s: %s" % (type(e).__name__, e))
        continue
    t = time.time()
    try:
        m = arcpy.mapping.MapDocument(mxd)
        n = len(arcpy.mapping.ListLayers(m))
        del m
        anotar("mxd", i, "abrir", True, time.time() - t)
    except Exception as e:
        anotar("mxd", i, "abrir", False, time.time() - t, "%s: %s" % (type(e).__name__, e))

for i in range(N):
    destino = os.path.join(carpeta, "shp_%02d.shp" % i)
    t = time.time()
    try:
        arcpy.CopyFeatures_management(fuente_shp, destino)
        anotar("shp", i, "escribir", True, time.time() - t)
    except Exception as e:
        anotar("shp", i, "escribir", False, time.time() - t, "%s: %s" % (type(e).__name__, e))
        continue
    t = time.time()
    try:
        with arcpy.da.SearchCursor(destino, ["OID@"]) as cur:
            filas = sum(1 for _ in cur)
        anotar("shp", i, "leer", True, time.time() - t)
    except Exception as e:
        anotar("shp", i, "leer", False, time.time() - t, "%s: %s" % (type(e).__name__, e))

fallos = [f for f in salida if not f["ok"]]
sys.stdout.write("RESUMEN %d pasos, %d fallos\n" % (len(salida), len(fallos)))
