# -*- coding: utf-8 -*-
"""
test_client_protocol.py  ──  Tests automáticos de `ArcMapClient.send` SIN ArcMap.

Levanta un puente falso en un socket local y comprueba cómo reacciona el cliente
a cada forma de respuesta. Corre en CI y en cualquier máquina: no necesita ArcMap,
ni arcpy, ni el add-in.

    python -m unittest discover -s tests -v

QUÉ SE PRUEBA Y POR QUÉ. Cada caso cubre un defecto que ya ocurrió de verdad, no
una hipótesis:

  - `puente_caido` vs `puente_ocupado`: los dos se confundían desde fuera y llevaron
    a relanzar geoprocesos que seguían vivos dentro de ArcMap.
  - Respuesta troceada a mitad de un carácter multibyte: lanzaba `UnicodeDecodeError`
    sin capturar. El parseo por chunk además era O(n²) con payloads grandes
    (screenshots en base64).
  - Cierre sin datos y respuesta no-JSON: devolvían excepción en vez de un error
    con forma de resultado.

Lo que NO se prueba aquí, a propósito: las 49 tools. Todas delegan en
`ArcMapClient.send` sin lógica propia, así que probarlas exigiría ArcMap vivo y no
añadiría cobertura sobre el protocolo. Eso es trabajo de una suite end-to-end
aparte, con un MXD fijo de pruebas.
"""
import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from arcmap_mcp_server import ArcMapClient  # noqa: E402


class PuenteFalso(object):
    """Servidor de un solo uso que responde como le digamos.

    `modo` decide el comportamiento:
      'json'      → devuelve `payload` serializado y cierra
      'crudo'     → devuelve `payload` (bytes) tal cual y cierra
      'troceado'  → devuelve `payload` (bytes) en trozos que parten caracteres
      'vacio'     → acepta y cierra sin enviar nada
      'colgado'   → acepta y no envía nada hasta que el cliente se cansa
    """

    def __init__(self, modo, payload=None):
        self.modo = modo
        self.payload = payload
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._parar = threading.Event()
        self._hilo = threading.Thread(target=self._servir)
        self._hilo.daemon = True
        self._hilo.start()

    def _servir(self):
        try:
            self._srv.settimeout(10)
            cli, _ = self._srv.accept()
        except (socket.timeout, OSError):
            return
        try:
            cli.settimeout(10)
            try:
                cli.recv(65536)
            except (socket.timeout, OSError):
                pass
            if self.modo == "json":
                cli.sendall(json.dumps(self.payload).encode("utf-8"))
            elif self.modo == "crudo":
                cli.sendall(self.payload)
            elif self.modo == "troceado":
                # Trozos de 7 bytes: con acentos y emoji parte caracteres por medio,
                # que es exactamente lo que reventaba al decodificar por chunk.
                datos = self.payload
                for i in range(0, len(datos), 7):
                    cli.sendall(datos[i:i + 7])
            elif self.modo == "colgado":
                self._parar.wait(5)
        except OSError:
            pass
        finally:
            try:
                cli.close()
            except OSError:
                pass

    def cerrar(self):
        self._parar.set()
        try:
            self._srv.close()
        except OSError:
            pass


