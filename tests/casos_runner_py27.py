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


TIPOS = {}             # ruta -> dataType que devolvera `arcpy.Describe`
FALLA_GP = {}          # entrada -> mensaje con el que falla el GetCount de mentira


class _Desc(object):
    def __init__(self, tipo):
        self.dataType = tipo


class _ResultFalso(object):
    def __init__(self, salidas):
        self.salidas = salidas
        self.outputCount = len(salidas)

    def getOutput(self, i):
        return self.salidas[i]

    def getMessages(self):
        return u"Completado"


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
    arcpy.GetMessages = lambda severidad=None: FALLA_GP.get("_ultimo", u"mensajes de mentira")
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

    class ExecuteError(Exception):
        pass
    arcpy.ExecuteError = ExecuteError
    arcpy.Usage = lambda nombre: u"Usage: %s in_rows" % nombre
    arcpy.Describe = lambda ruta: _Desc(TIPOS.get(ruta, u"File"))

    management = types.ModuleType("arcpy.management")

    def _get_count(in_rows):
        # Firma fija de un parametro: con mas, Python da TypeError, como arcpy.
        LLAMADAS.append(("GetCount", in_rows))
        if in_rows in FALLA_GP:
            FALLA_GP["_ultimo"] = FALLA_GP[in_rows]
            raise ExecuteError(FALLA_GP[in_rows])
        return _ResultFalso([u"24"])
    management.GetCount = _get_count

    def _copy(entrada, salida):
        LLAMADAS.append(("CopyFeatures", salida))
        return _ResultFalso([salida])
    management.CopyFeatures = _copy

    # arcpy.gp: lo que usa op_geoprocessing (la firma del geoprocesador). Un atributo
    # que no existe da AttributeError, como el de verdad.
    class _Gp(object):
        pass
    gp = _Gp()
    gp.GetCount_management = _get_count
    gp.CopyFeatures_management = _copy
    arcpy.gp = gp
    arcpy.management = management

    sys.modules["arcpy"] = arcpy
    sys.modules["arcpy.management"] = management
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
# 2026-09-23 — execute_code: cabecera coding, result, stdout al fallar,
# documento sin data frame y aviso de capas rotas que no viene a cuento.
# --------------------------------------------------------------------------- #

def _job_por_main(job):
    """Pasa un job por runner.main() de punta a punta y devuelve el out.json."""
    import io
    carpeta = tempfile.mkdtemp(prefix="runner_main_")
    job_path = os.path.join(carpeta, "job.json")
    out_path = os.path.join(carpeta, "out.json")
    with io.open(job_path, "w", encoding="utf-8") as f:
        texto = json.dumps(job, ensure_ascii=False)
        f.write(texto.decode("utf-8") if isinstance(texto, str) else texto)
    argv = sys.argv
    sys.argv = ["runner.py", job_path, out_path]
    try:
        runner.main()
    finally:
        sys.argv = argv
    with io.open(out_path, "r", encoding="utf-8") as f:
        datos = json.loads(f.read())
    shutil.rmtree(carpeta, ignore_errors=True)
    return datos


def _reiniciar_estado():
    runner._STDOUT_AL_FALLAR = None
    runner._ROTAS_EN_COPIA = None
    runner._COPIA_USADA = True
    runner._MXD_ORIGINAL = None


class _Capa(object):
    def __init__(self, nombre):
        self.name = nombre


class _DocSinDataFrame(object):
    """Lo que da arcpy con la copia de un documento "Sin título"."""
    @property
    def activeDataFrame(self):
        raise NameError("The attribute 'activeDataFrame' is not supported on this "
                        "instance of MapDocument.")


class _DocConDataFrame(object):
    activeDataFrame = object()


