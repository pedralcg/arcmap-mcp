# -*- coding: utf-8 -*-
"""Casos del runner arcpy ejecutados con el Python 2.7 de ArcGIS, SIN arcpy.

Lo lanza `test_runner_py27.py` como subproceso. No se llama `test_*` a proposito:
es codigo Python 2.7 y el descubridor de tests (que corre en Python 3) no debe
intentar importarlo.

POR QUE UN STUB DE ARCPY Y NO EL DE VERDAD: `import arcpy` cuesta ~6 s y TOMA UNA
LICENCIA de Desktop. Un test que pide licencia no se puede correr en bucle, y si
ArcMap esta abierto encima se bloquea. Lo que se prueba aqui es logica pura del
runner -codificacion, control de flujo, nombrado de salidas-, que no necesita
geoprocesar nada: con un arcpy de mentira en sys.modules se ejerce igual y en
milisegundos.

Salida: UNA linea JSON ASCII por stdout con el resultado de cada caso.
"""
import json
import os
import shutil
import sys
import tempfile
import traceback
import types

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNNER = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Python", "runner.py")

RESULTADOS = []


def caso(funcion):
    """Ejecuta un caso y anota si paso. Ninguno puede tumbar a los demas."""
    nombre = funcion.__name__
    try:
        funcion()
    except Exception:
        RESULTADOS.append({"nombre": nombre, "ok": False,
                           "error": traceback.format_exc()})
    else:
        RESULTADOS.append({"nombre": nombre, "ok": True, "error": None})
    return funcion


# --------------------------------------------------------------------------- #
# arcpy de mentira.
# --------------------------------------------------------------------------- #

EXISTENTES = set()      # rutas que `arcpy.Exists` dara por existentes
LLAMADAS = []           # geoprocesos invocados, para comprobar que NO se gastan


class _Env(object):
    def __init__(self):
        self.overwriteOutput = None
        self.scratchFolder = tempfile.gettempdir()


class _RasterFalso(object):
    def __init__(self, ruta=None):
        self.ruta = ruta
        self.guardado_en = None

    def save(self, ruta):
        self.guardado_en = ruta
        LLAMADAS.append(("save", ruta))


def _montar_arcpy():
    arcpy = types.ModuleType("arcpy")
    arcpy.env = _Env()
    arcpy.Exists = lambda ruta: ruta in EXISTENTES
    arcpy.GetMessages = lambda: u"mensajes de mentira"
    arcpy.CheckExtension = lambda ext: "Available"
    arcpy.CheckOutExtension = lambda ext: None
    arcpy.CheckInExtension = lambda ext: None

    mapping = types.ModuleType("arcpy.mapping")
    mapping.MapDocument = lambda ruta: (_ for _ in ()).throw(
        AssertionError("MapDocument sin configurar en este caso"))
    arcpy.mapping = mapping

    da = types.ModuleType("arcpy.da")
    arcpy.da = da

    ddd = types.ModuleType("arcpy.ddd")

    def _contour(mdt, salida, intervalo, base):
        LLAMADAS.append(("Contour", salida))
    ddd.Contour = _contour

    def _interpolate(superficie, lineas, salida):
        LLAMADAS.append(("InterpolateShape", salida))
    ddd.InterpolateShape = _interpolate
    arcpy.ddd = ddd

    conversion = types.ModuleType("arcpy.conversion")

    def _export_cad(entradas, formato, salida):
        LLAMADAS.append(("ExportCAD", salida))
    conversion.ExportCAD = _export_cad
    arcpy.conversion = conversion

    sa = types.ModuleType("arcpy.sa")
    sa.Raster = _RasterFalso
    sa.Float = lambda r: r

    def _cost_distance(origen, coste, out_backlink_raster=None):
        LLAMADAS.append(("CostDistance", out_backlink_raster))
        return _RasterFalso("cdist")
    sa.CostDistance = _cost_distance

    def _cost_path(destino, cdist, backlink, modo):
        LLAMADAS.append(("CostPath", modo))
        return _RasterFalso("lcp")
    sa.CostPath = _cost_path
    arcpy.sa = sa

    sys.modules["arcpy"] = arcpy
    sys.modules["arcpy.mapping"] = mapping
    sys.modules["arcpy.da"] = da
    sys.modules["arcpy.ddd"] = ddd
    sys.modules["arcpy.conversion"] = conversion
    sys.modules["arcpy.sa"] = sa
    return arcpy


