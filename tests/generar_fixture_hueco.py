# -*- coding: utf-8 -*-
"""generar_fixture_hueco.py  --  Rehace tests/fixtures/relleno_hueco_color_negro.lyr.

Es el simbolo del issue #8: SimpleFill con Style = esriSFSNull y un Color RGB 0,0,0 VIVO
(NullColor = false). Ninguna tool deja un simbolo asi (`edit_symbol(sin_relleno=True)` pone
el color a nulo) y el Hollow de ESRI.style tampoco sirve: trae el color ya nulo.

Python 2.7 de ArcGIS con comtypes. Parte de un .lyr de poligonos existente y le cambia el
renderer; abrir un workspace por comtypes dio 0x80040228 incluso con la licencia tomada, y
leer la fuente de un .lyr no lo necesita. Lanzarlo desde `execute_arcpy`
(usar_documento=False) con:

    import sys
    sys.argv = ["x", u"<origen.lyr>", u"<destino.lyr>"]
    execfile(r"<repo>\\tests\\generar_fixture_hueco.py", {"__name__": "__main__"})

El .lyr guarda la RUTA de su fuente y el nombre de la capa, y el fixture se publica: el origen
tiene que ser una capa neutra, nunca la de un proyecto. El publicado sale de un shapefile de un
solo cuadrado (EPSG 25830) en C:\\temp\\arcmap-mcp-regresion\\hueco, guardado con
SaveToLayerFile como base.lyr; la regresion solo usa su simbolo, la fuente no hace falta.

Al final reabre el .lyr y comprueba la condicion leyendo ArcObjects (no con edit_symbol,
que es lo que se prueba). Imprime CONDICION_OK o CONDICION_FALLA.
"""
import os
import sys

import comtypes.client as cc

COM = r"C:\Program Files (x86)\ArcGIS\Desktop10.5\com"


def mod(n):
    return cc.GetModule(os.path.join(COM, n + ".olb"))


sysm = mod("esriSystem")
carto = mod("esriCarto")
disp = mod("esriDisplay")

ao = cc.CreateObject(sysm.AoInitialize, interface=sysm.IAoInitialize)
print("licencia", ao.Initialize(60))

origen, salida = [a.decode("mbcs") if isinstance(a, str) else a for a in sys.argv[1:3]]

lf = cc.CreateObject(carto.LayerFile, interface=carto.ILayerFile)
lf.Open(origen)
capa = lf.Layer
lf.Close()

col = cc.CreateObject(disp.RgbColor, interface=disp.IRgbColor)
col.Red = 0
col.Green = 0
col.Blue = 0
col.QueryInterface(disp.IColor).NullColor = False
sym = cc.CreateObject(disp.SimpleFillSymbol, interface=disp.ISimpleFillSymbol)
sym.Style = 1  # esriSFSNull
sym.Color = col.QueryInterface(disp.IColor)

ren = cc.CreateObject(carto.SimpleRenderer, interface=carto.ISimpleRenderer)
ren.Symbol = sym.QueryInterface(disp.ISymbol)
capa.QueryInterface(carto.IGeoFeatureLayer).Renderer = ren.QueryInterface(carto.IFeatureRenderer)
capa.Name = u"relleno_hueco_color_negro"

if os.path.exists(salida):
    os.remove(salida)
lf = cc.CreateObject(carto.LayerFile, interface=carto.ILayerFile)
lf.New(salida)
lf.ReplaceContents(capa)
lf.Save()
lf.Close()

lf2 = cc.CreateObject(carto.LayerFile, interface=carto.ILayerFile)
lf2.Open(salida)
r2 = lf2.Layer.QueryInterface(carto.IGeoFeatureLayer).Renderer.QueryInterface(carto.ISimpleRenderer)
s2 = r2.Symbol.QueryInterface(disp.ISimpleFillSymbol)
c2 = s2.Color
rgb = c2.QueryInterface(disp.IRgbColor)
print("Style", s2.Style, "NullColor", c2.NullColor, "RGB", rgb.Red, rgb.Green, rgb.Blue)
lf2.Close()
ok = s2.Style == 1 and not c2.NullColor and (rgb.Red, rgb.Green, rgb.Blue) == (0, 0, 0)
print("CONDICION_OK" if ok else "CONDICION_FALLA")