def _con_documento(doc, rotas):
    """Stub de MapDocument/ListBrokenDataSources y un .mxd de mentira en disco."""
    fd, ruta = tempfile.mkstemp(suffix=".mxd", dir=_TMP)
    os.close(fd)
    # Otra ruta, otro documento: como arcpy de verdad.
    ARCPY.mapping.MapDocument = lambda r: doc if r == ruta else _DocConDataFrame()
    ARCPY.mapping.ListBrokenDataSources = lambda d: [_Capa(n) for n in rotas]
    return ruta


@caso
def coding_cabecera_se_tolera():
    _reiniciar_estado()
    codigo = u'# -*- coding: utf-8 -*-\nRESULT = u"monta\xf1a"'
    salida = runner.op_execute_code({"params": {"code": codigo}})
    assert salida["result"] == u"monta\xf1a", salida


@caso
def coding_en_segunda_linea_y_numeros_de_linea_intactos():
    _reiniciar_estado()
    codigo = u'#!python\n# vim: set fileencoding=latin-1 :\nx = 1\nraise ValueError("linea4")'
    try:
        runner.op_execute_code({"params": {"code": codigo}})
    except ValueError:
        tb = traceback.format_exc()
        assert u"line 4" in tb, tb
        return
    raise AssertionError("se esperaba ValueError")


@caso
def coding_en_tercera_linea_no_se_toca():
    """PEP 263 solo mira las dos primeras: más abajo es un comentario normal."""
    assert runner._quitar_cabecera_coding(u"a\nb\n# coding: utf-8") == u"a\nb\n# coding: utf-8"


@caso
def result_minuscula_se_devuelve_con_aviso():
    _reiniciar_estado()
    salida = runner.op_execute_code({"params": {"code": u'result = [1, 2]'}})
    assert salida["result"] == [1, 2], salida
    assert u"RESULT" in salida["aviso_result"], salida


@caso
def result_mayuscula_manda_sobre_minuscula():
    _reiniciar_estado()
    salida = runner.op_execute_code({"params": {"code": u'result = 1\nRESULT = 2'}})
    assert salida["result"] == 2 and "aviso_result" not in salida, salida


@caso
def stdout_se_conserva_cuando_el_codigo_falla():
    """El bucle que falla en el 5º documento debe decir que los 4 primeros salieron."""
    _reiniciar_estado()
    codigo = u'for i in range(4):\n    print u"OK plano %d" % i\nraise RuntimeError("plano 5")'
    datos = _job_por_main({"op": "execute_code", "params": {"code": codigo}})
    assert datos["ok"] is False, datos
    assert u"OK plano 3" in datos["stdout"], datos


@caso
def unicode_error_trae_pista():
    _reiniciar_estado()
    codigo = u'print "voy"\nstr(ValueError(u"acci\xf3n"))'
    datos = _job_por_main({"op": "execute_code", "params": {"code": codigo}})
    assert datos["ok"] is False, datos
    assert u"unicode(ex)" in datos["pista"], datos
    assert u"voy" in datos["stdout"], datos


@caso
def documento_sin_data_frame_no_bloquea():
    _reiniciar_estado()
    ruta = _con_documento(_DocSinDataFrame(), [])
    salida = runner.op_execute_code({"params": {"code": u'RESULT = df is None'},
                                     "mxd": ruta})
    assert salida["result"] is True, salida


@caso
def rotas_se_omiten_si_el_codigo_abre_otros_mxd():
    _reiniciar_estado()
    ruta = _con_documento(_DocConDataFrame(), [u"ENP", u"ZEPA"])
    codigo = u'mxd = MAP.MapDocument(r"C:\\otro\\plano.mxd")\nRESULT = "hecho"'
    datos = _job_por_main({"op": "execute_code", "params": {"code": codigo},
                           "mxd": ruta, "mxd_original": u"C:\\p\\PlanoAbierto.mxd"})
    assert datos["ok"] is True, datos
    assert "aviso_capas_rotas" not in datos["result"], datos
    assert "capas_rotas_en_copia" not in datos["result"], datos