ARCPY = _montar_arcpy()

# El runner se carga desde una COPIA: `imp.load_source` deja un .pyc al lado del
# fuente, y al lado del fuente esta el runner que se embebe en la DLL del add-in.
import imp  # noqa: E402  (despues del stub: el import de runner necesita arcpy)

_TMP = tempfile.mkdtemp(prefix="runner_py27_")
_COPIA = os.path.join(_TMP, "runner_bajo_prueba.py")
shutil.copyfile(RUNNER, _COPIA)
runner = imp.load_source("runner_bajo_prueba", _COPIA)


# --------------------------------------------------------------------------- #
# 2a — stdout que mezcla str y unicode.
# --------------------------------------------------------------------------- #

@caso
def defecto_real_del_stringio():
    """Deja constancia de que el caso 2a no era una hipotesis."""
    import StringIO
    buff = StringIO.StringIO()
    buff.write("N\xc3\xbamero")     # str, bytes utf-8 (lo que deja `print "..."`)
    buff.write(u"se\xf1al")         # unicode (lo que deja `print u"..."`)
    try:
        buff.getvalue()
    except UnicodeDecodeError:
        return
    raise AssertionError("StringIO deberia haber fallado al mezclar str y unicode")


@caso
def stdout_mezclado_no_pierde_el_result():
    codigo = u'print "N\xfamero"\nprint u"se\xf1al"\nRESULT = 42'
    salida = runner.op_execute_code({"params": {"code": codigo}})
    assert salida["result"] == 42, salida
    assert salida["stdout"] == u"N\xfamero\nse\xf1al\n", repr(salida["stdout"])


@caso
def stdout_en_cp1252_no_revienta():
    """os.listdir y compania devuelven `str` en la codepage ANSI, no en utf-8."""
    codigo = u'import sys\nsys.stdout.write("caf\\xe9")\nRESULT = 1'
    salida = runner.op_execute_code({"params": {"code": codigo}})
    assert salida["stdout"] == u"caf\xe9", repr(salida["stdout"])


@caso
def print_con_coma_sigue_separando():
    """El `print` de Py2 usa el atributo softspace del fichero de salida."""
    salida = runner.op_execute_code({"params": {"code": u'print "a", "b"\nRESULT = 1'}})
    assert salida["stdout"] == u"a b\n", repr(salida["stdout"])


# --------------------------------------------------------------------------- #
# 2b — sys.exit() / exit() / KeyboardInterrupt.
# --------------------------------------------------------------------------- #

@caso
def sys_exit_con_result_devuelve_el_result():
    codigo = u'import sys\nRESULT = "hecho"\nsys.exit()'
    salida = runner.op_execute_code({"params": {"code": codigo}})
    assert salida["result"] == u"hecho", salida
    assert "aviso_salida" in salida, salida


@caso
def exit_builtin_con_result_devuelve_el_result():
    salida = runner.op_execute_code({"params": {"code": u'RESULT = 7\nexit()'}})
    assert salida["result"] == 7, salida
    assert "aviso_salida" in salida, salida


@caso
def sys_exit_sin_result_da_error_accionable():
    codigo = u'import sys\nprint "voy"\nsys.exit(2)'
    try:
        runner.op_execute_code({"params": {"code": codigo}})
    except ValueError as ex:
        texto = unicode(ex)
        assert u"sys.exit(2)" in texto, texto
        assert u"RESULT" in texto, texto
        assert u"voy" in texto, texto   # el stdout capturado no se tira
        return
    raise AssertionError("un sys.exit(2) sin RESULT tiene que dar error")


@caso
def sys_exit_con_mensaje_lo_repite():
    codigo = u'import sys\nsys.exit("falta la capa")'
    try:
        runner.op_execute_code({"params": {"code": codigo}})
    except ValueError as ex:
        assert u"falta la capa" in unicode(ex), unicode(ex)
        return
    raise AssertionError("se esperaba ValueError")


@caso
def keyboard_interrupt_es_error():
    try:
        runner.op_execute_code({"params": {"code": u'raise KeyboardInterrupt'}})
    except ValueError as ex:
        assert u"KeyboardInterrupt" in unicode(ex), unicode(ex)
        return
    raise AssertionError("se esperaba ValueError")


