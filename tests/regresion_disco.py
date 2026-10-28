# -*- coding: utf-8 -*-
"""regresion_disco.py  --  edit_mxd_lote (modo disco, ADR-010) contra .mxd REALES.

Como regresion_sesion_viva.py, es MANUAL: exige el add-in instalado (de su .esriaddin sale
arcmap-mcp-disco.exe) y, para dos bloques, ArcMap abierto con el puente. Trabaja sobre COPIAS
en C:\\temp\\arcmap-mcp-regresion\\disco; nunca sobre un proyecto.

    python tests/regresion_disco.py        (sale 1 si algo falla)

OJO al probar un arreglo recién compilado SIN reinstalar el add-in: el servidor usa primero el
arcmap-mcp-disco.exe del .esriaddin INSTALADO, no el del repo, y la regresión pasa contra el código
viejo sin decirlo. Para el del repo: ARCMAP_DISCO_EXE=addin\\ArcmapMcp.Disco\\bin\\Release\\arcmap-mcp-disco.exe
(2026-10-28: el control de la capa rota «fallaba» con el arreglo ya compilado).

Cada bloque lleva su CONTROL, porque una verificación que nunca falla no prueba nada:
  - lo guardado se comprueba con arcpy en otro proceso, no con lo que dice el ejecutable;
  - todo o nada: con una operación que falla, el .mxd queda idéntico byte a byte y sin .bak;
  - se niega a tocar un .mxd con marcos OLE y el documento abierto en ArcMap;
  - la verificación al reabrir CAZA una pérdida real: una capa cuya ruta relativa llega a 262
    caracteres (ArcMap no la escribe) sale como diferencia; la misma capa cerca, no.
Y mide, como pide la ADR, el lote frente a open_mxd + las mismas operaciones + save_mxd.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import arcmap_mcp_server as s  # noqa: E402

SEP = chr(92)
BASE = "C:" + SEP + "temp" + SEP + "2026-09-25_etiquetas" + SEP + "maplex_ligero.mxd"
OLE = "C:" + SEP + "temp" + SEP + "2026-10-01_prueba_2170" + SEP + "ole_P09_copia.mxd"
VEC = ("C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP
       + "Áreas Naturales de Interés Turístico.shp")
DIR = "C:" + SEP + "temp" + SEP + "arcmap-mcp-regresion" + SEP + "disco"
OPS = [{"tool": "set_legend", "tamano": 8},
       {"tool": "set_legend_item", "capa": "guia_planos", "quitar": True},
       {"tool": "set_layer_visibility", "capa": "MUP", "visible": False}]

ok_n = 0
fallos = []


def comprobar(etiqueta, condicion, detalle=""):
    global ok_n
    if condicion:
        ok_n += 1
    else:
        fallos.append((etiqueta, str(detalle)[:160]))
    sys.stdout.write("  [%s] %s%s" % ("OK " if condicion else "FALLO", etiqueta, chr(10)))


def copia(nombre, origen=BASE, carpeta=DIR):
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, nombre + ".mxd")
    shutil.copy(origen, destino)
    for viejo in (destino + ".bak",):
        if os.path.exists(viejo):
            os.remove(viejo)
    return destino


def huella(ruta):
    with open(ruta, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def leer_con_arcpy(mxd):
    """Visibilidad de MUP y capas de la leyenda, leídas por arcpy en su propio proceso: un
    testigo que no comparte código con el ejecutable."""
    codigo = (
        "import arcpy, json, sys\n"
        "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
        "lyr = [l for l in arcpy.mapping.ListLayers(m) if l.name == 'MUP']\n"
        "leg = arcpy.mapping.ListLayoutElements(m, 'LEGEND_ELEMENT')\n"
        "print(json.dumps({'mup_visible': lyr[0].visible if lyr else None,\n"
        "                  'leyenda': [l.name for l in leg[0].listLegendItemLayers()] if leg else None}))\n")
    py = s._python27_arcgis()
    script = os.path.join(DIR, "_leer.py")
    with open(script, "w") as fh:
        fh.write(codigo)
    out = subprocess.run([py, script, mxd], capture_output=True, timeout=180, **s.SIN_VENTANA)
    return json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])


shutil.rmtree(DIR, ignore_errors=True)
exe = s._disco_exe()
sys.stdout.write("ejecutable: %s%s" % (exe, chr(10)))
comprobar("hay arcmap-mcp-disco.exe", exe is not None and os.path.isfile(exe), exe)

sys.stdout.write("--- lote de dos planos: guardado y verificado ---" + chr(10))
a, b = copia("plano_a"), copia("plano_b")
t0 = time.time()
r = s.edit_mxd_lote([a, b], OPS)
t_lote = (time.time() - t0) / 2
comprobar("lote: dos guardados y verificados", r.get("ok") and r["resumen"]["guardados"] == 2, json.dumps(r)[:300])
comprobar("lote: copia .bak de cada uno", all(os.path.isfile(m + ".bak") for m in (a, b)))
leido = leer_con_arcpy(a)
comprobar("arcpy lee MUP apagada", leido.get("mup_visible") is False, leido)
comprobar("arcpy lee la leyenda sin guia_planos", leido.get("leyenda") is not None
          and "guia_planos" not in leido["leyenda"], leido)
control = leer_con_arcpy(BASE)
comprobar("control: el original sigue con MUP encendida y guia_planos en la leyenda",
          control.get("mup_visible") is True and "guia_planos" in (control.get("leyenda") or []), control)

sys.stdout.write("--- operaciones y variables por plano, set_extent en el lote (2026-10-10) ---" + chr(10))
e1, e2 = copia("plano_e1"), copia("plano_e2")
EXT = {e1: [600000, 4200000, 610000, 4206000], e2: [620000, 4220000, 640000, 4232000]}
r = s.edit_mxd_lote(operaciones_por_mxd={m: [{"tool": "set_extent", "coords": c}] for m, c in EXT.items()},
                    variables_por_mxd={e1: {"T": "uno"}, e2: {"T": "dos"}},
                    operaciones=[{"tool": "rename_layer", "capa": "MUP", "nombre_nuevo": "MUP {T}"}])
comprobar("por plano: los dos guardados y verificados", r.get("ok") and r["resumen"]["guardados"] == 2,
          json.dumps(r)[:300])
codigo_ext = ("import arcpy, json, sys\n"
              "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
              "e = m.activeDataFrame.extent\n"
              "print(json.dumps({'ext': [e.XMin, e.YMin, e.XMax, e.YMax],"
              " 'capas': [l.name for l in arcpy.mapping.ListLayers(m) if l.name.startswith('MUP')]}))\n")
with open(os.path.join(DIR, "_ext.py"), "w") as fh:
    fh.write(codigo_ext)
for m, c in EXT.items():
    out = subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_ext.py"), m], capture_output=True,
                         timeout=180, **s.SIN_VENTANA)
    leido = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    ext = leido["ext"]
    contiene = ext[0] <= c[0] + 1 and ext[1] <= c[1] + 1 and ext[2] >= c[2] - 1 and ext[3] >= c[3] - 1
    comprobar("arcpy lee la extension pedida en %s" % os.path.basename(m), contiene, ext)
    sufijo = "uno" if m == e1 else "dos"
    comprobar("arcpy lee la variable de %s" % os.path.basename(m), ("MUP " + sufijo) in leido["capas"],
              leido["capas"])

sys.stdout.write("--- replace_data_source sobre una capa ROTA: geometria y campos (2026-10-28) ---" + chr(10))
# Antes, con la capa rota no habia FeatureClass vieja y se reapuntaba sin mirar nada: una capa de
# poligonos por valores unicos acababa en un .shp de puntos, o sin su campo, con ok y sin dibujar
# (revision 2026-10-25, medio 3). Con el add-in anterior, las dos primeras responden ok.
codigo_fc = ("import arcpy, sys\n"
             "sr = arcpy.SpatialReference(25830)\n"
             "for nombre, tipo in (('puntos_rds.shp', 'POINT'), ('poligonos_sin_campos_rds.shp', 'POLYGON')):\n"
             "    if arcpy.Exists(sys.argv[1] + '\\\\' + nombre):\n"
             "        arcpy.Delete_management(sys.argv[1] + '\\\\' + nombre)\n"
             "    arcpy.CreateFeatureclass_management(sys.argv[1], nombre, tipo, spatial_reference=sr)\n"
             "print('ok')\n")
os.makedirs(DIR, exist_ok=True)
with open(os.path.join(DIR, "_fc.py"), "w") as fh:
    fh.write(codigo_fc)
subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_fc.py"), DIR], capture_output=True, timeout=180,
               **s.SIN_VENTANA)
PUNTOS = os.path.join(DIR, "puntos_rds.shp")
SIN_CAMPOS = os.path.join(DIR, "poligonos_sin_campos_rds.shp")
base_vec = os.path.splitext(VEC)[0]
rota = os.path.join(DIR, "rota_rds")
for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
    if os.path.exists(base_vec + ext):
        shutil.copyfile(base_vec + ext, rota + ext)
g1 = copia("plano_g1")
r = s.edit_mxd_lote([g1], [{"tool": "add_layer", "fuente": rota + ".shp", "nombre": "rota"},
                           {"tool": "set_unique_values_symbology", "capa": "rota", "campo": "Municipio"}], bak=False)
comprobar("capa rota: preparada por valores unicos de Municipio", r.get("ok"), json.dumps(r)[:200])
for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
    if os.path.exists(rota + ext):
        os.remove(rota + ext)
if os.path.exists(PUNTOS) and os.path.exists(SIN_CAMPOS):
    r = s.edit_mxd_lote([g1], [{"tool": "replace_data_source", "capa": "rota", "fuente": PUNTOS}], bak=False)
    err = json.dumps(r, ensure_ascii=False)
    comprobar("capa rota de poligonos -> puntos: error sin guardar",
              not r["ok"] and r["documentos"][0].get("guardado") is False and "geometr" in err, err[:200])
    r = s.edit_mxd_lote([g1], [{"tool": "replace_data_source", "capa": "rota", "fuente": SIN_CAMPOS}], bak=False)
    err = json.dumps(r, ensure_ascii=False)
    comprobar("capa rota -> dataset sin el campo del renderer: error sin guardar",
              not r["ok"] and r["documentos"][0].get("guardado") is False and "Municipio" in err, err[:200])
    r = s.edit_mxd_lote([g1], [{"tool": "replace_data_source", "capa": "rota", "fuente": VEC}], bak=False)
    comprobar("control: la capa rota al dato bueno se reapunta y guarda", r.get("ok"), json.dumps(r)[:200])
else:
    comprobar("capa rota: arcpy no ha creado los shapefiles de prueba", False, [PUNTOS, SIN_CAMPOS])

sys.stdout.write("--- verificacion: una lectura que falla no verifica (2026-10-28) ---" + chr(10))
# Simbolizar "Zonas" y luego renombrarla: la lectura edit_symbol("Zonas") falla igual antes y
# despues de guardar, y al comparar cadenas salia verificado (revision 2026-10-25, medio 6).
v1, v2 = copia("plano_v1"), copia("plano_v2")
r = s.edit_mxd_lote([v1], [{"tool": "add_layer", "fuente": VEC, "nombre": "Zonas"},
                           {"tool": "set_unique_values_symbology", "capa": "Zonas", "campo": "Municipio"},
                           {"tool": "rename_layer", "capa": "Zonas", "nombre_nuevo": "Zonas renombrada"}], bak=False)
doc_v = (r.get("documentos") or [{}])[0]
fallidas = (doc_v.get("verificacion") or {}).get("lecturas_fallidas") or []
comprobar("lectura fallida: guardado pero NO verificado, con el motivo",
          not r["ok"] and doc_v.get("guardado") is True and r["resumen"]["no_verificados"] == 1
          and any(f.get("lectura") == "edit_symbol" for f in fallidas), json.dumps(r, ensure_ascii=False)[:300])
r = s.edit_mxd_lote([v2], [{"tool": "add_layer", "fuente": VEC, "nombre": "Zonas"},
                           {"tool": "set_unique_values_symbology", "capa": "Zonas", "campo": "Municipio"}], bak=False)
comprobar("control: sin renombrar, verificado", r.get("ok") and r["resumen"]["guardados"] == 1,
          json.dumps(r, ensure_ascii=False)[:200])

sys.stdout.write("--- set_scale como primera operacion se guarda (2026-10-28) ---" + chr(10))
# Sin activar la pantalla (como hace set_extent), set_scale respondia 12345 y el .mxd guardado
# seguia con la escala de antes, con ok y verificado (revision 2026-10-25, hipotesis 9).
s1 = copia("plano_s1")
r = s.edit_mxd_lote([s1], [{"tool": "set_scale", "escala": 12345}], bak=False)
# El MapDocument va en una variable: `MapDocument(ruta).activeDataFrame.scale` en una linea lo
# libera antes de leer y arcpy devuelve basura (29899,66 sobre un .mxd guardado a 12345, 2026-10-28).
codigo_escala = ("import arcpy, sys\n"
                 "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
                 "print(m.activeDataFrame.scale)\n")
with open(os.path.join(DIR, "_escala.py"), "w") as fh:
    fh.write(codigo_escala)
out = subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_escala.py"), s1], capture_output=True,
                     timeout=180, **s.SIN_VENTANA)
try:
    escala_leida = float(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
except (ValueError, IndexError):
    escala_leida = None
comprobar("set_scale: arcpy lee la escala pedida en el .mxd guardado",
          r.get("ok") and escala_leida is not None and abs(escala_leida - 12345) < 1, escala_leida)

sys.stdout.write("--- bajas de la revision: prefijos en el nombre y reemplazo en cascada (2026-10-28) ---" + chr(10))
b1 = copia("plano_b1")
r = s.edit_mxd_lote([b1], [{"tool": "add_layer", "fuente": VEC, "nombre": "Area cascada"},
                           {"tool": "rename_layer", "capa": "Area cascada", "nombre_nuevo": "visible:x"}], bak=False)
comprobar("rename_layer a 'visible:x': error sin guardar",
          not r["ok"] and r["documentos"][0].get("guardado") is False and "visible:" in json.dumps(r, ensure_ascii=False),
          json.dumps(r, ensure_ascii=False)[:200])
r = s.edit_mxd_lote([b1], [{"tool": "add_layer", "fuente": VEC, "nombre": "Area cascada"},
                           {"tool": "replace_text", "ambitos": ["capas"], "cambios": {"Area": "Zona", "Zona": "Área"}}],
                    bak=False)
codigo_cascada = ("import arcpy, json, sys\n"
                  "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
                  "print(json.dumps([l.name for l in arcpy.mapping.ListLayers(m) if l.name.endswith(' cascada')]))\n")
with open(os.path.join(DIR, "_cascada.py"), "w") as fh:
    fh.write(codigo_cascada)
out = subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_cascada.py"), b1], capture_output=True,
                     timeout=180, **s.SIN_VENTANA)
try:
    cascada = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
except (ValueError, IndexError):
    cascada = out.stderr.decode("utf-8", "replace")[-200:]
comprobar("replace_text en una pasada: {'Area':'Zona','Zona':'Área'} deja 'Zona', no 'Área'",
          r.get("ok") and cascada == ["Zona cascada"], cascada)

sys.stdout.write("--- replace_text: conservar_mayusculas respeta siglas y nombres (2026-10-28) ---" + chr(10))
# Antes: {"Murcia": "Region de Murcia"} daba "Region de murcia" y {"Rn2000": "RN2000"} dejaba
# "Rn2000" contando 0 cambios (revision 2026-10-25, medio 5). El control es "AREA" -> "ÁREA".
m1 = copia("plano_m1")
r = s.edit_mxd_lote([m1], [{"tool": "add_layer", "fuente": VEC, "nombre": "Murcia rds"},
                           {"tool": "add_layer", "fuente": VEC, "nombre": "Rn2000 rds"},
                           {"tool": "add_layer", "fuente": VEC, "nombre": "AREA rds"},
                           {"tool": "replace_text", "ambitos": ["capas"],
                            "cambios": {"Murcia": "Región de Murcia", "Rn2000": "RN2000", "Area": "Área"}}], bak=False)
# El lote recorta 'cambios' (y el plano base ya tiene otras capas con "Murcia"): los nombres se leen
# con arcpy en otro proceso.
codigo_nombres = ("import arcpy, json, sys\n"
                  "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
                  "print(json.dumps([l.name for l in arcpy.mapping.ListLayers(m) if l.name.endswith(' rds')]))\n")
with open(os.path.join(DIR, "_nombres.py"), "w") as fh:
    fh.write(codigo_nombres)
out = subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_nombres.py"), m1], capture_output=True,
                     timeout=180, **s.SIN_VENTANA)
try:
    rds = sorted(json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1]))
except (ValueError, IndexError):
    rds = out.stderr.decode("utf-8", "replace")[-200:]
comprobar("replace_text: siglas y nombres del reemplazo intactos, y el titulo en mayusculas",
          r.get("ok") and rds == ["RN2000 rds", "Región de Murcia rds", "ÁREA rds"], rds)

sys.stdout.write("--- replace_data_source: otro fichero, la simbologia se queda (2026-10-10) ---" + chr(10))
RAS = os.path.join("C:" + os.sep, "temp", "20260715_verif_qml2lyr", "real_NUEVO.tif")
if os.path.isfile(RAS):
    ras_copia = os.path.join(DIR, "real_COPIA.tif")
    shutil.copyfile(RAS, ras_copia)
    f1 = copia("plano_f1")
    COL = [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 0]]
    r = s.edit_mxd_lote([f1], [{"tool": "add_layer", "fuente": RAS, "nombre": "ras"},
                               {"tool": "set_raster_symbology", "capa": "ras", "num_clases": 4, "colores": COL,
                                "etiquetas": ["a", "b", "c", "d"]},
                               {"tool": "replace_data_source", "capa": "ras", "fuente": ras_copia}], bak=False)
    comprobar("replace_data_source: guardado y verificado", r.get("ok"), json.dumps(r)[:300])
    op = (r["documentos"][0].get("operaciones") or [{}])[-1].get("result") or {}
    comprobar("replace_data_source: la fuente nueva leida de vuelta",
              str(op.get("fuente_nueva", "")).lower().endswith("real_copia.tif"), op)
    r = s.edit_mxd_lote([f1], [{"tool": "edit_symbol", "capa": "ras"}], bak=False)
    clases = ((r["documentos"][0].get("operaciones") or [{}])[0].get("result") or {}).get("clases") or []
    comprobar("replace_data_source: cortes, etiquetas y colores conservados",
              [c.get("etiqueta") for c in clases] == ["a", "b", "c", "d"]
              and [c.get("color_relleno") for c in clases] == ["#FF0000", "#00FF00", "#0000FF", "#FFFF00"], clases)
    r = s.edit_mxd_lote([f1], [{"tool": "replace_data_source", "capa": "ras", "fuente": os.path.join(DIR, "no.tif")}],
                        bak=False)
    comprobar("replace_data_source: fuente inexistente, error sin guardar",
              not r["ok"] and r["documentos"][0].get("guardado") is False, json.dumps(r)[:200])

    sys.stdout.write("--- plantilla: crear planos en otra carpeta (2026-10-10) ---" + chr(10))
    hoja = os.path.join(DIR, "serie", "a", "hoja.mxd")
    a_mano = os.path.join(DIR, "serie", "a", "a_mano.mxd")
    r = s.edit_mxd_lote([hoja], plantilla=f1, operaciones=[{"tool": "rename_layer", "capa": "ras", "nombre_nuevo": "ras {N}"}],
                        variables_por_mxd={hoja: {"N": "1"}})
    comprobar("plantilla: creado y verificado", r.get("ok") and r["documentos"][0].get("creado"), json.dumps(r)[:300])
    shutil.copyfile(f1, a_mano)
    codigo_rotas = ("import arcpy, json, sys\n"
                    "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
                    "print(json.dumps([l.name for l in arcpy.mapping.ListLayers(m) if l.name.startswith('ras')"
                    " and l.isBroken]))\n")
    with open(os.path.join(DIR, "_rotas.py"), "w") as fh:
        fh.write(codigo_rotas)
    def rotas(m):
        out = subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_rotas.py"), m], capture_output=True,
                             timeout=180, **s.SIN_VENTANA)
        return json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    comprobar("plantilla: arcpy ve la capa sana desde la carpeta nueva", rotas(hoja) == [], rotas(hoja))
    comprobar("control: la copia a mano del mismo plano SI la rompe", rotas(a_mano) == ["ras"], rotas(a_mano))
    r = s.edit_mxd_lote([hoja], plantilla=f1)
    comprobar("plantilla: un destino que ya existe no se pisa", not r["ok"] and "ya existe" in str(r["documentos"][0].get("error")),
              json.dumps(r)[:200])
else:
    comprobar("hay raster de prueba %s" % RAS, False)

sys.stdout.write("--- huecos 11-15: simbolos, leyenda, .lyr, logos, AND, sin duplicar, comparar (2026-10-10) ---" + chr(10))
h = copia("plano_h")
LYR_H = os.path.join(DIR, "h.lyr")
r = s.edit_mxd_lote([h], [
    {"tool": "add_layer", "fuente": VEC, "nombre": "h"},
    {"tool": "add_layer", "fuente": VEC, "nombre": "h", "si_existe": "omitir"},
    {"tool": "set_definition_query", "capa": "h", "query": "FID >= 0"},
    {"tool": "set_definition_query", "capa": "h", "query": "FID < 100000", "anadir": True},
    {"tool": "set_single_symbology", "capa": "h", "trama": {"color": "#00AA00"}, "patron_linea": "punteada"},
    {"tool": "set_legend", "parche": [20, 10], "separaciones": {"entre_entradas": 5}},
    {"tool": "save_layer_as_lyr", "capa": "h", "ruta": LYR_H},
    {"tool": "copy_layout_elements", "origen": BASE, "nombres": ["FEADER"]}], bak=False)
comprobar("huecos 11-15: guardado y verificado al reabrir", r.get("ok"), json.dumps(r)[:300])
codigo_h = ("import arcpy, json, sys\n"
            "m = arcpy.mapping.MapDocument(sys.argv[1])\n"
            "hs = [l for l in arcpy.mapping.ListLayers(m) if l.name == 'h']\n"
            "lyr = arcpy.mapping.Layer(sys.argv[2])\n"
            "print(json.dumps({'h': len(hs), 'dq': hs[0].definitionQuery if hs else None,"
            " 'lyr_dq': lyr.definitionQuery,"
            " 'logos': len([e for e in arcpy.mapping.ListLayoutElements(m, 'PICTURE_ELEMENT') if e.name == 'FEADER'])}))\n")
with open(os.path.join(DIR, "_h.py"), "w") as fh:
    fh.write(codigo_h)
out = subprocess.run([s._python27_arcgis(), os.path.join(DIR, "_h.py"), h, LYR_H], capture_output=True, timeout=180,
                     **s.SIN_VENTANA)
try:
    leido = json.loads(out.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
except (ValueError, IndexError):
    leido = {"error": out.stderr.decode("mbcs", "replace")[-300:]}
comprobar("arcpy: una sola capa h, con la query unida por AND", leido.get("h") == 1
          and leido.get("dq") == "(FID >= 0) AND (FID < 100000)", leido)
comprobar("arcpy: el .lyr guardado trae la misma query", leido.get("lyr_dq") == "(FID >= 0) AND (FID < 100000)", leido)
comprobar("arcpy: un FEADER mas que la plantilla (el de su grupo y el copiado)", leido.get("logos") == 2, leido)
cmp = s.compare_mxd(h, BASE)
comprobar("compare_mxd: la capa de mas y la leyenda, nada en los textos", cmp.get("ok")
          and (cmp["diferencias"].get("capas") or {}).get("solo_en_plano") == ["h"]
          and "textos" not in cmp["diferencias"] and "leyenda_1" in (cmp["diferencias"].get("leyendas") or {}),
          json.dumps(cmp)[:300])
control = s.compare_mxd(BASE, BASE)
comprobar("compare_mxd (control): la plantilla contra si misma, iguales", control.get("iguales") is True,
          json.dumps(control)[:200])

sys.stdout.write("--- fuente y tamano juntos, valores unicos sin clases (cliente, 2026-10-10) ---" + chr(10))
# El IFontDisp redondeaba el tamano a pixeles al reasignar la fuente: 8,5 -> 8,25.
f8 = copia("plano_f8")
r = s.edit_mxd_lote([f8], [
    {"tool": "add_layer", "fuente": VEC, "nombre": "f8"},
    {"tool": "set_labels", "capa": "f8", "expresion": "[Municipio]", "fuente": "Arial", "tamano": 8.5},
    {"tool": "set_legend", "fuente": "Arial", "tamano": 8.5, "negrita_nombres": True},
    {"tool": "set_legend", "negrita_nombres": False}], bak=False, detalle=True)
comprobar("fuente + tamano: guardado y verificado", r.get("ok"), json.dumps(r)[:300])
ops = r["documentos"][0].get("operaciones") or []
res_op = [(o or {}).get("result") or {} for o in ops]
tams = [k.get("tamano") for k in (res_op[1].get("clases") if len(res_op) > 1 else None) or []]
comprobar("set_labels fuente + tamano 8,5: 8,5", bool(tams) and all(x == 8.5 for x in tams), tams)
leyenda = [((o.get("ahora") or {}).get("tamano_min"), (o.get("ahora") or {}).get("tamano_max")) for o in res_op[2:4]]
comprobar("set_legend fuente + 8,5 y luego solo negrita: 8,5 las dos veces", leyenda == [(8.5, 8.5)] * 2, leyenda)
inv = json.load(open(s.compare_mxd(f8, BASE, ambitos=["etiquetado"])["detalle_completo"], encoding="utf-8"))
capa_f8 = [c for c in inv["inventarios"]["mxd"]["capas"] if c.get("ruta") == "f8"]
tams = [k.get("tamano") for k in ((capa_f8[0].get("etiquetado") or {}).get("clases") if capa_f8 else None) or []]
comprobar("reabierto: las etiquetas siguen a 8,5", bool(tams) and all(x == 8.5 for x in tams), tams)
# El inventario (mismos handlers) marca los simbolos de varias capas; la capa f8, simple, no.
inv = json.load(open(s.compare_mxd(f8, BASE, ambitos=["simbologia"])["detalle_completo"], encoding="utf-8"))
por_ruta = {c.get("ruta"): ((c.get("simbologia") or {}).get("clases") or [{}])[0]
            for c in inv["inventarios"]["mxd"]["capas"]}
zepa = [v for k, v in por_ruta.items() if k and k.endswith("/ZEPA")]
comprobar("inventario: ZEPA con capas_simbolo y f8 sin el", len(zepa) == 1 and (zepa[0].get("capas_simbolo") or 0) >= 1
          and "capas_simbolo" not in (por_ruta.get("f8") or {"capas_simbolo": "falta f8"}), [zepa, por_ruta.get("f8")])
# audit_folder(detalle=True): la lista de capas va solo al fichero (106 KB con 6 planos reales).
carpeta_aud = os.path.join(DIR, "auditar")
shutil.rmtree(carpeta_aud, ignore_errors=True)
copia("a1", carpeta=carpeta_aud)
aud = s.audit_folder(carpeta_aud, detalle=True)
fila = (aud.get("documentos") or [{}])[0]
completo = json.load(open(aud["detalle_completo"], encoding="utf-8")) if aud.get("detalle_completo") else {}
capas_fichero = ((completo.get("documentos") or [{}])[0]).get("capas") or []
comprobar("audit_folder(detalle): sin capas en la respuesta, todas en el fichero", "capas" not in fila
          and fila.get("capas_en_detalle") == len(capas_fichero) == fila.get("num_capas") and len(capas_fichero) > 100,
          [fila.get("capas_en_detalle"), fila.get("num_capas"), len(capas_fichero)])
aud = s.audit_folder(carpeta_aud)
comprobar("control: sin detalle, la lista de capas sigue en la respuesta",
          len((aud["documentos"][0]).get("capas") or []) > 100, len((aud["documentos"][0]).get("capas") or []))
sc = copia("plano_sin_clases")
antes = huella(sc)
r = s.edit_mxd_lote([sc], [{"tool": "edit_symbol", "capa": ("Plano 02 - Emplazamiento General/Afección a figuras de"
                                                             " protección/Afección a figuras de protección"),
                            "patron_linea": "punteada"}])
comprobar("valores unicos sin clases: error, sin tocar el .mxd", not r["ok"] and huella(sc) == antes
          and "set_single_symbology" in str(r["documentos"][0].get("error")), json.dumps(r)[:300])

sys.stdout.write("--- todo o nada ---" + chr(10))
c = copia("plano_c")
antes = huella(c)
r = s.edit_mxd_lote([c], OPS + [{"tool": "set_legend_item", "capa": "capa_que_no_existe", "quitar": True}])
fila = r["documentos"][0]
comprobar("una operacion que falla: no se guarda y dice cual", not r["ok"] and fila.get("guardado") is False
          and "capa_que_no_existe" in str(fila.get("error")), json.dumps(fila)[:300])
comprobar("el .mxd queda identico y sin .bak", huella(c) == antes and not os.path.exists(c + ".bak"))

sys.stdout.write("--- negativas ---" + chr(10))
if os.path.isfile(OLE):
    o = copia("plano_ole", OLE)
    antes = huella(o)
    r = s.edit_mxd_lote([o], OPS)
    comprobar("marcos OLE: no se toca", r["documentos"][0].get("marcos_ole") and huella(o) == antes,
              json.dumps(r)[:200])
else:
    comprobar("hay un .mxd con OLE de prueba", False, OLE)
r = s.edit_mxd_lote([a], [{"tool": "export_jpg", "salida": "x.jpg"}])
comprobar("operacion fuera de la lista: error sin tocar nada", not r["ok"] and "export_jpg" in r.get("error", ""))

info = s._client.send("get_arcmap_info")
if info.get("ok"):
    d = copia("plano_abierto")
    abierto = s.open_mxd(d, "descartar")
    if abierto.get("ok"):
        antes = huella(d)
        r = s.edit_mxd_lote([d], OPS)
        comprobar("el documento abierto en ArcMap: no se toca", "documento abierto" in str(r["documentos"][0].get("error"))
                  and huella(d) == antes, json.dumps(r)[:200])
        # Medida que pide la ADR: la vía en vivo con las mismas operaciones, en el mismo plano.
        t0 = time.time()
        s.open_mxd(copia("plano_vivo"), "descartar")
        s.set_legend(tamano=8)
        s.set_legend_item(capa="guia_planos", quitar=True)
        s._client.send("set_layer_visibility", {"capa": "MUP", "visible": False})
        s.save_mxd(verificar=False)
        t_vivo = time.time() - t0
        sys.stdout.write("  medida: lote %.1f s por plano; en vivo (abrir + 3 operaciones + guardar) %.1f s%s"
                         % (t_lote, t_vivo, chr(10)))
    else:
        comprobar("open_mxd de la copia para la negativa", False, abierto)
else:
    sys.stdout.write("  [---] sin ArcMap con el puente: negativa del documento abierto y medida, saltadas" + chr(10))

sys.stdout.write("--- la verificacion caza una ruta relativa que se pierde al guardar ---" + chr(10))
largo = os.path.join(DIR, "rutas")


def capa_en(carpeta):
    os.makedirs(carpeta, exist_ok=True)
    base = os.path.splitext(VEC)[0]
    for f in glob.glob(base + ".*"):
        if not f.lower().endswith(".lock"):
            shutil.copy(f, os.path.join(carpeta, "r" + f[len(base):]))
    return os.path.join(carpeta, "r.shp")


# La regla medida (2.15.0): carpeta del .mxd + "\" + ruta RELATIVA del workspace >= 260. Con
# el .mxd en una carpeta profunda, la relativa se llena de "..\" y llega a 262 mientras las
# rutas absolutas siguen cortas (con una absoluta de más de 260 la capa ni se abre).
capas = os.path.join(largo, "c")
hondo = os.path.join(largo, "m")
while len(hondo) + 1 + len(os.path.relpath(capas, hondo)) < 262:
    hondo = os.path.join(hondo, "a")
for nombre, carpeta_mxd, se_pierde in (("plano_cerca", largo, False), ("plano_lejos", hondo, True)):
    m = copia(nombre, carpeta=carpeta_mxd)
    r = s.edit_mxd_lote([m], [{"tool": "add_layer", "fuente": capa_en(capas), "nombre": "capa_ruta"}])
    v = (r["documentos"][0].get("verificacion") or {})
    if se_pierde:
        comprobar("ruta de 262: guardado pero NO verificado, con la TOC como diferencia",
                  r["documentos"][0].get("guardado") and v.get("verificado") is False
                  and any(dif.get("lectura") == "toc" for dif in v.get("diferencias", [])), json.dumps(v)[:300])
    else:
        comprobar("ruta corta (control): verificado", r.get("ok") and v.get("verificado") is True, json.dumps(r)[:300])

sys.stdout.write(chr(10) + "RESULTADO: %d correctos, %d fallos%s" % (ok_n, len(fallos), chr(10)))
for nombre, det in fallos:
    sys.stdout.write("   FALLO %-40s %s%s" % (nombre, det, chr(10)))
sys.exit(1 if fallos else 0)