@caso
def rotas_nombran_el_documento_si_se_usa_la_copia():
    _reiniciar_estado()
    rotas = [u"capa%d" % i for i in range(8)]
    ruta = _con_documento(_DocConDataFrame(), rotas)
    datos = _job_por_main({"op": "execute_code", "params": {"code": u'RESULT = mxd is not None'},
                           "mxd": ruta, "mxd_original": u"C:\\p\\PlanoAbierto.mxd"})
    aviso = datos["result"]["aviso_capas_rotas"]
    assert u"PlanoAbierto.mxd" in aviso, aviso
    assert u"y 3 más" in aviso and u"capa7" not in aviso, aviso
    assert datos["result"]["capas_rotas_en_copia"] == 8, datos


# --------------------------------------------------------------------------- #
# Auditor: lo que sale en el plano frente a lo que solo esta en el documento.
# En el bloque 02 de ID2018 habia 2.335 capas rotas y solo 87 se dibujaban; en el
# 03, 77 marcos fuera de la hoja arrastraban filtros de otros montes.
# --------------------------------------------------------------------------- #

AUDITOR = os.path.join(RAIZ, "src", "auditor_mxd.py")
_COPIA_AUDITOR = os.path.join(_TMP, "auditor_bajo_prueba.py")
shutil.copyfile(AUDITOR, _COPIA_AUDITOR)
auditor = imp.load_source("auditor_bajo_prueba", _COPIA_AUDITOR)


class _CapaTOC(object):
    """Capa de mentira con lo que lee el auditor. `visible=None` = no se deja leer."""
    def __init__(self, largo, visible=True, grupo=False, rota=False, query=None):
        self.longName = largo
        self.name = largo.split(u"\\")[-1]
        self._visible = visible
        self.isGroupLayer = grupo
        self.isBroken = rota
        self.definitionQuery = query or u""
        self.dataSource = u"C:\\datos\\%s.shp" % self.name

    @property
    def visible(self):
        if self._visible is None:
            raise RuntimeError("visible no disponible")
        return self._visible

    def supports(self, que):
        return not self.isGroupLayer


class _Pagina(object):
    width, height = 42.0, 29.7


class _Marco(object):
    def __init__(self, nombre, x, y, ancho=20.0, alto=20.0):
        self.name = nombre
        self.elementPositionX, self.elementPositionY = x, y
        self.elementWidth, self.elementHeight = ancho, alto


class _MxdAuditado(object):
    pageSize = _Pagina()


def _auditar(marcos_y_capas):
    """Monta un documento de mentira. `ListLayers(mxd, "", df)` casa el marco por
    nombre y devuelve un marco NUEVO en cada ListDataFrames, como arcpy."""
    ARCPY.mapping.MapDocument = lambda ruta: _MxdAuditado()
    ARCPY.mapping.ListDataFrames = lambda mxd: [
        _Marco(*m) for m, _capas in marcos_y_capas]
    por_nombre = dict((m[0], capas) for m, capas in marcos_y_capas)
    ARCPY.mapping.ListLayers = lambda mxd, comodin="", df=None: list(por_nombre[df.name])
    return auditor.auditar(u"C:\\x\\plano.mxd")


def _por_largo(salida):
    return dict((c["nombre_largo"], c) for c in salida["capas"])


@caso
def auditor_grupo_apagado_apaga_a_sus_nietos():
    salida = _auditar([(("PLANO", 1, 1), [
        _CapaTOC(u"Base", visible=False, grupo=True),
        _CapaTOC(u"Base\\Sub", grupo=True),
        _CapaTOC(u"Base\\Sub\\Rios", rota=True, query=u"ID = 1"),
        _CapaTOC(u"Limite", rota=True),
    ])])
    capas = _por_largo(salida)
    rios = capas[u"Base\\Sub\\Rios"]
    assert rios["visible"] is True and rios["visible_efectivo"] is False, rios
    assert rios["en_plano"] is False and rios["data_frame"] == u"PLANO", rios
    assert capas[u"Limite"]["en_plano"] is True, capas
    assert salida["num_rotas"] == 2 and salida["num_rotas_en_plano"] == 1, salida
    assert salida["num_con_query"] == 1 and salida["num_con_query_en_plano"] == 0, salida