@caso
def sys_exit_escribe_el_out_json():
    """Lo que de verdad se rompia: el runner moria sin escribir salida y el add-in
    solo podia decir 'salida vacia' de un codigo que habia corrido."""
    carpeta = tempfile.mkdtemp(prefix="runner_exit_")
    job_path = os.path.join(carpeta, "job.json")
    out_path = os.path.join(carpeta, "out.json")
    import io
    with io.open(job_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"op": "execute_code",
                            "params": {"code": "import sys\nsys.exit(3)"}},
                           ensure_ascii=False).decode("utf-8"))
    argv = sys.argv
    sys.argv = ["runner.py", job_path, out_path]
    try:
        codigo = runner.main()
    finally:
        sys.argv = argv
    assert codigo == 0, codigo
    assert os.path.isfile(out_path), "el runner no escribio out.json"
    with io.open(out_path, "r", encoding="utf-8") as f:
        datos = json.loads(f.read())
    assert datos["ok"] is False, datos
    assert u"sys.exit(3)" in datos["error"], datos
    shutil.rmtree(carpeta, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 2c — saneo de la respuesta antes de serializar.
# --------------------------------------------------------------------------- #

@caso
def defecto_real_del_serializar_viejo():
    """Igual que con StringIO: que conste que el caso 2c ocurria de verdad.

    Aqui el que revienta NO es json.dumps: con ensure_ascii=False copia los bytes
    del `str` tal cual y devuelve un `str`. Revienta el `.decode("utf-8")` de la
    linea siguiente -el que _serializar necesita para poder escribir en un fichero
    abierto con io.open-, y se lleva por delante el job entero.
    """
    texto = json.dumps({"ruta": "Cartograf\xeda"}, ensure_ascii=False)
    assert isinstance(texto, str), type(texto)
    try:
        texto.decode("utf-8")
    except UnicodeDecodeError:
        return
    raise AssertionError("el _serializar viejo deberia fallar al decodificar")


@caso
def serializar_sanea_str_en_cp1252():
    respuesta = {"ok": True, "result": {"rutas": ["Cartograf\xeda", u"ya unicode"]}}
    datos = json.loads(runner._serializar(respuesta))
    assert datos["result"]["rutas"][0] == u"Cartograf\xeda", datos


@caso
def serializar_respeta_los_tipos_no_texto():
    """Coaccionar a ciegas convertiria un 42 en '42' y mentiria sobre el resultado."""
    respuesta = {"ok": True, "result": {"n": 42, "x": 1.5, "si": True,
                                        "nada": None, "lista": (1, "a")}}
    datos = json.loads(runner._serializar(respuesta))
    res = datos["result"]
    assert res["n"] == 42 and isinstance(res["n"], int), res
    assert res["x"] == 1.5, res
    assert res["si"] is True, res
    assert res["nada"] is None, res
    assert res["lista"] == [1, u"a"], res


@caso
def serializar_con_claves_que_no_son_texto():
    respuesta = {"ok": True, "result": {1: "a", (1, 2): "b", None: "c"}}
    datos = json.loads(runner._serializar(respuesta))
    claves = sorted(datos["result"].keys())
    assert claves == [u"(1, 2)", u"1", u"None"], claves


@caso
def result_con_str_cp1252_llega_hasta_el_fichero():
    """Recorrido completo: RESULT -> _escribir_salida -> JSON legible."""
    carpeta = tempfile.mkdtemp(prefix="runner_sanea_")
    out_path = os.path.join(carpeta, "out.json")
    runner._escribir_salida(out_path, {"ok": True,
                                       "result": {"fichero": "plano_ca\xf1ada.mxd"}})
    import io
    with io.open(out_path, "r", encoding="utf-8") as f:
        datos = json.loads(f.read())
    assert datos["result"]["fichero"] == u"plano_ca\xf1ada.mxd", datos
    shutil.rmtree(carpeta, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 3 — export_ddp: valores que no casan con ninguna pagina.
# --------------------------------------------------------------------------- #

class _DdpFalso(object):
    def __init__(self, paginas):
        self.paginas = paginas          # {valor: id}
        self.pageCount = len(paginas)
        self.exportado = None

    def getPageIDFromName(self, nombre):
        return self.paginas.get(nombre, 0)

    def exportToPDF(self, salida, range_type, page_range, multiple, dpi):
        self.exportado = (salida, range_type, page_range, multiple, dpi)


class _MxdFalso(object):
    def __init__(self, ddp):
        self.dataDrivenPages = ddp


def _con_atlas(paginas):
    ddp = _DdpFalso(paginas)
    ARCPY.mapping.MapDocument = lambda ruta: _MxdFalso(ddp)
    return ddp


@caso
def export_ddp_delata_los_valores_que_no_existen():
    _con_atlas({u"EXP-01": 1, u"EXP-02": 2})
    salida = runner.op_export_ddp({
        "mxd": "snapshot.mxd",
        "params": {"salida": "atlas.pdf", "valores": [u"EXP-01", u"EXP-99", u"EXP-02"]}})
    assert salida["valores_no_encontrados"] == [u"EXP-99"], salida
    # Aqui la clave SI es "aviso": el handler ExportDdp del add-in reexpide el
    # resultado del runner tal cual. El que la pisa es ExecuteArcpy, y por eso
    # op_execute_code usa "aviso_salida".
    assert "aviso" in salida, salida
    assert u"EXP-99" in salida["aviso"], salida
    assert salida["page_range"] == u"1,2", salida
    assert salida["num"] == 2, salida


@caso
def export_ddp_sin_fallos_no_inventa_aviso():
    _con_atlas({u"EXP-01": 1, u"EXP-02": 2})
    salida = runner.op_export_ddp({
        "mxd": "snapshot.mxd",
        "params": {"salida": "atlas.pdf", "valores": [u"EXP-01", u"EXP-02"]}})
    assert salida["valores_no_encontrados"] == [], salida
    assert "aviso" not in salida, salida


@caso
def export_ddp_sigue_fallando_si_no_casa_ninguno():
    _con_atlas({u"EXP-01": 1})
    try:
        runner.op_export_ddp({"mxd": "snapshot.mxd",
                              "params": {"salida": "atlas.pdf",
                                         "valores": [u"NADA", u"TAMPOCO"]}})
    except ValueError as ex:
        assert u"Ning" in unicode(ex), unicode(ex)
        return
    raise AssertionError("sin ninguna pagina valida tiene que fallar")


@caso
def export_ddp_modo_all_no_toca_los_valores():
    ddp = _con_atlas({u"EXP-01": 1, u"EXP-02": 2})
    salida = runner.op_export_ddp({"mxd": "snapshot.mxd",
                                   "params": {"salida": "atlas.pdf"}})
    assert salida["modo_efectivo"] == "ALL", salida
    assert salida["valores_no_encontrados"] == [], salida
    assert ddp.exportado[1] == "ALL", ddp.exportado


# --------------------------------------------------------------------------- #
# 4 — sobrescribir y backlink unico.
# --------------------------------------------------------------------------- #

@caso
def salida_existente_aborta_antes_del_geoproceso():
    EXISTENTES.clear()
    del LLAMADAS[:]
    EXISTENTES.add(u"C:\\salidas\\curvas.shp")
    try:
        runner.op_contours({"params": {"mdt": "mdt.tif", "salida": u"C:\\salidas\\curvas.shp",
                                       "intervalo": 10}})
    except ValueError as ex:
        texto = unicode(ex)
        assert u"ya existe" in texto, texto
        assert u"sobrescribir=true" in texto, texto
        assert LLAMADAS == [], "se gasto el geoproceso pese a abortar: %r" % (LLAMADAS,)
        return
    finally:
        EXISTENTES.clear()
    raise AssertionError("con la salida ya escrita tenia que abortar")


@caso
def sobrescribir_activa_el_overwrite_y_ejecuta():
    EXISTENTES.clear()
    del LLAMADAS[:]
    EXISTENTES.add(u"C:\\salidas\\curvas.shp")
    ARCPY.env.overwriteOutput = None
    try:
        salida = runner.op_contours({"params": {"mdt": "mdt.tif",
                                                "salida": u"C:\\salidas\\curvas.shp",
                                                "intervalo": 10,
                                                "sobrescribir": True}})
    finally:
        EXISTENTES.clear()
    assert ARCPY.env.overwriteOutput is True, ARCPY.env.overwriteOutput
    assert ("Contour", u"C:\\salidas\\curvas.shp") in LLAMADAS, LLAMADAS
    assert salida["salida"] == u"C:\\salidas\\curvas.shp", salida


@caso
def sin_sobrescribir_el_overwrite_queda_apagado():
    """Es global al proceso: dejarlo encendido convierte una ruta mal tecleada en
    un borrado silencioso."""
    EXISTENTES.clear()
    del LLAMADAS[:]
    ARCPY.env.overwriteOutput = True
    runner.op_contours({"params": {"mdt": "mdt.tif", "salida": u"C:\\salidas\\nuevas.shp",
                                   "intervalo": 10}})
    assert ARCPY.env.overwriteOutput is False, ARCPY.env.overwriteOutput


@caso
def contours_tambien_mira_el_dxf():
    EXISTENTES.clear()
    del LLAMADAS[:]
    EXISTENTES.add(u"C:\\salidas\\curvas.dxf")
    try:
        runner.op_contours({"params": {"mdt": "mdt.tif", "salida": u"C:\\salidas\\c.shp",
                                       "intervalo": 10, "dxf": u"C:\\salidas\\curvas.dxf"}})
    except ValueError as ex:
        assert u"curvas.dxf" in unicode(ex), unicode(ex)
        assert LLAMADAS == [], LLAMADAS
        return
    finally:
        EXISTENTES.clear()
    raise AssertionError("un dxf ya escrito tambien es una salida ocupada")


@caso
def hydrology_cuenca_cuenta_fdir_y_facc():
    """Llevan nombre fijo dentro de salida_dir: chocan al repetir en la misma carpeta."""
    EXISTENTES.clear()
    carpeta = tempfile.mkdtemp(prefix="runner_cuenca_")
    EXISTENTES.add(os.path.join(carpeta, "fdir"))
    try:
        runner.op_hydrology({"params": {"operacion": "cuenca",
                                        "parametros": {"mdt": "mdt.tif",
                                                       "salida_dir": carpeta,
                                                       "salida": os.path.join(carpeta, "c")}}})
    except ValueError as ex:
        assert u"fdir" in unicode(ex), unicode(ex)
        return
    finally:
        EXISTENTES.clear()
        shutil.rmtree(carpeta, ignore_errors=True)
    raise AssertionError("un fdir ya escrito tenia que abortar la cuenca")


@caso
def backlink_es_unico_en_cada_llamada():
    """Con el nombre fijo `lcp_backlink`, la segunda ruta calculada en la misma
    carpeta moria sin haber tocado siquiera la salida pedida."""
    a = runner._ruta_backlink(u"C:\\trabajo")
    b = runner._ruta_backlink(u"C:\\trabajo")
    assert a != b, (a, b)


@caso
def backlink_en_carpeta_es_tif():
    """Sin extension seria un GRID de Esri, que corta el nombre a 13 caracteres."""
    ruta = runner._ruta_backlink(u"C:\\trabajo")
    assert ruta.lower().endswith(u".tif"), ruta


@caso
def backlink_en_gdb_no_lleva_extension():
    ruta = runner._ruta_backlink(u"C:\\trabajo\\datos.gdb")
    assert not ruta.lower().endswith(u".tif"), ruta
    assert u"lcp_backlink_" in ruta, ruta


@caso
def least_cost_path_usa_el_backlink_unico():
    EXISTENTES.clear()
    del LLAMADAS[:]
    salida = runner.op_least_cost_path({"params": {"coste": "fric.tif", "origen": "o.shp",
                                                   "destino": "d.shp",
                                                   "salida": u"C:\\trabajo\\lcp.tif"}})
    usados = [ruta for nombre, ruta in LLAMADAS if nombre == "CostDistance"]
    assert usados and usados[0] == salida["backlink"], (usados, salida)
    assert not salida["backlink"].endswith(u"lcp_backlink"), salida


# --------------------------------------------------------------------------- #

def main():
    fallos = [r for r in RESULTADOS if not r["ok"]]
    sys.stdout.write(json.dumps({"ok": not fallos, "casos": RESULTADOS},
                                ensure_ascii=True))
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
