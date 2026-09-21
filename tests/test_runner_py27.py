# -*- coding: utf-8 -*-
"""
test_runner_py27.py  ──  Ejercita el runner y el auditor con el Python 2.7 de ArcGIS.

Los dos ficheros que corren bajo ArcMap son Python 2.7 (`runner.py`, embebido en la
DLL del add-in, y `src/auditor_mxd.py`), y esta suite corre en Python 3: no se
pueden importar. Se lanzan como SUBPROCESO con el intérprete de ArcGIS, que es como
los ejecuta el add-in de verdad.

    python -m unittest discover -s tests -v

Sin ese intérprete en la máquina, todo esto se salta (skip) en vez de fallar: son
tests de una pieza que solo existe donde hay ArcGIS instalado.

`casos_runner_py27.py` lleva los casos del runner y NO importa arcpy: lo sustituye
por un stub en `sys.modules`. Importar arcpy de verdad cuesta ~6 s y toma una
licencia de Desktop, y nada de lo que se prueba ahí (codificación, control de flujo,
nombrado de salidas) necesita geoprocesar. El del auditor sí paga ese precio, porque
lo que se prueba es justo el camino completo hasta arcpy.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from arcmap_mcp_server import _python27_arcgis  # noqa: E402

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.join(AQUI, "..")
CASOS = os.path.join(AQUI, "casos_runner_py27.py")
AUDITOR = os.path.join(RAIZ, "src", "auditor_mxd.py")


def python27():
    py = _python27_arcgis()
    if not py:
        raise unittest.SkipTest("sin Python 2.7 de ArcGIS en esta máquina "
                                "(defínelo con ARCMAP_PYTHON27)")
    return py


class TestRunnerBajoPython27(unittest.TestCase):
    """Los casos de `casos_runner_py27.py`, uno a uno."""

    @classmethod
    def setUpClass(cls):
        py = python27()
        proc = subprocess.run([py, CASOS], capture_output=True, timeout=120)
        bruto = proc.stdout.decode("utf-8", "replace").strip()
        if not bruto:
            raise AssertionError(
                "los casos no escribieron nada. stderr:\n%s"
                % proc.stderr.decode("utf-8", "replace"))
        cls.informe = json.loads(bruto)

    def test_todos_los_casos_pasan(self):
        for caso in self.informe["casos"]:
            with self.subTest(caso=caso["nombre"]):
                self.assertTrue(caso["ok"], caso["error"])

    def test_el_guion_ejercita_los_cuatro_frentes(self):
        """Si alguien borra media tabla de casos, el resto seguiría en verde."""
        nombres = " ".join(c["nombre"] for c in self.informe["casos"])
        for marca in ("stdout_mezclado", "sys_exit", "serializar_sanea",
                      "export_ddp_delata", "salida_existente_aborta",
                      "backlink_es_unico"):
            self.assertIn(marca, nombres)


class TestAuditorConRutaAcentuada(unittest.TestCase):
    """El fallo del 2026-09-20, lanzado como lo lanza `audit_folder`.

    En Python 2 sobre Windows `sys.argv` llega en bytes de la codepage ANSI, y el
    `json.dumps` final los decodificaba como UTF-8: UnicodeDecodeError fuera de todo
    try, stdout VACÍO y `audit_folder` reportando "sin salida" en toda la carpeta.
    Por eso el test no mira el contenido de la auditoría, sino que SALGA JSON.
    """

    @classmethod
    def setUpClass(cls):
        cls.py = python27()
        cls.tmp = tempfile.mkdtemp(prefix="auditor_")
        cls.carpeta = os.path.join(cls.tmp, "Cartografía ñoño")
        os.makedirs(cls.carpeta, exist_ok=True)
        cls.mxd = os.path.join(cls.carpeta, "plano añejo.mxd")
        with open(cls.mxd, "wb") as fh:
            fh.write(b"esto no es un mxd")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _auditar(self, ruta):
        try:
            proc = subprocess.run([self.py, AUDITOR, ruta],
                                  capture_output=True, timeout=240)
        except subprocess.TimeoutExpired:
            self.fail("el auditor no terminó en 240 s. Causa habitual: ArcMap "
                      "abierto, que bloquea al arcpy standalone al tomar la licencia.")
        bruto = proc.stdout.decode("utf-8", "replace").strip()
        self.assertTrue(bruto, "stdout VACÍO (el fallo original). stderr:\n%s"
                        % proc.stderr.decode("utf-8", "replace"))
        return json.loads(bruto)   # si no es JSON, revienta aquí y se ve

    def test_devuelve_json_y_no_un_traceback(self):
        datos = self._auditar(self.mxd)
        self.assertFalse(datos["ok"])
        self.assertTrue(datos.get("error"), datos)

    def test_la_ruta_vuelve_con_sus_acentos(self):
        """Si la codificación se decidiera mal, la ruta volvería como mojibake y el
        informe señalaría a un fichero que no existe."""
        datos = self._auditar(self.mxd)
        self.assertEqual(datos["ruta"], self.mxd)

    def test_ruta_acentuada_que_no_existe(self):
        """El otro camino: aquí no hay fichero en disco que arbitre la codificación."""
        datos = self._auditar(os.path.join(self.carpeta, "no_existe_ñ.mxd"))
        self.assertFalse(datos["ok"])
        self.assertTrue(datos.get("error"), datos)

    def test_sin_argumentos(self):
        proc = subprocess.run([self.py, AUDITOR], capture_output=True, timeout=60)
        datos = json.loads(proc.stdout.decode("utf-8", "replace").strip())
        self.assertFalse(datos["ok"])


class TestPython27SigueSiendoValido(unittest.TestCase):
    """`runner.py` va EMBEBIDO en la DLL y `auditor_mxd.py` lo lanza el servidor con
    el intérprete de ArcGIS: un f-string o cualquier sintaxis de Python 3 no se
    detecta hasta que falla en producción, dentro de ArcMap."""

    # `python -m py_compile` deja el .pyc AL LADO del fuente, y al lado de
    # runner.py está el fichero que se embebe en la DLL: el bytecode se manda a un
    # temporal para que correr los tests no ensucie el árbol del add-in.
    COMPILA = ("import py_compile, sys; "
               "py_compile.compile(sys.argv[1], cfile=sys.argv[2], doraise=True)")

    def test_compilan_con_el_python_de_arcgis(self):
        py = python27()
        destino = os.path.join(tempfile.mkdtemp(prefix="pyc27_"), "salida.pyc")
        self.addCleanup(lambda: __import__("shutil").rmtree(
            os.path.dirname(destino), ignore_errors=True))
        for fichero in (os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Python",
                                     "runner.py"),
                        AUDITOR, CASOS):
            with self.subTest(fichero=os.path.basename(fichero)):
                proc = subprocess.run([py, "-c", self.COMPILA, fichero, destino],
                                      capture_output=True, timeout=120)
                self.assertEqual(proc.returncode, 0,
                                 proc.stderr.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