@caso
def auditor_grupo_cerrado_no_arrastra_al_hermano_siguiente():
    """Tras salir de un grupo apagado, la capa de al lado vuelve a verse, aunque
    su nombre empiece igual (`Base2` no es hija de `Base`)."""
    salida = _auditar([(("PLANO", 1, 1), [
        _CapaTOC(u"Base", visible=False, grupo=True),
        _CapaTOC(u"Base\\Rios"),
        _CapaTOC(u"Base2"),
    ])])
    assert _por_largo(salida)[u"Base2"]["visible_efectivo"] is True, salida


@caso
def auditor_grupos_homonimos_en_ramas_distintas():
    """Por orden del TOC, no por nombre: el `Hidro` apagado de A no apaga el de B."""
    salida = _auditar([(("PLANO", 1, 1), [
        _CapaTOC(u"A", grupo=True),
        _CapaTOC(u"A\\Hidro", visible=False, grupo=True),
        _CapaTOC(u"A\\Hidro\\Rios"),
        _CapaTOC(u"B", grupo=True),
        _CapaTOC(u"B\\Hidro", grupo=True),
        _CapaTOC(u"B\\Hidro\\Rios"),
    ])])
    capas = _por_largo(salida)
    assert capas[u"A\\Hidro\\Rios"]["visible_efectivo"] is False, capas
    assert capas[u"B\\Hidro\\Rios"]["visible_efectivo"] is True, capas


@caso
def auditor_marco_fuera_de_la_hoja_no_sale_en_el_plano():
    salida = _auditar([
        (("PLANO", 1, 1), [_CapaTOC(u"Montes")]),
        (("Nuevo marco de datos", 60, 5), [_CapaTOC(u"Otro monte", query=u"MONTE = 7",
                                                    rota=True)]),
        (("Situacion", 35, 20), [_CapaTOC(u"Provincia")]),
    ])
    marcos = dict((m["nombre"], m) for m in salida["marcos"])
    assert marcos["PLANO"]["en_pagina"] == "dentro", marcos
    assert marcos["Nuevo marco de datos"]["en_pagina"] == "fuera", marcos
    assert marcos["Situacion"]["en_pagina"] == "parcial", marcos
    capas = _por_largo(salida)
    otro = capas[u"Otro monte"]
    assert otro["visible_efectivo"] is True and otro["en_plano"] is False, otro
    assert capas[u"Provincia"]["en_plano"] is True, capas
    assert salida["num_con_query"] == 1 and salida["num_con_query_en_plano"] == 0, salida
    assert salida["num_rotas_en_plano"] == 0, salida
    assert salida["data_frames"] == ["PLANO", "Nuevo marco de datos", "Situacion"], salida


@caso
def auditor_visibilidad_ilegible_no_se_da_por_apagada():
    salida = _auditar([(("PLANO", 1, 1), [
        _CapaTOC(u"G", visible=None, grupo=True),
        _CapaTOC(u"G\\Rota", rota=True),
        _CapaTOC(u"Apagada", visible=False, rota=True),
    ])])
    capas = _por_largo(salida)
    assert capas[u"G\\Rota"]["visible_efectivo"] is None, capas
    assert capas[u"Apagada"]["en_plano"] is False, capas
    assert salida["num_en_plano_desconocido"] == 1, salida
    assert salida["num_rotas_en_plano"] == 0 and salida["num_rotas"] == 2, salida


@caso
def auditor_sin_tamano_de_pagina_no_adivina():
    class _SinPagina(object):
        @property
        def pageSize(self):
            raise RuntimeError("sin layout")
    _auditar([(("PLANO", 1, 1), [_CapaTOC(u"Montes")])])  # monta marcos y capas
    ARCPY.mapping.MapDocument = lambda ruta: _SinPagina()
    salida = auditor.auditar(u"C:\\x\\plano.mxd")
    assert salida["marcos"][0]["en_pagina"] is None, salida
    assert salida["capas"][0]["en_plano"] is None, salida
    assert salida["capas"][0]["visible_efectivo"] is True, salida


