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
  - Presupuestos de espera: media docena de tools largas usaban el timeout corto de
    60 s y el relay cortaba con un "puente ocupado" falso mientras el export seguía.

Lo que NO se prueba aquí, a propósito: el comportamiento de las 49 tools. Todas
delegan en `ArcMapClient.send` sin lógica propia, así que probarlas exigiría ArcMap
vivo y no añadiría cobertura sobre el protocolo. Lo que SÍ se prueba de ellas es el
timeout que le pasan a `send`, que es una decisión del servidor y se comprueba con un
cliente espía. El resto es trabajo de una suite end-to-end aparte, con un MXD fijo.
"""
import json
import os
import re
import socket
import sys
import threading
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import arcmap_mcp_server as servidor  # noqa: E402
from arcmap_mcp_server import ArcMapClient  # noqa: E402

RAIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")


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

    def test_manda_a_ping_y_no_al_hilo_principal(self):
        """El mensaje describía el puente Python viejo: decía que ArcMap atiende el
        socket en su hilo principal (hoy el listener va en un hilo de fondo y `ping`
        contesta igual). Quien lo leyera daba por colgado lo que solo estaba ocupado."""
        srv = PuenteFalso("colgado")
        self.addCleanup(srv.cerrar)

        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=1)
        r = cli.send("export_ddp")

        self.assertIn("ping", r["error"])
        self.assertIn("comando_en_curso", r["error"])
        self.assertIn("ocupado_desde_s", r["error"])
        self.assertNotIn("hilo principal", r["error"])

    def _ocupado(self, ctype, variable=None):
        """Un puente colgado NUEVO por llamada: `PuenteFalso` atiende una sola vez.

        El presupuesto que se pasa es el mismo `_Espera` de las tools pero de 1 s:
        aquí se comprueba el NOMBRE que viaja pegado al valor, no cuánto se espera
        (con los 2760 s de verdad el puente falso cerraría antes y daría otro error).
        """
        srv = PuenteFalso("colgado")
        self.addCleanup(srv.cerrar)
        cli = ArcMapClient(host="127.0.0.1", port=srv.port, timeout=1)
        if variable is None:
            return cli.send(ctype)
        with unittest.mock.patch.dict(os.environ, {variable: "1"}):
            presupuesto = servidor._Espera(variable, 1)
        return cli.send(ctype, timeout=presupuesto)

    def test_nombra_la_variable_de_fondo_y_no_la_de_gp(self):
        """Decía siempre ARCMAP_GP_TIMEOUT, que no gobierna ni los exports ni las DDP:
        mandaba a subir un número que no iba a cambiar nada."""
        r = self._ocupado("export_ddp", "ARCMAP_FONDO_TIMEOUT")
        self.assertEqual(r["estado"], "puente_ocupado")
        self.assertIn("ARCMAP_FONDO_TIMEOUT", r["error"])
        self.assertNotIn("ARCMAP_GP_TIMEOUT", r["error"])

    def test_nombra_la_variable_de_guardado(self):
        r = self._ocupado("save_mxd", "ARCMAP_SAVE_TIMEOUT")
        self.assertIn("ARCMAP_SAVE_TIMEOUT", r["error"])

    def test_sin_presupuesto_propio_nombra_el_del_cliente(self):
        r = self._ocupado("list_layers")
        self.assertIn("ARCMAP_BRIDGE_TIMEOUT", r["error"])


class ClienteEspia(object):
    """Cliente de mentira que apunta con qué se le llama y no habla con nadie."""

    def __init__(self):
        self.llamadas = []

    def send(self, ctype, params=None, timeout=None):
        self.llamadas.append({"ctype": ctype, "params": params or {}, "timeout": timeout})
        return {"ok": True}


class TestPresupuestosDeEspera(unittest.TestCase):
    """Qué presupuesto de espera pasa cada tool.

    El defecto real: `export_pdf`, `export_jpg`, `export_view_png` y las tres de DDP
    se quedaban con el timeout corto (60 s) mientras el add-in les daba 1800 s. El
    relay cortaba a los 60 s con un "puente ocupado" que era mentira —el export
    seguía— y el usuario relanzaba encima.
    """

    # tool -> (argumentos, variable de entorno que debe gobernar su espera)
    ESPERADOS = {
        "export_pdf":          (("x.pdf",), "ARCMAP_GP_TIMEOUT"),
        "export_jpg":          (("x.jpg",), "ARCMAP_GP_TIMEOUT"),
        "export_view_png":     (("x.png",), "ARCMAP_GP_TIMEOUT"),
        "run_geoprocessing":   (("management.CopyFeatures",), "ARCMAP_GP_TIMEOUT"),
        "calculate_geometry":  (("capa", "AREA"), "ARCMAP_GP_TIMEOUT"),
        "list_ddp":            ((), "ARCMAP_FONDO_TIMEOUT"),
        "export_ddp":          (("x.pdf",), "ARCMAP_FONDO_TIMEOUT"),
        "goto_ddp_page":       ((1,), "ARCMAP_FONDO_TIMEOUT"),
        "raster_index":        (("NDVI", {"NIR": "a", "RED": "b"}, "o.tif"),
                                "ARCMAP_FONDO_TIMEOUT"),
        "hydrology":           (("inundacion", {"mdt": "m", "nivel": 1, "salida": "s"}),
                                "ARCMAP_FONDO_TIMEOUT"),
        "contours":            (("m.tif", "s.shp", 10), "ARCMAP_FONDO_TIMEOUT"),
        "topographic_profile": (("m.tif", "l.shp", "o.shp"), "ARCMAP_FONDO_TIMEOUT"),
        "least_cost_path":     (("c.tif", "o.shp", "d.shp", "s.tif"),
                                "ARCMAP_FONDO_TIMEOUT"),
        "save_mxd":            ((), "ARCMAP_SAVE_TIMEOUT"),
        "save_mxd_as":         (("x.mxd",), "ARCMAP_SAVE_TIMEOUT"),
        "execute_arcpy":       (("RESULT = 1",), "ARCMAP_EXEC_TIMEOUT_CLIENTE"),
    }

    # Contraste: éstas son instantáneas y tienen que seguir con el timeout corto.
    # Sin este lado, "subir todos los timeouts" pasaría la otra mitad del test.
    RAPIDAS = {"ping": (), "list_layers": (), "set_scale": (1000,),
               "refresh": (), "get_bookmarks": ()}

    def setUp(self):
        self.espia = ClienteEspia()
        self.original = servidor._client
        servidor._client = self.espia
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _llamar(self, tool, args, **kwargs):
        getattr(servidor, tool)(*args, **kwargs)
        return self.espia.llamadas[-1]

    def _timeout_de(self, tool, args):
        return self._llamar(tool, args)["timeout"]

    def test_cada_tool_larga_pasa_su_presupuesto(self):
        for tool, (args, variable) in sorted(self.ESPERADOS.items()):
            timeout = self._timeout_de(tool, args)
            self.assertIsNotNone(timeout, "%s no pasa timeout: se queda con los 60 s "
                                          "del cliente" % tool)
            self.assertEqual(getattr(timeout, "variable", None), variable,
                             "%s espera segun %s, no segun %s"
                             % (tool, getattr(timeout, "variable", "?"), variable))

    def test_las_rapidas_siguen_con_el_timeout_corto(self):
        for tool, args in sorted(self.RAPIDAS.items()):
            self.assertIsNone(self._timeout_de(tool, args),
                              "%s no necesita timeout largo" % tool)

    def test_las_ambientales_reenvian_sobrescribir(self):
        """Si el server no lo reenvía, el runner nunca ve el parámetro y la tool
        acepta `sobrescribir=true` sin que sirva de nada."""
        ambientales = {
            "raster_index": ("NDVI", {"NIR": "a", "RED": "b"}, "o.tif"),
            "hydrology": ("inundacion", {"mdt": "m", "nivel": 1, "salida": "s"}),
            "contours": ("m.tif", "s.shp", 10),
            "topographic_profile": ("m.tif", "l.shp", "o.shp"),
            "least_cost_path": ("c.tif", "o.shp", "d.shp", "s.tif"),
        }
        for tool, args in sorted(ambientales.items()):
            pedido = self._llamar(tool, args, sobrescribir=True)
            self.assertEqual(pedido["ctype"], tool)
            self.assertIs(pedido["params"].get("sobrescribir"), True,
                          "%s no reenvía sobrescribir" % tool)
            defecto = self._llamar(tool, args)
            self.assertIs(defecto["params"].get("sobrescribir"), False,
                          "%s no manda sobrescribir=False por defecto" % tool)

    def test_los_export_pisan_por_defecto_y_save_mxd_as_no(self):
        """Los defectos van al revés a propósito: un PDF pisado se regenera (y una
        serie de planos lo necesita), un .mxd pisado es trabajo de alguien."""
        for tool in ("export_pdf", "export_jpg", "export_view_png"):
            self.assertIs(self._llamar(tool, ("C:/o.x",))["params"]["sobrescribir"], True, tool)
            self.assertIs(self._llamar(tool, ("C:/o.x",), sobrescribir=False)
                          ["params"]["sobrescribir"], False, tool)
        self.assertIs(self._llamar("save_mxd_as", ("C:/o.mxd",))["params"]["sobrescribir"], False)
        self.assertIs(self._llamar("save_mxd_as", ("C:/o.mxd",), sobrescribir=True)
                      ["params"]["sobrescribir"], True)

    def test_parametros_nuevos_del_addin_llegan_al_puente(self):
        """Un parámetro que el add-in entiende y el relay no reenvía es un parámetro
        que no existe: la tool lo acepta y no pasa nada."""
        p = self._llamar("get_unique_values", ("capa", "campo"), max_valores=7)["params"]
        self.assertEqual(p["max_valores"], 7)
        p = self._llamar("set_graduated_symbology", ("capa", "campo"),
                         algoritmo="hsv", color_desde="#FFFFB2")["params"]
        self.assertEqual((p["algoritmo"], p["color_desde"]), ("hsv", "#FFFFB2"))
        p = self._llamar("set_unique_values_symbology", ("capa", "campo"),
                         algoritmo="lablch")["params"]
        self.assertEqual(p["algoritmo"], "lablch")
        p = self._llamar("repair_data_source", ("capa", "a", "b"), data_frame="Detalle")["params"]
        self.assertEqual(p["data_frame"], "Detalle")
        # Sin indicarlos NO viajan: el add-in aplica su propio defecto.
        p = self._llamar("set_graduated_symbology", ("capa", "campo"))["params"]
        self.assertNotIn("algoritmo", p)
        self.assertNotIn("data_frame",
                         self._llamar("repair_data_source", ("capa", "a", "b"))["params"])

    def test_serializar_sesion_tiene_su_propio_presupuesto(self):
        """Con serializar_sesion el add-in gasta hasta 600 s copiando ANTES de sus
        900 s de ejecución: con los 930 de siempre ganaba el corte mudo de socket."""
        normal = self._llamar("execute_arcpy", ("RESULT = 1",))["timeout"]
        sesion = self._llamar("execute_arcpy", ("RESULT = 1",), serializar_sesion=True)["timeout"]
        self.assertEqual(normal.variable, "ARCMAP_EXEC_TIMEOUT_CLIENTE")
        self.assertEqual(sesion.variable, "ARCMAP_EXEC_SESION_TIMEOUT_CLIENTE")
        self.assertGreater(sesion, 600 + 900)


class TestNoEmpatarConElAddIn(unittest.TestCase):
    """El invariante del que salen todos los números de arriba.

    Cuando los dos topes EMPATAN gana el corte mudo del socket: el add-in se queda
    sin devolver su error con fase y el trabajo queda huérfano dentro de ArcMap.
    Por eso cada presupuesto del relay va ESTRICTAMENTE por encima del tope que el
    add-in aplica al mismo comando. Se lee del .cs para que el día que alguien mueva
    el tope de allí, esto lo diga aquí.
    """

    CS = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn")

    def _segundos(self, fichero, patron):
        ruta = os.path.join(self.CS, fichero)
        if not os.path.isfile(ruta):
            self.skipTest("sin fuentes del add-in en esta copia: %s" % ruta)
        with open(ruta, encoding="utf-8") as fh:
            m = re.search(patron, fh.read())
        self.assertIsNotNone(
            m, "no se encontró '%s' en %s: si el tope del add-in cambió de nombre, "
               "revisa a mano que los timeouts del relay siguen por encima"
               % (patron, fichero))
        return int(m.group(1))

    def test_gp_por_encima_del_tope_de_comandos_largos(self):
        tope = self._segundos("McpServer.cs",
                              r"LongHandlerTimeout\s*=\s*TimeSpan\.FromSeconds\((\d+)\)")
        self.assertGreater(int(servidor.GP_TIMEOUT), tope)

    def test_fondo_por_encima_del_techo_de_handlers_de_fondo(self):
        tope = self._segundos("McpServer.cs",
                              r"FondoTimeout\s*=\s*TimeSpan\.FromSeconds\((\d+)\)")
        self.assertGreater(int(servidor.FONDO_TIMEOUT), tope)

    def test_guardado_por_encima_del_tope_de_guardado(self):
        tope = self._segundos("McpServer.cs",
                              r"GuardadoTimeout\s*=\s*TimeSpan\.FromSeconds\((\d+)\)")
        self.assertGreater(int(servidor.SAVE_TIMEOUT), tope)

    def test_exec_por_encima_del_tope_de_execute(self):
        tope = self._segundos(os.path.join("Handlers", "PythonHandlers.cs"),
                              r'ExecuteTimeout\s*=\s*LeerTimeout\("ARCMAP_EXEC_TIMEOUT",\s*(\d+)\)')
        self.assertGreater(int(servidor.EXEC_TIMEOUT), tope)


class TestAuditFolderValida(unittest.TestCase):
    """Números absurdos que no fallaban: MENTÍAN.

    `max_documentos=0` devolvía una auditoría vacía con cara de completa; en negativo,
    `encontrados[:-5]` recorta los ÚLTIMOS cinco y el aviso dice "los -5 primeros"; y
    `timeout_por_documento=0` hace que subprocess corte al instante y todos los
    documentos salgan marcados como colgados.
    """

    def test_max_documentos_cero(self):
        r = servidor.audit_folder(RAIZ, con_capas=False, max_documentos=0)
        self.assertFalse(r["ok"])
        self.assertIn("max_documentos", r["error"])

    def test_max_documentos_negativo(self):
        r = servidor.audit_folder(RAIZ, con_capas=False, max_documentos=-5)
        self.assertFalse(r["ok"])
        self.assertIn("max_documentos", r["error"])

    def test_timeout_no_positivo(self):
        for valor in (0, -1):
            r = servidor.audit_folder(RAIZ, con_capas=False, timeout_por_documento=valor)
            self.assertFalse(r["ok"])
            self.assertIn("timeout_por_documento", r["error"])

    def test_no_numerico(self):
        r = servidor.audit_folder(RAIZ, con_capas=False, max_documentos="muchos")
        self.assertFalse(r["ok"])
        self.assertIn("enteros", r["error"])

    def test_valores_validos_siguen_pasando(self):
        """La validación no puede haberse comido el camino bueno."""
        r = servidor.audit_folder(RAIZ, con_capas=False, max_documentos=5,
                                  timeout_por_documento=30)
        self.assertTrue(r["ok"])
        self.assertEqual(r["mxd_encontrados"], 0)  # la raíz del repo no tiene .mxd


class TestArgumentosEstrictos(unittest.TestCase):
    """Un argumento que la tool no declara es ERROR, no se ignora.

    El 2026-09-22 `export_jpg(salida=..., mxd=<otro plano>, resolucion=230)` exportó
    el documento ABIERTO con `ok: true`: FastMCP tiraba `mxd` y `resolucion` sin decir
    nada. Estos tests pasan por `call_tool`, el mismo camino que una llamada real.
    """

    def _llamar(self, nombre, args):
        import asyncio
        return asyncio.run(servidor.mcp.call_tool(nombre, args))

    def test_el_caso_del_22_de_septiembre(self):
        with self.assertRaises(Exception) as ctx:
            self._llamar("export_jpg", {"salida": r"C:\x.jpg", "mxd": r"C:\otro.mxd",
                                        "resolucion": 230})
        texto = str(ctx.exception)
        self.assertIn("mxd", texto)
        self.assertIn("resolucion", texto)
        self.assertIn("NO admite", texto)

    def test_tool_sin_argumentos_tambien(self):
        with self.assertRaises(Exception):
            self._llamar("describe_mxd", {"ruta": r"C:\no_existe.mxd", "extra": 1})

    def test_los_argumentos_validos_siguen_pasando(self):
        r = self._llamar("describe_mxd", {"ruta": r"C:\no_existe.mxd"})
        self.assertIn("no existe", str(r))

    def test_el_schema_lo_anuncia_en_todas(self):
        import asyncio
        tools = asyncio.run(servidor.mcp.list_tools())
        self.assertGreater(len(tools), 50)
        sin = [t.name for t in tools if t.inputSchema.get("additionalProperties") is not False]
        self.assertEqual(sin, [])


class TestExportMxdLoteValida(unittest.TestCase):
    """Lo que se rechaza ANTES de lanzar un solo python.exe."""

    def test_formato_desconocido(self):
        r = servidor.export_mxd_lote([r"C:\a.mxd"], formato="png")
        self.assertFalse(r["ok"])
        self.assertIn("formato", r["error"])

    def test_dpi_fuera_de_rango(self):
        r = servidor.export_mxd_lote([r"C:\a.mxd"], dpi=2000)
        self.assertFalse(r["ok"])
        self.assertIn("dpi", r["error"])

    def test_lista_vacia(self):
        r = servidor.export_mxd_lote([])
        self.assertFalse(r["ok"])

    def test_mxd_inexistente_no_tumba_el_lote(self):
        if servidor._python27_arcgis() is None:
            self.skipTest("sin Python 2.7 de ArcGIS")
        r = servidor.export_mxd_lote([r"C:\no_existe_1.mxd", r"C:\no_existe_2.mxd"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["resumen"]["fallidos"], 2)
        self.assertEqual(len(r["documentos"]), 2)

    def test_sin_sobrescribir_salta_y_lo_dice(self):
        if servidor._python27_arcgis() is None:
            self.skipTest("sin Python 2.7 de ArcGIS")
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        mxd = os.path.join(carpeta, "plano.mxd")
        jpg = os.path.join(carpeta, "plano.jpg")
        for ruta in (mxd, jpg):
            with open(ruta, "wb") as fh:
                fh.write(b"x")
        r = servidor.export_mxd_lote([mxd], sobrescribir=False)
        self.assertEqual(r["resumen"]["saltados"], 1)
        with open(jpg, "rb") as fh:
            self.assertEqual(fh.read(), b"x")  # intacto


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
        "export_mxd_lote": "un python.exe por .mxd: el proceso que ya exportó no vuelve a exportar",
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

    def test_acerca_de_cuenta_las_tools_sin_puente(self):
        """«Acerca de» suma ToolsSinPuente a los comandos del puente. Con export_mxd_lote
        pasaron de 2 a 3 y la ficha habría dicho 57 herramientas siendo 58."""
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "addin",
                            "ArcmapMcp.AddIn", "AboutFicha.cs")
        with open(ruta, encoding="utf-8-sig") as fh:
            m = re.search(r"const int ToolsSinPuente = (\d+);", fh.read())
        self.assertIsNotNone(m, "no se encuentra ToolsSinPuente en AboutFicha.cs")
        self.assertEqual(int(m.group(1)), len(self.SIN_PUENTE))

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


class TestArcMapLocal(unittest.TestCase):
    """Issue #1: ArcMap instalado fuera de Program Files (`D:\\软件安装\\Desktop10.8\\`).

    La detección miraba solo las carpetas por defecto y `describe_mxd` perdía, en
    silencio, su veredicto más útil. Ahora manda el registro, como en install.ps1.
    """

    def _con_registro(self, entradas, dirs_existentes=()):
        p1 = unittest.mock.patch.object(servidor, "_arcmap_por_registro", return_value=entradas)
        real_isdir = os.path.isdir
        p2 = unittest.mock.patch.object(
            servidor.os.path, "isdir",
            side_effect=lambda d: d in dirs_existentes or real_isdir(d))
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_ruta_no_estandar_se_detecta_por_registro(self):
        d = "D:\\软件安装\\Desktop10.8\\"
        self._con_registro([("10.8", d)], dirs_existentes=(d,))
        self.assertEqual(servidor._detectar_arcmap_local(), ("10.8", "registro"))

    def test_clave_huerfana_no_gana_a_la_instalada(self):
        viva = "C:\\ArcGIS\\Desktop10.5\\"
        self._con_registro([("10.8", "C:\\ya\\no\\existe\\"), ("10.5", viva)],
                           dirs_existentes=(viva,))
        self.assertEqual(servidor._detectar_arcmap_local()[0], "10.5")

    def test_orden_numerico_no_alfabetico(self):
        a, b = "C:\\a\\", "C:\\b\\"
        self._con_registro([("10.8", a), ("10.10", b)], dirs_existentes=(a, b))
        self.assertEqual(servidor._detectar_arcmap_local()[0], "10.10")

    def test_sin_deteccion_el_motivo_lo_dice(self):
        """Un null a secas se leía igual que 'se comparó y no salió nada'."""
        readme = os.path.join(RAIZ, "README.md")
        with unittest.mock.patch.object(servidor, "_detectar_arcmap_local",
                                        return_value=(None, "no se encontró ArcGIS Desktop")), \
                unittest.mock.patch.object(servidor, "_mxd_version_declarada",
                                           return_value=("10.8", None)):
            r = servidor.describe_mxd(readme)
        self.assertEqual(r["veredicto"], "indeterminado")
        self.assertIn("NO se ha podido detectar", r["motivo"])
        self.assertIn("no se encontró ArcGIS Desktop", r["version_arcmap_local_fuente"])

    def test_con_deteccion_hay_veredicto(self):
        readme = os.path.join(RAIZ, "README.md")
        with unittest.mock.patch.object(servidor, "_detectar_arcmap_local",
                                        return_value=("10.8", "registro")), \
                unittest.mock.patch.object(servidor, "_mxd_version_declarada",
                                           return_value=("10.8", None)):
            r = servidor.describe_mxd(readme)
        self.assertEqual(r["veredicto"], "compatible")
        self.assertEqual(r["version_arcmap_local"], "10.8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