class TestPuenteCaido(unittest.TestCase):
    """Nadie escucha en el puerto."""

    def test_devuelve_estado_puente_caido(self):
        # Puerto que nadie ocupa: se reserva uno y se suelta antes de llamar.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        puerto_muerto = s.getsockname()[1]
        s.close()

        cli = ArcMapClient(host="127.0.0.1", port=puerto_muerto, timeout=2)
        r = cli.send("ping")

        self.assertFalse(r["ok"])
        self.assertEqual(r["estado"], "puente_caido")
        self.assertIn(str(puerto_muerto), r["error"])

    def test_no_se_confunde_con_ocupado(self):
        """El fallo que costó tiempo real: los dos estados eran indistinguibles."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        puerto_muerto = s.getsockname()[1]
        s.close()

        cli = ArcMapClient(host="127.0.0.1", port=puerto_muerto, timeout=2)
        r = cli.send("ping")
        self.assertNotEqual(r.get("estado"), "puente_ocupado")


class TestPuenteOcupado(unittest.TestCase):
    """La conexión se abre pero ArcMap no contesta: hilo principal ocupado."""

    def test_devuelve_estado_puente_ocupado(self):
        srv = PuenteFalso("colgado")
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=1)
        r = cli.send("ping")

        self.assertFalse(r["ok"])
        self.assertEqual(r["estado"], "puente_ocupado")

    def test_avisa_de_que_el_geoproceso_sigue_vivo(self):
        """Relanzar es el error caro: el mensaje tiene que decirlo."""
        srv = PuenteFalso("colgado")
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=1)
        r = cli.send("run_geoprocessing")
        self.assertIn("SIGUE CORRIENDO", r["error"])


class TestRespuestas(unittest.TestCase):
    """Formas de respuesta que ya rompieron el cliente alguna vez."""

    def test_respuesta_normal(self):
        srv = PuenteFalso("json", {"ok": True, "version": "10.5"})
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=5)
        r = cli.send("ping")

        self.assertTrue(r["ok"])
        self.assertEqual(r["version"], "10.5")

    def test_multibyte_troceado_no_revienta(self):
        """Regresión: un chunk cortado a mitad de carácter daba UnicodeDecodeError."""
        payload = json.dumps(
            {"ok": True, "capas": ["Ríos", "Montañas", "Encinar señalizado ñÁÉ"]},
            ensure_ascii=False,
        ).encode("utf-8")
        srv = PuenteFalso("troceado", payload)
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=5)
        r = cli.send("list_layers")

        self.assertTrue(r["ok"])
        self.assertEqual(r["capas"][0], "Ríos")
        self.assertEqual(r["capas"][2], "Encinar señalizado ñÁÉ")

    def test_payload_grande(self):
        """Los screenshots en base64 son de este tamaño; el parseo por chunk era O(n²)."""
        grande = {"ok": True, "imagen": "A" * 400000}
        srv = PuenteFalso("json", grande)
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=15)
        r = cli.send("get_canvas_screenshot")

        self.assertTrue(r["ok"])
        self.assertEqual(len(r["imagen"]), 400000)

    def test_cierre_sin_datos(self):
        srv = PuenteFalso("vacio")
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=5)
        r = cli.send("ping")

        self.assertFalse(r["ok"])
        self.assertIn("sin respuesta", r["error"].lower())

    def test_respuesta_no_json(self):
        srv = PuenteFalso("crudo", b"<html>error 500</html>")
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=5)
        r = cli.send("ping")

        self.assertFalse(r["ok"])
        self.assertIn("ilegible", r["error"].lower())


class TestContratoDeTools(unittest.TestCase):
    """Invariante del que depende todo lo anterior."""

    # Tools que NO hablan con el puente a propósito, con el motivo al lado. La
    # lista es explícita para que añadir una obligue a justificarla aquí: si se
    # relajara la regla, una tool que hablara al socket por su cuenta se colaría
    # sin que ningún test la cubriera.
    SIN_PUENTE = {
        "describe_mxd": "lee el .mxd del disco; tiene que funcionar con ArcMap cerrado",
        "audit_folder": "audita una carpeta de .mxd con arcpy standalone; sin sesión viva",
    }

    def test_todas_las_tools_pasan_por_send(self):
        """Si una tool hablara al socket por su cuenta, estos tests no la cubrirían."""
        ruta = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "src", "arcmap_mcp_server.py"
        )
        with open(ruta, encoding="utf-8") as fh:
            src = fh.read()

        bloques = src.split("@mcp.tool()")[1:]
        self.assertGreater(len(bloques), 40, "se esperaban ~50 tools")

        sueltas = []
        for bloque in bloques:
            cuerpo = bloque.split("@mcp.tool()")[0]
            if "_client.send" in cuerpo:
                continue
            nombre = ""
            for linea in cuerpo.strip().splitlines():
                if linea.startswith("def "):
                    nombre = linea[4:].split("(")[0]
                    break
            if nombre not in self.SIN_PUENTE:
                sueltas.append(nombre or "?")

        self.assertEqual(sueltas, [], "tools que no delegan en _client.send: %s" % sueltas)

    def test_las_excepciones_siguen_existiendo(self):
        """Si una tool de SIN_PUENTE desaparece, la lista miente y hay que podarla."""
        ruta = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "src", "arcmap_mcp_server.py"
        )
        with open(ruta, encoding="utf-8") as fh:
            src = fh.read()
        for nombre in self.SIN_PUENTE:
            self.assertIn("def %s(" % nombre, src)


class TestVersionMxd(unittest.TestCase):
    """Lectura del .mxd sin arcpy: `describe_mxd` y sus piezas."""

    def test_comparador_de_versiones(self):
        from arcmap_mcp_server import _a_tupla

        self.assertEqual(_a_tupla("10.5"), (10, 5))
        self.assertEqual(_a_tupla("9.3"), (9, 3))
        self.assertIsNone(_a_tupla(None))
        self.assertIsNone(_a_tupla(""))
        self.assertIsNone(_a_tupla("no-es-version"))
        # Lo que importa: 10.8 > 10.5 numéricamente. Comparado como texto sería
        # falso ("10.8" < "10.5" no, pero "10.10" < "10.5" sí), y ese es
        # justamente el error que haría inútil el aviso el día que exista 10.10.
        self.assertGreater(_a_tupla("10.8"), _a_tupla("10.5"))
        self.assertGreater(_a_tupla("10.10"), _a_tupla("10.9"))

    def test_fichero_que_no_es_mxd(self):
        from arcmap_mcp_server import _mxd_version_declarada

        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")
        version, error = _mxd_version_declarada(ruta)
        self.assertIsNone(version)
        self.assertIn("compound document", error)

    def test_fichero_inexistente(self):
        from arcmap_mcp_server import _mxd_version_declarada

        version, error = _mxd_version_declarada(r"C:\no\existe\jamas.mxd")
        self.assertIsNone(version)
        self.assertIn("no se pudo leer", error)

    def test_no_revienta_con_basura_binaria(self):
        """Un fichero con la firma correcta y el resto roto no debe lanzar."""
        import tempfile

        from arcmap_mcp_server import _mxd_version_declarada

        with tempfile.NamedTemporaryFile(suffix=".mxd", delete=False) as fh:
            fh.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
            tmp = fh.name
        self.addCleanup(os.unlink, tmp)

        version, error = _mxd_version_declarada(tmp)  # no debe lanzar
        self.assertIsNone(version)
        self.assertTrue(error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