# --------------------------------------------------------------------------- #
# run_geoprocessing(fuera_de_arcmap=True) -> op_geoprocessing (2.16.0)
# --------------------------------------------------------------------------- #

def _gp(tool, params, **kw):
    p = {"tool": tool, "params": params}
    p.update(kw)
    return runner.op_geoprocessing({"params": p})


@caso
def gp_fuera_forma_punteada_y_clasica():
    del LLAMADAS[:]
    assert _gp(u"management.GetCount", [u"a.shp"])["salidas"] == [u"24"]
    assert _gp(u"GetCount_management", [u"a.shp"])["salidas"] == [u"24"]
    assert LLAMADAS == [("GetCount", u"a.shp")] * 2, LLAMADAS


@caso
def gp_fuera_tool_inexistente_lo_dice():
    try:
        _gp(u"management.NoExiste", [u"x"])
    except ValueError as ex:
        assert u"No existe la herramienta" in unicode(ex), unicode(ex)
        return
    raise AssertionError("una tool inexistente tenia que fallar")


@caso
def gp_fuera_parametros_de_mas_dan_la_firma():
    del LLAMADAS[:]
    try:
        _gp(u"management.GetCount", [u"a.shp", u"sobra"])
    except ValueError as ex:
        assert u"Usage: GetCount_management" in unicode(ex), unicode(ex)
        assert LLAMADAS == [], LLAMADAS
        return
    raise AssertionError("con parametros de mas tenia que fallar")


@caso
def gp_fuera_no_sobrescribe_por_defecto():
    ARCPY.env.overwriteOutput = True
    _gp(u"management.GetCount", [u"a.shp"])
    assert ARCPY.env.overwriteOutput is False, ARCPY.env.overwriteOutput
    _gp(u"management.GetCount", [u"a.shp"], sobrescribir=True)
    assert ARCPY.env.overwriteOutput is True, ARCPY.env.overwriteOutput


@caso
def gp_fuera_000258_con_sobrescribir_explica_el_bloqueo():
    FALLA_GP[u"bloqueada.shp"] = u"ERROR 000258: Ya existe la salida"
    try:
        for sobrescribir, pista in ((True, True), (False, False)):
            try:
                _gp(u"management.GetCount", [u"bloqueada.shp"], sobrescribir=sobrescribir)
            except ValueError as ex:
                texto = unicode(ex)
                assert u"000258" in texto, texto
                assert (u"Usa otra ruta" in texto) == pista, (sobrescribir, texto)
            else:
                raise AssertionError("tenia que fallar")
    finally:
        FALLA_GP.clear()


@caso
def gp_fuera_capas_salida_solo_datos_que_existen():
    EXISTENTES.clear()
    TIPOS.clear()
    EXISTENTES.add(u"C:\\s\\copia.shp")
    TIPOS[u"C:\\s\\copia.shp"] = u"ShapeFile"
    try:
        assert _gp(u"management.CopyFeatures", [u"a.shp", u"C:\\s\\copia.shp"])["capas_salida"] \
            == [u"C:\\s\\copia.shp"]
        # un derivado (el recuento) no es una capa, aunque sea la salida
        assert _gp(u"management.GetCount", [u"a.shp"])["capas_salida"] == []
    finally:
        EXISTENTES.clear()
        TIPOS.clear()


# --------------------------------------------------------------------------- #

def main():
    fallos = [r for r in RESULTADOS if not r["ok"]]
    sys.stdout.write(json.dumps({"ok": not fallos, "casos": RESULTADOS},
                                ensure_ascii=True))
    shutil.rmtree(_TMP, ignore_errors=True)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
