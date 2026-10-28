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
import ast
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


class TestVersionDelAddin(unittest.TestCase):
    """Con servidor nuevo y add-in viejo, lo nuevo se ignoraba y respondía ok:
    set_text_element(x=5) BORRABA el título (revisión del 2026-10-01). El servidor
    pregunta la versión con ping y se niega antes de enviar."""

    def cliente(self, *versiones):
        """Cliente cuyo puente contesta los ping con estas versiones, en orden."""
        cli = ArcMapClient(port=1)
        cli.enviados = []
        pings = list(versiones)

        def enviar(ctype, params=None, timeout=None):
            cli.enviados.append(ctype)
            if ctype == "ping":
                v = pings.pop(0) if len(pings) > 1 else pings[0]
                if v == "caido":
                    return {"ok": False, "estado": "puente_caido", "error": "x"}
                if v == "ocupado":
                    return {"ok": True, "result": {"estado": "ocupado"}}
                return {"ok": True, "result": {"addin": "arcmap-mcp %s (.NET)" % v}}
            return {"ok": True, "result": {}}
        cli._enviar = enviar
        return cli

    def test_add_in_viejo_no_recibe_el_parametro_nuevo(self):
        cli = self.cliente("2.16.0")
        r = cli.send("set_text_element", {"nombre": "Titulo", "x": 5})
        self.assertFalse(r["ok"])
        self.assertEqual(r["estado"], "addin_antiguo")
        self.assertIn("2.17.0", r["error"])
        self.assertIn("2.16.0", r["error"])
        self.assertIn("'x'", r["error"])
        self.assertNotIn("set_text_element", cli.enviados)

    def test_sin_nada_nuevo_no_pregunta_la_version(self):
        cli = self.cliente("2.16.0")
        self.assertTrue(cli.send("set_text_element", {"nombre": "Titulo", "texto": "Plano 1"})["ok"])
        self.assertEqual(cli.enviados, ["set_text_element"])

    def test_add_in_al_dia_recibe_el_parametro_y_la_version_se_guarda(self):
        cli = self.cliente("2.17.0")
        self.assertTrue(cli.send("set_text_element", {"nombre": "T", "x": 5})["ok"])
        self.assertTrue(cli.send("rename_layer", {"capa": "a", "nombre_nuevo": "b"})["ok"])
        self.assertEqual(cli.enviados, ["ping", "set_text_element", "rename_layer"])

    def test_tras_reinstalar_y_reiniciar_arcmap_no_se_niega(self):
        """La versión guardada es la de antes: se pregunta otra vez antes de negarse."""
        cli = self.cliente("2.16.0", "2.17.0")
        cli._version_del_addin()
        self.assertTrue(cli.send("set_legend", {"tamano": 8})["ok"])

    def test_el_puente_caido_olvida_la_version(self):
        cli = self.cliente("2.17.0")
        cli.send("ping")
        self.assertEqual(cli._version, (2, 17, 0))
        cli._enviar = lambda *a, **k: {"ok": False, "estado": "puente_caido", "error": "x"}
        cli.send("list_layers")
        self.assertIsNone(cli._version)

    def test_sin_version_conocida_el_comando_sigue(self):
        """Puente ocupado o caído: el comando dará su propio error, que ya es claro."""
        for estado in ("ocupado", "caido"):
            cli = self.cliente(estado)
            cli.send("open_mxd", {"ruta": "C:/a.mxd", "cambios_sin_guardar": "fallar"})
            self.assertIn("open_mxd", cli.enviados, estado)

    def test_un_valor_nuevo_de_un_parametro_viejo(self):
        cli = self.cliente("2.16.0")
        self.assertTrue(cli.send("set_raster_symbology", {"capa": "r", "modo": "clasificado"})["ok"])
        self.assertEqual(cli.send("set_raster_symbology", {"capa": "r", "modo": "rgb"})["estado"],
                         "addin_antiguo")

    def test_la_tabla_solo_nombra_parametros_que_existen(self):
        """Una errata en la tabla dejaría pasar justo lo que tenía que frenar."""
        import ast
        with open(os.path.join(RAIZ, "src", "arcmap_mcp_server.py"), encoding="utf-8") as fh:
            arbol = ast.parse(fh.read())
        firmas = {n.name: {a.arg for a in n.args.args} for n in arbol.body if isinstance(n, ast.FunctionDef)}
        for comando, claves in servidor._MINIMOS_ADDIN.items():
            self.assertIn(comando, firmas, comando)
            for clave in claves:
                if clave is not None:
                    nombre = clave[0] if isinstance(clave, tuple) else clave
                    self.assertIn(nombre, firmas[comando], "%s.%s" % (comando, nombre))


class TestRequestDemasiadoGrande(unittest.TestCase):
    """Un request de más de 1 MB no sale del cliente. Enviado, el add-in cerraba sin
    leer el resto y el cliente veía WinError 10054 en vez del motivo (2026-09-26)."""

    def test_no_se_envia_y_dice_por_que(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        s.settimeout(0.5)
        cli = ArcMapClient(host="127.0.0.1", port=s.getsockname()[1], timeout=2)
        r = cli.send("execute_code", {"code": "#" + "a" * (1024 * 1024)})
        self.assertFalse(r["ok"])
        self.assertIn("DEMASIADO GRANDE", r["error"])
        with self.assertRaises(socket.timeout, msg="el cliente llegó a conectar"):
            s.accept()

    def test_el_tope_es_el_del_add_in(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "McpServer.cs")
        with open(ruta, encoding="utf-8") as fh:
            cs = fh.read()
        self.assertIn("private const int MaxRequestBytes = 1024 * 1024;", cs)
        self.assertEqual(servidor.MAX_REQUEST_BYTES, 1024 * 1024)


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
        # Abrir puede incluir guardar el anterior: mismo tope que el guardado en el add-in.
        "open_mxd":            ((r"C:\x.mxd", "descartar"), "ARCMAP_SAVE_TIMEOUT"),
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


class TestAuditFolderLoQueSaleEnElPlano(unittest.TestCase):
    """El resumen tiene que dar el número que importa para el plano, no solo el total.

    En una serie de montes (bloque 02) había 2.335 capas rotas y solo 87 se dibujaban: con el total
    a secas, la auditoría señalaba 27 veces más problemas de los que había. Aquí el
    auditor Python 2.7 se sustituye por su salida, para probar solo el agregado.
    """

    def _auditar(self, salidas):
        import subprocess
        import tempfile
        carpeta = tempfile.mkdtemp(prefix="audit_plano_")
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        for nombre in salidas:
            with open(os.path.join(carpeta, nombre), "wb") as fh:
                fh.write(b"no es un mxd")

        self.abiertos = []

        def falso_run(args, **kw):
            nombre = os.path.basename(args[-1])
            self.abiertos.append(nombre)
            if salidas[nombre] == "TIMEOUT":
                raise subprocess.TimeoutExpired(args, kw.get("timeout"))
            datos = dict(salidas[nombre], ok=True)
            return subprocess.CompletedProcess(args, 0, json.dumps(datos).encode(), b"")

        with unittest.mock.patch.object(servidor, "_python27_arcgis",
                                        return_value=sys.executable), \
                unittest.mock.patch("subprocess.run", side_effect=falso_run):
            return servidor.audit_folder(carpeta, timeout_por_documento=5)

    def test_suma_por_separado_lo_que_se_ve(self):
        r = self._auditar({
            "a.mxd": {"num_capas": 40, "num_rotas": 30, "num_rotas_en_plano": 2,
                      "num_con_query": 5, "num_con_query_en_plano": 1,
                      "num_en_plano_desconocido": 0, "marcos": [], "capas": []},
            "b.mxd": {"num_capas": 10, "num_rotas": 4, "num_rotas_en_plano": 0,
                      "num_con_query": 3, "num_con_query_en_plano": 3,
                      "num_en_plano_desconocido": 1, "marcos": [], "capas": []},
        })
        self.assertTrue(r["ok"], r)
        res = r["resumen"]
        self.assertEqual((res["num_rotas"], res["num_rotas_en_plano"]), (34, 2))
        self.assertEqual((res["num_con_query"], res["num_con_query_en_plano"]), (8, 4))
        self.assertEqual(res["num_en_plano_desconocido"], 1)
        fila = r["documentos"][0]
        self.assertEqual(fila["num_rotas_en_plano"], 2)
        self.assertIn("marcos", fila)

    def test_sin_pasada_de_capas_no_inventa_ceros(self):
        """Con `con_capas=False` no se abrió nada: un `num_rotas_en_plano: 0` en el
        resumen se leería como «ninguna rota», que no se sabe."""
        r = servidor.audit_folder(RAIZ, con_capas=False)
        self.assertNotIn("num_rotas_en_plano", r["resumen"])


class TestAuditFolderCorteTrasTimeouts(unittest.TestCase):
    """Sin negativa por ArcMap abierto (descartada como causa el 2026-09-25), la
    protección es el corte: si todo agota el timeout, una carpeta de 200 planos no
    puede costar 200 timeouts. Pero un documento lento entre sanos no debe cortar."""

    SANO = {"num_capas": 1, "num_rotas": 0, "num_rotas_en_plano": 0, "num_con_query": 0,
            "num_con_query_en_plano": 0, "num_en_plano_desconocido": 0,
            "marcos": [], "capas": []}

    _auditar = TestAuditFolderLoQueSaleEnElPlano._auditar

    def test_dos_timeouts_seguidos_cortan_el_resto(self):
        r = self._auditar({"a.mxd": "TIMEOUT", "b.mxd": "TIMEOUT",
                           "c.mxd": self.SANO, "d.mxd": self.SANO})
        self.assertEqual(self.abiertos, ["a.mxd", "b.mxd"])
        self.assertEqual(r["resumen"]["timeout"], 2)
        self.assertEqual(r["resumen"]["sin_abrir"], 2)
        self.assertTrue(r["documentos"][3]["sin_abrir"])
        self.assertIn("CORTADA", r["aviso_capas"])

    def test_timeout_aislado_no_corta(self):
        r = self._auditar({"a.mxd": "TIMEOUT", "b.mxd": self.SANO,
                           "c.mxd": "TIMEOUT", "d.mxd": self.SANO})
        self.assertEqual(len(self.abiertos), 4)
        self.assertEqual(r["resumen"]["sin_abrir"], 0)
        self.assertEqual(r["resumen"]["con_capas"], 2)
        self.assertNotIn("aviso_capas", r)

    def test_arcmap_abierto_ya_no_impide_la_pasada_de_capas(self):
        """Control del cambio: la 2.14.0 miraba tasklist y omitía la pasada 2."""
        self.assertFalse(hasattr(servidor, "_arcmap_esta_abierto"))
        r = self._auditar({"a.mxd": self.SANO})
        self.assertEqual(r["resumen"]["con_capas"], 1)


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


class ClienteGuion(object):
    """Cliente de mentira que responde lo que diga su guion, en orden."""

    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = []

    def send(self, ctype, params=None, timeout=None):
        self.llamadas.append(ctype)
        return self.respuestas.pop(0) if len(self.respuestas) > 1 else self.respuestas[0]


class TestTablasOle(unittest.TestCase):
    """get_ole_tables. Lo que depende del formato real (el árbol del compound file y el Word
    97 binario) se validó contra los 8 planos con OLE de un expediente real, porque un fixture
    sintético no prueba el formato real. Aquí, la lógica que no depende de él."""

    def test_almacen_vivo_sale_del_numero_tras_el_clsid(self):
        cl = servidor._CLSID_MARCO_OLE
        capa = (b"xx" + cl + struct_pack("<IIII", 9111, 65537, 256, 7) + b"yy"
                + cl + struct_pack("<IIII", 1, 65537, 256, 1))
        with unittest.mock.patch.object(servidor, "_mxd_streams", return_value=({"PageLayout": capa}, None)):
            self.assertEqual(servidor._almacenes_vivos("x.mxd"), [7, 1])

    def test_las_copias_antiguas_no_salen_por_defecto(self):
        """Plano 04 real: la errata «INTRAESTRUCUTRAS» seguía en un almacén viejo del .mxd."""
        docx_bueno, docx_viejo = self._docx("INFRAESTRUCTURAS"), self._docx("INTRAESTRUCUTRAS")
        streams = {"Embedding 1/Package": docx_bueno, "Embedding 17/Package": docx_viejo}
        with unittest.mock.patch.object(servidor, "_cfb_streams_anidados", return_value=(streams, None)), \
                unittest.mock.patch.object(servidor, "_almacenes_vivos", return_value=[1]):
            r = servidor._leer_tablas_ole("x.mxd")["result"]
            todos = servidor._leer_tablas_ole("x.mxd", incluir_antiguos=True)["result"]
        self.assertEqual([o["almacen"] for o in r["objetos"]], ["Embedding 1"])
        self.assertEqual(r["objetos"][0]["filas"][0][0], "INFRAESTRUCTURAS")
        self.assertEqual(r["copias_antiguas_omitidas"], 1)
        self.assertEqual([o["en_el_layout"] for o in todos["objetos"]], [True, False])

    def test_los_almacenes_anidados_no_cuentan_como_copias_antiguas(self):
        doc = self._docx("A")
        streams = {"Embedding 1/Package": doc, "Embedding 2/Package": doc,
                   "Embedding 1/ObjectPool/_1/Package": doc}
        with unittest.mock.patch.object(servidor, "_cfb_streams_anidados", return_value=(streams, None)), \
                unittest.mock.patch.object(servidor, "_almacenes_vivos", return_value=[1]):
            r = servidor._leer_tablas_ole("x.mxd")["result"]
        self.assertEqual(r["copias_antiguas_omitidas"], 1)

    def test_sin_layout_legible_lo_avisa(self):
        with unittest.mock.patch.object(servidor, "_cfb_streams_anidados",
                                        return_value=({"Embedding 1/Package": self._docx("A")}, None)), \
                unittest.mock.patch.object(servidor, "_almacenes_vivos", return_value=None):
            r = servidor._leer_tablas_ole("x.mxd")["result"]
        self.assertIn("copias antiguas", r["aviso"])

    def test_filas_de_word97(self):
        texto = "Título\rCOL A\x07COL B\x07\x07a1\x07b1\x07\x07"
        self.assertEqual(servidor._filas_de_tabla_word(texto), [["COL A", "COL B"], ["a1", "b1"]])

    def test_sin_propiedades_de_parrafo_avisa_de_que_las_filas_son_deducidas(self):
        wd, tabla, _ = TestFilasWord97.streams("t1_vacia_final.doc")
        streams = {"Embedding 1/WordDocument": wd, "Embedding 1/1Table": tabla}
        with unittest.mock.patch.object(servidor, "_cfb_streams_anidados", return_value=(streams, None)), \
                unittest.mock.patch.object(servidor, "_almacenes_vivos", return_value=[1]), \
                unittest.mock.patch.object(servidor, "_parrafos_word97", side_effect=ValueError("x")):
            obj = servidor._leer_tablas_ole("x.mxd")["result"]["objetos"][0]
        self.assertIn("celdas vacías", obj["aviso"])
        self.assertIn("filas", obj)

    @staticmethod
    def _docx(primera_celda):
        import io
        import zipfile
        xml = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
               '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>%s</w:t></w:r></w:p></w:tc>'
               '<w:tc><w:p><w:r><w:t>Cantidad (t)</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
               '</w:body></w:document>' % primera_celda)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", xml)
        return buf.getvalue()


class TestFilasWord97(unittest.TestCase):
    """Filas de una tabla de Word 97-2003 por su estructura (propiedades de párrafo), no por el
    patrón \\x07\\x07 del texto, que confunde una celda vacía con un fin de fila.

    Los .doc son de Word de verdad (Word 16 guardando en formato 97-2003, scripts al lado) y
    no llevan datos de nadie. Con el método anterior, t1 daba [['', 'C', 'D']] y t2 [['', 'E']].
    En los .mxd reales el arreglo corrigió el TOTAL del Plano 09 de un expediente real (salía partido
    en dos filas) y una tabla de 10 columnas que salía desplazada una columna en 26 de 28 filas."""

    CARPETA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "word97")

    @classmethod
    def streams(cls, nombre):
        st, error = servidor._cfb_streams_anidados(os.path.join(cls.CARPETA, nombre), lambda p: True)
        assert error is None, error
        return st["WordDocument"], st["1Table"], st.get("Data", b"")

    def filas(self, nombre):
        return servidor._filas_word97(*self.streams(nombre))

    def test_celda_vacia_al_final_de_la_fila(self):
        self.assertEqual(self.filas("t1_vacia_final.doc"), [["A", ""], ["C", "D"]])

    def test_huecos_en_todas_las_posiciones_y_fila_vacia(self):
        self.assertEqual(self.filas("t2_huecos.doc"),
                         [["", "", "X"], ["", "", ""], ["B", "", ""], ["", "E", ""]])

    def test_una_sola_columna(self):
        """Antes no salía como tabla: devolvía [] y caía al modo texto."""
        self.assertEqual(self.filas("t3_una_columna.doc"), [["x"], [""], ["z"]])

    def test_el_texto_fuera_de_las_tablas_no_entra(self):
        """Título antes, párrafo entre dos tablas, nota después y una celda de dos párrafos."""
        self.assertEqual(self.filas("t4_texto_y_dos_tablas.doc"),
                         [["COL A", "COL B"], ["linea 1 linea 2", ""], ["", "q"], ["r", ""]])

    def test_celdas_combinadas(self):
        self.assertEqual(self.filas("t5_combinada.doc"), [["CABECERA"], ["p", "", "s"]])

    def test_tabla_ancha_con_las_propiedades_en_el_stream_data(self):
        """40 columnas: el fin de fila no cabe en la página y Word deja sprmPHugePapx."""
        filas = self.filas("t6_ancha.doc")
        self.assertEqual([len(f) for f in filas], [40, 40, 40])
        self.assertEqual(filas[0][:4], ["c1", "c2", "", "c4"])
        self.assertEqual((filas[1][0], filas[2][-1]), ("fila2", "ultima"))
        wd, tabla, _ = self.streams("t6_ancha.doc")
        with self.assertRaises(servidor.struct.error):   # sin Data: a la reserva, con aviso
            servidor._filas_word97(wd, tabla, b"")


def struct_pack(fmt, *valores):
    import struct
    return struct.pack(fmt, *valores)


class TestLectorCfbAcotado(unittest.TestCase):
    """El lector de compound files no puede gastar más memoria que el tamaño del fichero.

    Revisión adversarial del 2026-10-01: un .mxd fabricado de 2,5 KB con una cadena que se
    apunta a sí misma hizo que `_cfb_streams_anidados` llegara a 2,1 GB y devolviera «ok».
    Aquí, un compound file mínimo de verdad (cabecera, FAT, directorio, mini-FAT, mini
    stream) que se lee SIN mocks, y luego roto de cada forma en que una cadena puede ciclar."""

    FREE, END, FATSECT, DIFSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFC
    CONTENIDO = bytes(range(100))

    def _entrada(self, nombre, tipo, izq, der, hijo, inicio, size):
        n = (nombre + "\x00").encode("utf-16-le")
        e = bytearray(128)
        e[:len(n)] = n
        e[64:66] = struct_pack("<H", len(n))
        e[66], e[67] = tipo, 1
        e[68:80] = struct_pack("<III", izq, der, hijo)
        e[116:124] = struct_pack("<II", inicio, size)
        return bytes(e)

    def _cfb(self, fat_dir=None, fat_mini=None, mfat0=None, difat_ciclo=False):
        """Sectores: 0 FAT, 1 directorio, 2 mini-FAT, 3 mini stream, 4 bloque de DIFAT."""
        cab = bytearray(512)
        cab[:8] = servidor._FIRMA_CFB
        cab[24:34] = struct_pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
        cab[44:52] = struct_pack("<II", 1, 1)               # 1 sector de FAT; directorio en 1
        cab[56:68] = struct_pack("<III", 4096, 2, 1)        # corte mini; mini-FAT en 2
        cab[68:76] = struct_pack("<II", 4, 0xFFFFFFF0) if difat_ciclo else struct_pack("<II", self.END, 0)
        cab[76:512] = struct_pack("<109I", 0, *[self.FREE] * 108)
        fat = [self.FATSECT, self.END if fat_dir is None else fat_dir, self.END,
               self.END if fat_mini is None else fat_mini, self.DIFSECT] + [self.FREE] * 123
        directorio = (self._entrada("Root Entry", 5, self.FREE, self.FREE, 1, 3, 128)
                      + self._entrada("Embedding 1", 1, self.FREE, self.FREE, 2, 0, 0)
                      + self._entrada("WordDocument", 2, self.FREE, self.FREE, self.FREE, 0, 100)
                      + bytes(128))
        mfat = [1 if mfat0 is None else mfat0, self.END] + [self.FREE] * 126
        mini = self.CONTENIDO.ljust(512, b"\x00")
        difat = struct_pack("<128I", *([self.FREE] * 127 + [4]))  # se apunta a sí mismo
        datos = (bytes(cab) + struct_pack("<128I", *fat) + directorio + struct_pack("<128I", *mfat)
                 + mini + difat)
        import tempfile
        fd, ruta = tempfile.mkstemp(suffix=".mxd")
        with os.fdopen(fd, "wb") as fh:
            fh.write(datos)
        self.addCleanup(os.remove, ruta)
        return ruta

    def _leer_con_tope(self, ruta, tope_mb=20):
        import tracemalloc
        tracemalloc.start()
        try:
            streams, error = servidor._cfb_streams_anidados(ruta, lambda p: True)
            servidor._mxd_streams(ruta, ("WordDocument",))
            pico = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(pico, tope_mb * 2 ** 20, "el lector gastó %d MB" % (pico // 2 ** 20))
        return streams, error

    def test_fichero_sano_se_lee_recorriendo_el_arbol(self):
        streams, error = self._leer_con_tope(self._cfb())
        self.assertIsNone(error)
        self.assertEqual(streams, {"Embedding 1/WordDocument": self.CONTENIDO})

    def test_cadena_del_mini_stream_que_se_apunta_a_si_misma(self):
        """El caso del revisor (FAT[3] = 3): 2,1 GB con el lector anterior."""
        streams, _ = self._leer_con_tope(self._cfb(fat_mini=3))
        self.assertLessEqual(len(streams["Embedding 1/WordDocument"]), 100)

    def test_mini_fat_que_se_apunta_a_si_misma(self):
        streams, _ = self._leer_con_tope(self._cfb(mfat0=0))
        self.assertLessEqual(len(streams["Embedding 1/WordDocument"]), 100)

    def test_directorio_que_se_apunta_a_si_mismo(self):
        """Antes: 2 millones de vueltas y 8 millones de entradas."""
        streams, error = self._leer_con_tope(self._cfb(fat_dir=1))
        self.assertIsNone(error)
        self.assertEqual(streams, {"Embedding 1/WordDocument": self.CONTENIDO})

    def test_cadena_de_difat_que_se_apunta_a_si_misma(self):
        """Con 4.000 millones de bloques anunciados en la cabecera, la DIFAT crecía sin fin."""
        streams, error = self._leer_con_tope(self._cfb(difat_ciclo=True))
        self.assertIsNone(error)
        self.assertEqual(streams, {"Embedding 1/WordDocument": self.CONTENIDO})


@unittest.skipUnless(os.name == "nt", "la lectura de CPU por proceso es de Windows")
class TestCorteSinCpu(unittest.TestCase):
    """export_mxd_lote corta un documento cuya CPU no se mueve (2026-10-01: un arcpy estuvo
    10 min al 0 %). Procesos de verdad: uno que duerme y uno que calcula."""

    def _lanzar(self, codigo, corte, timeout=30):
        import time
        t = time.monotonic()
        # El python.exe de un venv es un LANZADOR que arranca el intérprete real como hijo y se
        # queda quieto: su CPU sale 0 aunque el hijo trabaje (medido). El lote vigila el
        # python.exe de ArcGIS, que es intérprete de verdad; aquí, el intérprete base.
        exe = getattr(sys, "_base_executable", sys.executable)
        rc, out, err, motivo = servidor._ejecutar_vigilando([exe, "-c", codigo],
                                                            timeout, corte, intervalo=0.5)
        return time.monotonic() - t, motivo, out

    def test_proceso_quieto_se_corta_antes_del_timeout(self):
        dur, motivo, _ = self._lanzar("import time; time.sleep(60)", corte=2)
        self.assertIsNotNone(motivo)
        self.assertIn("SIN PROGRESO", motivo)
        self.assertLess(dur, 15, "tenía que cortar a los ~2 s, no esperar al timeout")

    def test_proceso_que_trabaja_no_se_corta(self):
        codigo = ("import time\nt=time.time()\nwhile time.time()-t<4: sum(range(10000))\n"
                  "print('fin')")
        dur, motivo, out = self._lanzar(codigo, corte=2)
        self.assertIsNone(motivo)
        self.assertIn(b"fin", out)

    def test_sin_corte_solo_manda_el_timeout(self):
        dur, motivo, _ = self._lanzar("import time; time.sleep(60)", corte=0, timeout=3)
        self.assertIn("TIMEOUT", motivo)

    def test_vuelve_en_cuanto_acaba_el_proceso(self):
        """Con time.sleep(intervalo), un hijo de 1 s devolvía a los 5 s: 6 min de más en 150 planos."""
        import time
        exe = getattr(sys, "_base_executable", sys.executable)
        t = time.monotonic()
        servidor._ejecutar_vigilando([exe, "-c", "import time; time.sleep(1)"], 30, 120, intervalo=5.0)
        self.assertLess(time.monotonic() - t, 3.5)

    def test_con_un_lanzador_el_corte_se_desactiva_y_se_avisa(self):
        """La CPU de un lanzador (.cmd, python.exe de un venv) no se mueve: se cortaría todo."""
        with unittest.mock.patch.object(servidor, "_python27_arcgis", return_value=r"C:\x\arcpy.cmd"), \
                unittest.mock.patch.object(servidor.os.path, "isfile", side_effect=lambda p: p.endswith(".py")):
            r = servidor.export_mxd_lote([r"C:\no\existe.mxd"])
        self.assertIn("corte_sin_cpu desactivado", r["aviso_corte_sin_cpu"])


class TestOpenMxdEsperaAlDocumento(unittest.TestCase):
    """OpenDocument abre en diferido (medido en vivo 2026-10-01): justo después, el documento
    activo sigue siendo el anterior. El servidor espera a verlo abierto."""

    def _con(self, respuestas):
        g = ClienteGuion(respuestas)
        p = unittest.mock.patch.object(servidor, "_client", g)
        p.start()
        self.addCleanup(p.stop)
        q = unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None))
        q.start()
        self.addCleanup(q.stop)
        r = unittest.mock.patch("time.sleep")
        r.start()
        self.addCleanup(r.stop)
        return g

    def test_espera_hasta_ver_el_documento_nuevo(self):
        g = self._con([
            {"ok": True, "result": {"solicitado": r"C:\b.mxd", "anterior": r"C:\a.mxd"}},
            {"ok": True, "result": {"mxd": r"C:\a.mxd"}},
            {"ok": True, "result": {"mxd": r"C:\a.mxd"}},
            {"ok": True, "result": {"mxd": r"C:\B.mxd", "data_frames": ["Capas"]}},
        ])
        r = servidor.open_mxd(r"C:\b.mxd", "descartar")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["result"]["abierto"], r"C:\B.mxd")
        self.assertEqual(g.llamadas, ["open_mxd"] + ["get_arcmap_info"] * 3)

    def test_la_espera_total_no_pasa_de_un_save_timeout(self):
        """Antes cada llamada llevaba su SAVE_TIMEOUT y el bucle otro: hasta ~21 min."""
        reloj = [0.0]
        tope = int(servidor.SAVE_TIMEOUT)

        class Lento(ClienteGuion):
            def send(self, ctype, params=None, timeout=None):
                # guardar se come casi todo el plazo; cada sondeo agota el suyo
                reloj[0] += tope - 10 if ctype == "open_mxd" else timeout
                return super().send(ctype, params, timeout)

        g = Lento([{"ok": True, "result": {"solicitado": r"C:\b.mxd"}},
                   {"ok": True, "result": {"mxd": r"C:\a.mxd"}}])
        with unittest.mock.patch.object(servidor, "_client", g), \
                unittest.mock.patch("time.sleep"), \
                unittest.mock.patch("time.monotonic", side_effect=lambda: reloj[0]):
            r = servidor.open_mxd(r"C:\b.mxd", "descartar")
        self.assertFalse(r["ok"])
        self.assertLessEqual(reloj[0], tope + 1)

    def test_sin_eleccion_no_se_inventa_un_valor(self):
        # El parámetro es obligatorio en la firma: ni guardar ni descartar por defecto.
        import inspect
        p = inspect.signature(servidor.open_mxd).parameters["cambios_sin_guardar"]
        self.assertIs(p.default, inspect.Parameter.empty)


class TestExportEsperaAlDibujoDesdeFuera(unittest.TestCase):
    """E_PENDING se espera en el SERVIDOR, no dentro de ArcMap.

    El 2026-09-25 la espera del add-in (DoEvents en el hilo de ArcMap) colgó ArcMap tres
    veces al exportar justo después de cambiar etiquetas. Ahora el add-in devuelve
    `dibujando: ...` al momento y aquí se reintenta sin tocar el puente mientras tanto.
    """

    DIBUJANDO = {"ok": False, "error": "dibujando: ArcMap aún está dibujando el mapa"}
    BIEN = {"ok": True, "result": {"salida": "x.jpg"}}

    def setUp(self):
        self.original = servidor._client
        self.addCleanup(setattr, servidor, "_client", self.original)
        parche = unittest.mock.patch("time.sleep")
        self.sleep = parche.start()
        self.addCleanup(parche.stop)

    def test_reintenta_hasta_que_sale_y_lo_dice(self):
        servidor._client = ClienteGuion([self.DIBUJANDO, self.DIBUJANDO, self.BIEN])
        r = servidor.export_jpg(r"C:\x.jpg")
        self.assertTrue(r["ok"], r)
        self.assertEqual(servidor._client.llamadas, ["export_jpg"] * 3)
        self.assertIn("2 reintentos", r["result"]["aviso_dibujo"])

    def test_la_captura_del_lienzo_tambien_reintenta(self):
        """get_canvas_screenshot falló por E_PENDING en la regresión del 2026-09-26."""
        import base64
        png = {"ok": True, "result": {"imagen_b64": base64.b64encode(b"png").decode()}}
        servidor._client = ClienteGuion([self.DIBUJANDO, png])
        img = servidor.get_canvas_screenshot()
        self.assertEqual(servidor._client.llamadas, ["get_canvas_screenshot"] * 2)
        self.assertEqual(img.data, b"png")

    def test_sin_dibujando_no_hay_reintento_ni_aviso(self):
        servidor._client = ClienteGuion([dict(self.BIEN, result={"salida": "x.pdf"})])
        r = servidor.export_pdf(r"C:\x.pdf")
        self.assertEqual(servidor._client.llamadas, ["export_pdf"])
        self.assertNotIn("aviso_dibujo", r["result"])

    def test_otro_error_no_se_reintenta(self):
        """`busy:` tiene su propio tratamiento: reintentarlo aquí apilaría llamadas."""
        servidor._client = ClienteGuion([{"ok": False, "error": "busy: ArcMap lleva 3 s"}])
        servidor.export_view_png(r"C:\x.png")
        self.assertEqual(servidor._client.llamadas, ["export_view_png"])
        self.sleep.assert_not_called()

    def test_se_rinde_dentro_del_margen_y_nombra_la_variable(self):
        servidor._client = ClienteGuion([self.DIBUJANDO])
        reloj = iter(range(0, 10000, 3))
        with unittest.mock.patch("time.monotonic", side_effect=lambda: next(reloj)):
            r = servidor.export_jpg(r"C:\x.jpg")
        self.assertFalse(r["ok"])
        self.assertIn("ARCMAP_ESPERA_DIBUJO", r["error"])
        self.assertLessEqual(len(servidor._client.llamadas),
                             int(servidor.ESPERA_DIBUJO) // servidor._PAUSA_DIBUJO + 1)

    def test_el_prefijo_es_el_mismo_que_el_del_add_in(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Handlers", "ExportHandlers.cs")
        with open(ruta, encoding="utf-8") as fh:
            cs = fh.read()
        self.assertRegex(cs, r'const string Dibujando = "dibujando: ";')
        self.assertNotIn("DoEvents()", cs, "la espera volvió al hilo de ArcMap")


class TestTimeoutDelRunnerExplicaElBloqueoCom(unittest.TestCase):
    """El error de timeout del runner decía que con ArcMap abierto no se bloqueaba, y el
    2026-09-26 se midió lo contrario con un documento concreto. Que no vuelva."""

    def setUp(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Handlers", "PythonHandlers.cs")
        with open(ruta, encoding="utf-8") as fh:
            self.cs = fh.read()

    def test_no_niega_el_bloqueo_con_arcmap_abierto(self):
        self.assertNotIn("que ArcMap esté abierto o colgado no lo", self.cs)

    def test_el_aviso_va_en_el_error_de_timeout(self):
        self.assertIn("const string AvisoBloqueoCom", self.cs)
        self.assertIn("? AvisoBloqueoCom", self.cs)


class TestGeoprocesoNoArrastraMensajesAjenos(unittest.TestCase):
    """Los mensajes del geoprocesador son del PROCESO, no de cada GeoProcessorClass: un
    geoproceso que fallaba antes de arrancar (tool inexistente) salía con el error del
    anterior. Reproducido el 2026-09-26. El comportamiento se prueba en
    `regresion_sesion_viva.py`; aquí se vigila que el arreglo no se pierda."""

    def setUp(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Handlers",
                            "GeoprocessingHandlers.cs")
        with open(ruta, encoding="utf-8") as fh:
            cs = fh.read()
        ini = cs.index("public static JObject RunGeoprocessing(")
        self.run = cs[ini:cs.index("public static JObject CalculateGeometry(")]

    def test_vacia_los_mensajes_antes_de_ejecutar(self):
        self.assertIn("gp.ClearMessages();", self.run)
        self.assertLess(self.run.index("gp.ClearMessages();"), self.run.index("gp.Execute("))

    def test_comprueba_la_firma_antes_de_ejecutar(self):
        self.assertLess(self.run.index("ComprobarFirma("), self.run.index("gp.Execute("))

    def test_fuera_de_arcmap_tambien_la_comprueba_antes_del_runner(self):
        """Por arcpy.gp, dos parámetros de más tumban Python: la vía de fuera no puede
        dejar la comprobación al runner (2026-09-26)."""
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Handlers", "PythonHandlers.cs")
        with open(ruta, encoding="utf-8") as fh:
            cs = fh.read()
        fuera = cs[cs.index("public static JObject GeoprocesoFuera("):]
        self.assertLess(fuera.index("GeoprocessingHandlers.ComprobarFirma("),
                        fuera.index('RunJob("geoprocessing"'))


class TestGeoprocesoFueraDeArcMap(unittest.TestCase):
    """`fuera_de_arcmap` elige el comando; sin él, la llamada es la de siempre."""

    def setUp(self):
        self.original = servidor._client
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _llamar(self, **kw):
        enviados = []

        class Cliente(object):
            def send(self, ctype, params=None, timeout=None):
                enviados.append((ctype, params, timeout))
                return {"ok": True, "result": {}}
        servidor._client = Cliente()
        servidor.run_geoprocessing("management.GetCount", ["a.shp"], **kw)
        return enviados[0]

    def test_por_defecto_dentro_y_con_el_contrato_de_siempre(self):
        ctype, params, timeout = self._llamar()
        self.assertEqual(ctype, "run_geoprocessing")
        self.assertEqual(set(params), {"tool", "params", "resolver_capas"})
        self.assertEqual(timeout, servidor.GP_TIMEOUT)

    def test_fuera_va_al_comando_de_fondo_con_sus_opciones(self):
        ctype, params, timeout = self._llamar(fuera_de_arcmap=True, sobrescribir=True)
        self.assertEqual(ctype, "run_geoprocessing_fuera")
        self.assertIs(params["sobrescribir"], True)
        self.assertIs(params["anadir_al_mapa"], True)
        self.assertEqual(timeout, servidor.FONDO_TIMEOUT)

    def test_el_comando_existe_en_el_add_in(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "McpServer.cs")
        with open(ruta, encoding="utf-8") as fh:
            self.assertIn('{ "run_geoprocessing_fuera", Handlers.PythonHandlers.GeoprocesoFuera }',
                          fh.read())


class TestSetLabelsParametros(unittest.TestCase):
    """`set_labels` solo toca lo que se pasa: un None que llegara al add-in como
    null sería indistinguible de «quítalo». Por `call_tool`, como una llamada real,
    para que el schema acepte el color en sus dos formas."""

    def setUp(self):
        self.espia = ClienteEspia()
        self.original = servidor._client
        servidor._client = self.espia
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _llamar(self, args):
        import asyncio
        asyncio.run(servidor.mcp.call_tool("set_labels", args))
        return self.espia.llamadas[-1]

    def test_solo_viaja_lo_indicado(self):
        ll = self._llamar({"capa": "Cotos", "halo": 1})
        self.assertEqual(ll["ctype"], "set_labels")
        self.assertEqual(ll["params"], {"capa": "Cotos", "halo": 1})

    def test_color_como_lista_y_como_hex(self):
        ll = self._llamar({"capa": "Cotos", "color": [0, 107, 46], "color_halo": "#FFFFFF",
                           "halo": 0.8, "activar": False})
        self.assertEqual(ll["params"]["color"], [0, 107, 46])
        self.assertEqual(ll["params"]["color_halo"], "#FFFFFF")
        self.assertIs(ll["params"]["activar"], False)


class TestSimbologiaParametros(unittest.TestCase):
    """set_single_symbology y edit_symbol: solo viaja lo indicado, y `categoria`
    numérica (la clase 2 de unos rangos) llega como texto, que es como la compara el
    add-in contra valores y etiquetas."""

    def setUp(self):
        self.espia = ClienteEspia()
        self.original = servidor._client
        servidor._client = self.espia
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _llamar(self, tool, args):
        import asyncio
        asyncio.run(servidor.mcp.call_tool(tool, args))
        return self.espia.llamadas[-1]

    def test_single_solo_lo_indicado(self):
        ll = self._llamar("set_single_symbology", {"capa": "ENP", "color_relleno": "#A8D5A2",
                                                   "sin_relleno": False})
        self.assertEqual(ll["ctype"], "set_single_symbology")
        self.assertEqual(ll["params"], {"capa": "ENP", "color_relleno": "#A8D5A2",
                                        "sin_relleno": False})

    def test_edit_categoria_numerica_viaja_como_texto(self):
        ll = self._llamar("edit_symbol", {"capa": "Estratos", "categoria": 2,
                                          "color_relleno": [255, 0, 0]})
        self.assertEqual(ll["params"], {"capa": "Estratos", "categoria": "2",
                                        "color_relleno": [255, 0, 0]})

    def test_edit_sin_nada_es_solo_lectura(self):
        ll = self._llamar("edit_symbol", {"capa": "Estratos"})
        self.assertEqual(ll["params"], {"capa": "Estratos"})

    def test_edit_etiqueta_viaja_sola(self):
        # Cambiar solo el texto de leyenda de una clase: sin colores que lo acompañen.
        ll = self._llamar("edit_symbol", {"capa": "Medidas", "categoria": "M9",
                                          "etiqueta": "M9 · Seto existente"})
        self.assertEqual(ll["params"], {"capa": "Medidas", "categoria": "M9",
                                        "etiqueta": "M9 · Seto existente"})

    def test_texto_mover_sin_texto_no_lo_vacia(self):
        # Antes `texto` era obligatorio y el add-in hacía `texto ?? ""`: mover un título
        # sin pasar texto lo habría dejado en blanco. Sin texto, no viaja la clave.
        ll = self._llamar("set_text_element", {"buscar": "PLANO DE", "y": 2.5, "tamano": 14})
        self.assertEqual(ll["ctype"], "set_text_element")
        self.assertEqual(ll["params"], {"nombre": None, "buscar": "PLANO DE", "y": 2.5, "tamano": 14})

    def test_texto_solo_texto_como_siempre(self):
        ll = self._llamar("set_text_element", {"nombre": "Fecha", "texto": "Octubre 2026"})
        self.assertEqual(ll["params"], {"nombre": "Fecha", "buscar": None, "texto": "Octubre 2026"})

    def test_texto_vacio_explicito_si_viaja(self):
        ll = self._llamar("set_text_element", {"nombre": "Nota", "texto": ""})
        self.assertEqual(ll["params"]["texto"], "")

    def test_rename_layer(self):
        ll = self._llamar("rename_layer", {"capa": "Grupo/Unidades de restauracion",
                                           "nombre_nuevo": "Unidades de restauración"})
        self.assertEqual(ll["ctype"], "rename_layer")
        self.assertEqual(ll["params"], {"capa": "Grupo/Unidades de restauracion",
                                        "nombre_nuevo": "Unidades de restauración"})

    def test_leyenda_columna_nueva(self):
        ll = self._llamar("set_legend_item", {"capa": "Unidades de restauración", "columna_nueva": True})
        self.assertEqual(ll["ctype"], "set_legend_item")
        self.assertEqual(ll["params"], {"capa": "Unidades de restauración", "leyenda": None,
                                        "columna_nueva": True})

    def test_leyenda_quitar_y_mover_viajan(self):
        ll = self._llamar("set_legend_item", {"capa": "Badén", "quitar": True})
        self.assertEqual(ll["params"], {"capa": "Badén", "leyenda": None, "quitar": True})
        ll = self._llamar("set_legend_item", {"capa": "Turrilla", "despues_de": "Restauración"})
        self.assertEqual(ll["params"], {"capa": "Turrilla", "leyenda": None,
                                        "despues_de": "Restauración"})
        ll = self._llamar("set_legend_item", {"capa": "Turrilla", "posicion": 1})
        self.assertEqual(ll["params"], {"capa": "Turrilla", "leyenda": None, "posicion": 1})

    def test_set_legend_solo_viaja_lo_dado(self):
        ll = self._llamar("set_legend", {})
        self.assertEqual(ll["ctype"], "set_legend")
        self.assertEqual(ll["params"], {})
        ll = self._llamar("set_legend", {"tamano": 8, "recuadro": [34.5, 8.8, 41.27, 16.77]})
        self.assertEqual(ll["params"], {"tamano": 8, "recuadro": [34.5, 8.8, 41.27, 16.77]})

    def test_set_labels_maplex_y_por_valor_viajan(self):
        ll = self._llamar("set_labels", {"capa": "Zonificación", "parte_mayor": True,
                                         "sin_duplicados_m": 0, "repetir_cada_mm": 60})
        # El 0 viaja: apaga la opción, no es «no tocar».
        self.assertEqual(ll["params"], {"capa": "Zonificación", "parte_mayor": True,
                                        "sin_duplicados_m": 0, "repetir_cada_mm": 60})
        ll = self._llamar("set_labels", {"capa": "Zonificación", "por_valor": {"campo": "ZONA"},
                                         "copiar_de": "Montes"})
        self.assertEqual(ll["params"], {"capa": "Zonificación", "copiar_de": "Montes",
                                        "por_valor": {"campo": "ZONA"}})

    def test_valores_unicos_varios_campos_y_clases_viajan(self):
        ll = self._llamar("set_unique_values_symbology", {"capa": "Actuaciones",
                                                          "campos": ["ANIO_ACT", "Actuacion"], "delimitador": "|"})
        self.assertEqual(ll["params"], {"capa": "Actuaciones", "campo": None,
                                        "campos": ["ANIO_ACT", "Actuacion"], "delimitador": "|"})
        clases = [{"valores": ["0", "99"], "etiqueta": "Sin actuación"}]
        ll = self._llamar("set_unique_values_symbology", {"capa": "Actuaciones", "campo": "ANIO_ACT",
                                                          "clases": clases, "resto": "Otras"})
        self.assertEqual(ll["params"], {"capa": "Actuaciones", "campo": "ANIO_ACT",
                                        "clases": clases, "resto": "Otras"})

    def test_edit_symbol_anadir_y_encabezado_viajan(self):
        # Los valores viajan como texto (el renderer los guarda así) y "" es un encabezado vacío.
        ll = self._llamar("edit_symbol", {"capa": "Actuaciones", "categoria": "0", "anadir_valores": [99, "98"],
                                          "encabezado": ""})
        self.assertEqual(ll["params"], {"capa": "Actuaciones", "categoria": "0",
                                        "anadir_valores": ["99", "98"], "encabezado": ""})

    def test_raster_rgb_viaja_con_bandas(self):
        ll = self._llamar("set_raster_symbology", {"capa": "PNOA", "modo": "rgb", "bandas": [1, 2, 3]})
        self.assertEqual(ll["params"]["modo"], "rgb")
        self.assertEqual(ll["params"]["bandas"], [1, 2, 3])

    def test_edit_quitar_categoria(self):
        ll = self._llamar("edit_symbol", {"capa": "Fauna", "categoria": "Murciélagos", "quitar": True})
        self.assertEqual(ll["params"], {"capa": "Fauna", "categoria": "Murciélagos", "quitar": True})

    def test_list_style_sin_estilo_pide_los_cargados(self):
        # Sin `estilo` el add-in devuelve los estilos cargados: no debe viajar un
        # estilo null que lo confunda con «estilo vacío».
        ll = self._llamar("list_style_symbols", {})
        self.assertEqual(ll["ctype"], "list_style_symbols")
        self.assertEqual(ll["params"], {"limite": 200})

    def test_list_style_con_filtros(self):
        ll = self._llamar("list_style_symbols", {"estilo": r"C:\temp\empresa.style",
                                                 "clase": "relleno", "patron": "monte*"})
        self.assertEqual(ll["params"], {"estilo": r"C:\temp\empresa.style", "clase": "relleno",
                                        "patron": "monte*", "limite": 200})

    def test_apply_style_solo_lo_indicado(self):
        ll = self._llamar("apply_style_symbol", {"capa": "ENP", "estilo": "Usuario",
                                                 "nombre_simbolo": "Monte público"})
        self.assertEqual(ll["ctype"], "apply_style_symbol")
        self.assertEqual(ll["params"], {"capa": "ENP", "estilo": "Usuario",
                                        "nombre_simbolo": "Monte público"})

    def test_apply_style_solo_por_id(self):
        # El nombre no es único (ESRI.style trae dos «Verde» en la misma categoría).
        ll = self._llamar("apply_style_symbol", {"capa": "ENP", "estilo": "ESRI", "id_simbolo": 12})
        self.assertEqual(ll["params"], {"capa": "ENP", "estilo": "ESRI", "id_simbolo": 12})


class TestTocYServicios(unittest.TestCase):
    """move_layer y el WMS de add_layer: solo viaja lo indicado. Un `posicion` o un
    `visible` null que llegara al add-in cambiaría el defecto que decide él."""

    def setUp(self):
        self.espia = ClienteEspia()
        self.original = servidor._client
        servidor._client = self.espia
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _llamar(self, tool, args):
        import asyncio
        asyncio.run(servidor.mcp.call_tool(tool, args))
        return self.espia.llamadas[-1]

    def test_move_layer_minimo(self):
        ll = self._llamar("move_layer", {"capa": "Fondos"})
        self.assertEqual(ll["ctype"], "move_layer")
        self.assertEqual(ll["params"], {"capa": "Fondos"})

    def test_move_layer_con_referencia(self):
        ll = self._llamar("move_layer", {"capa": "Fondos", "referencia": "Ortos/PNOA",
                                         "posicion": "AFTER"})
        self.assertEqual(ll["params"], {"capa": "Fondos", "referencia": "Ortos/PNOA",
                                        "posicion": "AFTER"})

    def test_add_layer_sin_nada_nuevo_no_manda_visible(self):
        ll = self._llamar("add_layer", {"fuente": r"C:\a.shp"})
        self.assertNotIn("visible", ll["params"])
        self.assertNotIn("subcapas", ll["params"])

    def test_add_layer_wms_con_subcapas_apagado(self):
        url = "https://www.ign.es/wms-inspire/mapa-raster"
        ll = self._llamar("add_layer", {"fuente": url, "visible": False,
                                        "subcapas": ["Mapas raster del IGN"]})
        self.assertEqual(ll["params"]["fuente"], url)
        self.assertIs(ll["params"]["visible"], False)
        self.assertEqual(ll["params"]["subcapas"], ["Mapas raster del IGN"])


class TestGuardarVerificaRutasRelativas(unittest.TestCase):
    """save_mxd / save_mxd_as reabren lo guardado cuando se pide, o solas si el add-in
    predice capas que se van a perder; y lo que cuenta son las rotas NUEVAS, no las que
    ya estaban rotas en memoria."""

    def setUp(self):
        self.original = servidor._client
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _cliente(self, resultado):
        class Cliente(object):
            llamadas = []

            def send(self, ctype, params=None, timeout=None):
                self.llamadas.append({"ctype": ctype, "params": params or {}})
                return {"ok": True, "result": dict(resultado)}
        servidor._client = Cliente()
        return servidor._client

    def test_sin_riesgo_no_reabre_ni_devuelve_la_lista_interna(self):
        self._cliente({"guardado": True, "ruta": r"C:\a.mxd", "rotas_en_memoria": []})
        with unittest.mock.patch.object(servidor, "_verificar_guardado") as ver:
            r = servidor.save_mxd()
        ver.assert_not_called()
        self.assertNotIn("rotas_en_memoria", r["result"])
        self.assertNotIn("verificacion", r["result"])

    def test_con_riesgo_reabre_solo(self):
        cli = self._cliente({"guardado": True, "ruta": r"C:\a.mxd",
                             "capas_perderan_ruta": [{"ruta": "G/Capa"}],
                             "rotas_en_memoria": [{"data_frame": "Capas", "ruta": "X"}]})
        with unittest.mock.patch.object(servidor, "_verificar_guardado",
                                        return_value={"reabierto": True}) as ver:
            r = servidor.save_mxd()
        ver.assert_called_once_with(r"C:\a.mxd", [{"data_frame": "Capas", "ruta": "X"}])
        self.assertEqual(r["result"]["verificacion"], {"reabierto": True})
        self.assertEqual(cli.llamadas[-1]["params"], {})

    def test_verificar_false_lo_impide_aunque_haya_riesgo(self):
        self._cliente({"guardado": True, "ruta": r"C:\a.mxd",
                       "capas_perderan_ruta": [{"ruta": "G/Capa"}]})
        with unittest.mock.patch.object(servidor, "_verificar_guardado") as ver:
            servidor.save_mxd(verificar=False)
        ver.assert_not_called()

    def test_save_as_verifica_la_copia(self):
        cli = self._cliente({"guardado": True, "salida": r"C:\copia.mxd"})
        with unittest.mock.patch.object(servidor, "_verificar_guardado",
                                        return_value={"reabierto": True}) as ver:
            servidor.save_mxd_as(r"C:\copia.mxd", verificar=True)
        ver.assert_called_once_with(r"C:\copia.mxd", [])
        self.assertIs(cli.llamadas[-1]["params"]["verificar"], True)

    def test_solo_cuentan_las_rotas_nuevas(self):
        # El auditor da la ruta de grupo con "\" y el add-in con "/": son la misma capa.
        auditado = {"ok": True, "num_rotas": 3, "capas": [
            {"data_frame": "Capas", "nombre_largo": "Grupo\\Ya rota", "rota": True, "grupo": False},
            {"data_frame": "Capas", "nombre_largo": "Grupo\\Nueva", "rota": True, "grupo": False,
             "fuente": "\\p.shp"},
            {"data_frame": "Capas", "nombre_largo": "Grupo", "rota": True, "grupo": True},
            {"data_frame": "Capas", "nombre_largo": "Grupo\\Sana", "rota": False, "grupo": False},
        ]}
        proc = unittest.mock.Mock(stdout=json.dumps(auditado).encode("utf-8"))
        with unittest.mock.patch.object(servidor, "_python27_arcgis", return_value=sys.executable), \
                unittest.mock.patch("subprocess.run", return_value=proc):
            v = servidor._verificar_guardado(r"C:\a.mxd", [{"data_frame": "Capas", "ruta": "Grupo/Ya rota"}])
        self.assertTrue(v["reabierto"])
        self.assertEqual([c["ruta"] for c in v["rotas_nuevas"]], ["Grupo\\Nueva"])
        self.assertFalse(v["guardado_sin_perdidas"])


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


class TestAuditFolderExportacion(unittest.TestCase):
    """2026-10-06: un plano editado a las 10:47 no se re-exportó y el recuento de JPG
    cuadraba por casualidad. audit_folder compara fechas, sin abrir nada."""

    def setUp(self):
        import tempfile
        self.carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.carpeta, ignore_errors=True))
        self.t = 1_800_000_000

    def _fichero(self, nombre, segundos_antes):
        ruta = os.path.join(self.carpeta, nombre)
        with open(ruta, "wb") as fh:
            fh.write(b"x")
        os.utime(ruta, (self.t - segundos_antes, self.t - segundos_antes))

    def test_estados_y_resumen(self):
        self._fichero("al_dia.mxd", 0)
        self._fichero("al_dia.jpg", 30)          # exportado justo antes de guardar: margen
        self._fichero("viejo.mxd", 0)
        self._fichero("viejo.jpg", 3600)
        self._fichero("nada.mxd", 0)
        self._fichero("mixto.mxd", 0)
        self._fichero("mixto.jpg", 3600)
        self._fichero("mixto.pdf", -10)          # el PDF es posterior: manda el más nuevo
        r = servidor.audit_folder(self.carpeta, con_capas=False)
        filas = {f["fichero"]: f for f in r["documentos"]}
        self.assertEqual(filas["al_dia.mxd"]["exportacion"], "al_dia")
        self.assertEqual(filas["viejo.mxd"]["exportacion"], "desfasada")
        self.assertEqual(filas["viejo.mxd"]["exportacion_fichero"], "viejo.jpg")
        self.assertEqual(filas["nada.mxd"]["exportacion"], "sin_exportar")
        self.assertEqual(filas["mixto.mxd"]["exportacion"], "al_dia")
        self.assertEqual(filas["mixto.mxd"]["exportacion_fichero"], "mixto.pdf")
        self.assertEqual(r["resumen"]["exportacion"],
                         {"al_dia": 2, "desfasada": 1, "sin_exportar": 1})

    def test_fuera_del_margen_es_desfasada(self):
        self._fichero("p.mxd", 0)
        self._fichero("p.jpg", 61)
        self.assertEqual(servidor._estado_exportacion(os.path.join(self.carpeta, "p.mxd")),
                         {"exportacion": "desfasada", "exportacion_fichero": "p.jpg"})


class TestEditMxdLote(unittest.TestCase):
    """edit_mxd_lote (modo disco, ADR-010) con el ejecutable sustituido. Lo que se prueba aquí
    es lo que decide el servidor: qué se niega a tocar SIN lanzar nada, y cómo lee la salida.
    Lo que hace el ejecutable se prueba contra .mxd reales en tests/regresion_disco.py."""

    def _lote(self, ops=None, n_ole=0, primero_abierto=False, titulos=(), arcmap=False, salida=None, n_docs=1):
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        mxds = []
        for i in range(n_docs):
            mxds.append(os.path.join(carpeta, "plano%d.mxd" % i))
            with open(mxds[-1], "wb") as fh:
                fh.write(b"x")
        datos = salida or {"ok": True, "guardado": True, "operaciones": [], "verificacion": {"verificado": True}}
        lanzar = unittest.mock.Mock(return_value=(0, json.dumps(datos).encode("utf-8"), b"", None))
        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(n_ole, None)), \
                unittest.mock.patch.object(servidor, "_arcmap_en_marcha", return_value=arcmap), \
                unittest.mock.patch.object(servidor, "_mxd_abierto_en_arcmap", side_effect=lambda: (
                    os.path.normcase(os.path.abspath(mxds[0])) if primero_abierto else None, list(titulos))), \
                unittest.mock.patch.object(servidor, "_disco_exe", return_value="disco.exe"), \
                unittest.mock.patch.object(servidor, "_ejecutar_vigilando", lanzar):
            r = servidor.edit_mxd_lote(mxds, ops or [{"tool": "set_legend", "tamano": 8}])
        return r, lanzar, mxds

    def test_una_operacion_fuera_de_la_lista_no_lanza_nada(self):
        r, lanzar, _ = self._lote(ops=[{"tool": "export_jpg", "salida": "x"}])
        self.assertFalse(r["ok"])
        self.assertIn("export_jpg", r["error"])
        lanzar.assert_not_called()

    def test_un_mxd_con_marcos_ole_no_se_toca(self):
        """Guardarlo desde fuera de ArcMap lo corrompe: con ArcMap abierto o cerrado."""
        r, lanzar, _ = self._lote(n_ole=2)
        self.assertEqual(r["documentos"][0]["marcos_ole"], 2)
        self.assertIn("open_mxd", r["documentos"][0]["error"])
        lanzar.assert_not_called()

    def test_el_documento_abierto_en_arcmap_no_se_toca(self):
        r, lanzar, _ = self._lote(arcmap=True, primero_abierto=True)
        self.assertIn("documento abierto", r["documentos"][0]["error"])
        lanzar.assert_not_called()

    def test_sin_puente_se_compara_por_el_titulo_de_la_ventana(self):
        r, lanzar, _ = self._lote(arcmap=True, titulos=["plano0.mxd - ArcMap"])
        self.assertIn("documento abierto", r["documentos"][0]["error"])
        lanzar.assert_not_called()

    def test_guardado_y_verificado(self):
        r, lanzar, mxds = self._lote(n_docs=2)
        self.assertTrue(r["ok"])
        self.assertEqual(r["resumen"], {"guardados": 2, "fallidos": 0, "no_verificados": 0})
        self.assertEqual(lanzar.call_count, 2)

    def test_en_paralelo_lanza_todos_y_cuenta_igual(self):
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        mxds = []
        for i in range(4):
            mxds.append(os.path.join(carpeta, "p%d.mxd" % i))
            open(mxds[-1], "wb").close()
        ok = json.dumps({"ok": True, "guardado": True}).encode("utf-8")
        lanzar = unittest.mock.Mock(return_value=(0, ok, b"", None))
        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None)), \
                unittest.mock.patch.object(servidor, "_arcmap_en_marcha", return_value=False), \
                unittest.mock.patch.object(servidor, "_disco_exe", return_value="disco.exe"), \
                unittest.mock.patch.object(servidor, "_ejecutar_vigilando", lanzar):
            r = servidor.edit_mxd_lote(mxds, [{"tool": "set_legend", "tamano": 8}], en_paralelo=4)
            malo = servidor.edit_mxd_lote(mxds, [{"tool": "set_legend", "tamano": 8}], en_paralelo=9)
        self.assertEqual(r["resumen"]["guardados"], 4)
        self.assertEqual([d["mxd"] for d in r["documentos"]], mxds)
        self.assertEqual(lanzar.call_count, 4)
        self.assertIn("en_paralelo", malo["error"])

    def _lote_por_plano(self, n_docs=2, **kw):
        """Lanza el lote con el ejecutable sustituido y devuelve (respuesta, trabajos leídos):
        las operaciones que recibió cada plano, en el orden de lanzamiento."""
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        mxds = []
        for i in range(n_docs):
            mxds.append(os.path.join(carpeta, "monte%d.mxd" % i))
            open(mxds[-1], "wb").close()
        recibidos = []

        def lanzar(cmd, *_a, **_k):
            with open(cmd[1], encoding="utf-8") as fh:
                recibidos.append(json.load(fh))
            return (0, json.dumps({"ok": True, "guardado": True}).encode("utf-8"), b"", None)

        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None)), \
                unittest.mock.patch.object(servidor, "_arcmap_en_marcha", return_value=False), \
                unittest.mock.patch.object(servidor, "_disco_exe", return_value="disco.exe"), \
                unittest.mock.patch.object(servidor, "_ejecutar_vigilando", side_effect=lanzar):
            kw = {k: (v(mxds) if callable(v) else v) for k, v in kw.items()}
            r = servidor.edit_mxd_lote(**kw)
        if r.get("detalle_completo") and os.path.exists(r["detalle_completo"]):
            os.remove(r["detalle_completo"])
        return r, recibidos, mxds

    def test_variables_por_plano_se_sustituyen_en_cada_uno(self):
        """Serie de montes: cada monte con su MUP en el filtro (575 recetas sobre 386 MXD, 2026-10-10)."""
        q = {"tool": "set_definition_query", "capa": "Rodales", "query": "\"ID_MONTE\" = '{MUP}' AND x = '{otra}'"}
        r, recibidos, mxds = self._lote_por_plano(
            mxds=lambda m: m, operaciones=[q],
            variables_por_mxd=lambda m: {m[0]: {"MUP": "M0002"}, m[1]: {"MUP": "M0001"}})
        self.assertTrue(r["ok"], r)
        consultas = [t["operaciones"][0]["query"] for t in recibidos]
        # {otra} no es una variable declarada: se queda tal cual.
        self.assertEqual(sorted(consultas), ["\"ID_MONTE\" = 'M0001' AND x = '{otra}'",
                                             "\"ID_MONTE\" = 'M0002' AND x = '{otra}'"])

    def test_operaciones_por_plano_van_detras_de_las_comunes(self):
        r, recibidos, mxds = self._lote_por_plano(
            operaciones=[{"tool": "set_legend", "tamano": 8}],
            operaciones_por_mxd=lambda m: {m[0]: [{"tool": "remove_layer", "capa": "A"}],
                                           m[1]: [{"tool": "remove_layer", "capa": "B"}]})
        self.assertTrue(r["ok"], r)
        por_mxd = {t["mxd"]: [o.get("capa") or o["tool"] for o in t["operaciones"]] for t in recibidos}
        self.assertEqual(por_mxd[os.path.abspath(mxds[0])], ["set_legend", "A"])
        self.assertEqual(por_mxd[os.path.abspath(mxds[1])], ["set_legend", "B"])

    def test_variable_que_falta_en_un_plano_no_lanza_nada(self):
        r, recibidos, _ = self._lote_por_plano(
            mxds=lambda m: m, operaciones=[{"tool": "set_text_element", "buscar": "X", "texto": "{MONTE}"}],
            variables_por_mxd=lambda m: {m[0]: {"MONTE": "Coto Nueve"}})
        self.assertFalse(r["ok"])
        self.assertIn("{MONTE}", r["error"])
        self.assertEqual(recibidos, [])

    def test_clave_de_un_plano_que_no_esta_en_mxds_es_error(self):
        r, recibidos, _ = self._lote_por_plano(
            mxds=lambda m: m[:1], operaciones=[{"tool": "set_legend", "tamano": 8}],
            operaciones_por_mxd=lambda m: {m[1]: [{"tool": "set_legend", "tamano": 9}]})
        self.assertFalse(r["ok"])
        self.assertIn("no están en 'mxds'", r["error"])
        self.assertEqual(recibidos, [])

    def test_operacion_propia_fuera_del_lote_no_lanza_nada(self):
        r, recibidos, _ = self._lote_por_plano(
            operaciones_por_mxd=lambda m: {m[0]: [{"tool": "set_legend", "tamano": 8}],
                                           m[1]: [{"tool": "export_jpg"}]})
        self.assertFalse(r["ok"])
        self.assertIn("export_jpg", r["error"])
        self.assertEqual(recibidos, [])

    def test_plantilla_crea_destinos_y_manda_el_origen(self):
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        r, recibidos, mxds = self._lote_por_plano(
            mxds=lambda m: [os.path.join(carpeta, "nuevo%d.mxd" % i) for i in range(2)],
            plantilla=lambda m: m[0])
        self.assertTrue(r["ok"], r)
        self.assertEqual(len(recibidos), 2)
        self.assertTrue(all(t["origen"] == os.path.abspath(mxds[0]) and t["operaciones"] == [] for t in recibidos))

    def test_plantilla_no_pisa_un_destino_que_existe(self):
        r, recibidos, mxds = self._lote_por_plano(mxds=lambda m: [m[1]], plantilla=lambda m: m[0])
        self.assertFalse(r["ok"])
        self.assertIn("ya existe", r["documentos"][0]["error"])
        self.assertEqual(recibidos, [])

    def test_la_respuesta_va_resumida_y_lo_completo_a_un_fichero(self):
        """Con dos planos de 159 capas la respuesta pesó 120.000 caracteres y el cliente no la
        mostró (prueba del 2026-10-10): las lecturas de la verificación y el `orden` de la
        leyenda de cada operación, repetidos por documento."""
        largo = ["entrada %d" % i for i in range(200)]
        salida = {"ok": True, "guardado": True,
                  "operaciones": [{"ok": True, "tool": "set_legend_item",
                                   "result": {"cambiado": True, "orden": largo}}],
                  "verificacion": {"verificado": True, "diferencias": [],
                                   "lecturas": [{"lectura": "toc", "al_reabrir": {"capas": largo}},
                                                {"lectura": "leyendas", "al_reabrir": largo}]}}
        r, _, _ = self._lote(salida=salida, n_docs=2)
        self.addCleanup(lambda: os.path.exists(r.get("detalle_completo") or "") and os.remove(r["detalle_completo"]))
        self.assertTrue(r["ok"])
        doc = r["documentos"][0]
        self.assertEqual(doc["verificacion"]["lecturas"], ["toc", "leyendas"])
        self.assertTrue(doc["verificacion"]["verificado"])
        op = doc["operaciones"][0]
        self.assertTrue(op["result"]["cambiado"])
        self.assertIn("recortado", op["result"]["orden"])
        self.assertLess(len(json.dumps(r, ensure_ascii=False)), 3000)
        with open(r["detalle_completo"], encoding="utf-8") as fh:
            completo = json.load(fh)
        self.assertEqual(completo["documentos"][0]["operaciones"][0]["result"]["orden"], largo)

    def test_detalle_true_devuelve_lo_completo(self):
        largo = ["x%d" % i for i in range(200)]
        salida = {"ok": True, "guardado": True, "operaciones": [],
                  "verificacion": {"verificado": True, "lecturas": [{"lectura": "toc", "al_reabrir": largo}]}}
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        mxd = os.path.join(carpeta, "p.mxd")
        open(mxd, "wb").close()
        lanzar = unittest.mock.Mock(return_value=(0, json.dumps(salida).encode("utf-8"), b"", None))
        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None)), \
                unittest.mock.patch.object(servidor, "_arcmap_en_marcha", return_value=False), \
                unittest.mock.patch.object(servidor, "_disco_exe", return_value="disco.exe"), \
                unittest.mock.patch.object(servidor, "_ejecutar_vigilando", lanzar):
            r = servidor.edit_mxd_lote([mxd], [{"tool": "set_legend", "tamano": 8}], detalle=True)
        self.addCleanup(lambda: os.path.exists(r.get("detalle_completo") or "") and os.remove(r["detalle_completo"]))
        self.assertEqual(r["documentos"][0]["verificacion"]["lecturas"][0]["al_reabrir"], largo)

    def test_guardado_pero_no_verificado_no_es_ok(self):
        """Lo que se lee en el mismo proceso no prueba lo guardado: manda la lectura al reabrir."""
        r, _, _ = self._lote(salida={"ok": False, "guardado": True, "error": "no coinciden",
                                     "verificacion": {"verificado": False, "diferencias": [{"lectura": "toc"}]}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["resumen"]["no_verificados"], 1)
        self.assertFalse(r["documentos"][0]["verificacion"]["verificado"])

    def test_el_ejecutable_sale_del_esriaddin_instalado(self):
        """Quien instala con doble clic solo tiene el zip: se extrae a una caché propia."""
        import tempfile
        import zipfile
        casa = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(casa, ignore_errors=True))
        carpeta = os.path.join(casa, "Documents", "ArcGIS", "AddIns", "Desktop10.5", servidor._ADDIN_ID)
        os.makedirs(carpeta)
        with zipfile.ZipFile(os.path.join(carpeta, "arcmap-mcp.esriaddin"), "w") as z:
            z.writestr("config.xml", "<x/>")
            z.writestr("Install/arcmap-mcp-disco.exe", b"MZ")
            z.writestr("Install/ArcmapMcp.AddIn.dll", b"MZ")
        entorno = {"USERPROFILE": casa, "LOCALAPPDATA": os.path.join(casa, "local")}
        with unittest.mock.patch.dict(os.environ, entorno), \
                unittest.mock.patch.dict(os.environ, {"ARCMAP_DISCO_EXE": ""}):
            exe = servidor._disco_exe()
        self.assertTrue(exe.startswith(os.path.join(casa, "local")), exe)
        self.assertTrue(os.path.isfile(exe))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(exe), "ArcmapMcp.AddIn.dll")))


class _LoteFalso(object):
    """export_mxd_lote con el lanzamiento y la detección de OLE/ArcMap sustituidos."""

    def _lote(self, n_ole, arcmap_abierto, n_docs=1,
              respuesta=b'{"ok": false, "error": "falso"}', efecto=None, **kw):
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        mxds = []
        for i in range(n_docs):
            mxds.append(os.path.join(carpeta, "plano%d.mxd" % i))
            with open(mxds[-1], "wb") as fh:
                fh.write(b"x")
        lanzar = unittest.mock.Mock(return_value=(0, respuesta, b"", None), side_effect=efecto)
        tasklist = unittest.mock.Mock(return_value=arcmap_abierto)
        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(n_ole, None)), \
                unittest.mock.patch.object(servidor, "_arcmap_en_marcha", tasklist), \
                unittest.mock.patch.object(servidor, "_python27_arcgis", return_value="py27.exe"), \
                unittest.mock.patch.object(servidor, "_ejecutar_vigilando", lanzar):
            r = servidor.export_mxd_lote(mxds, **kw)
        return r, lanzar, tasklist


class TestExportMxdLoteMarcosOle(_LoteFalso, unittest.TestCase):
    """2026-10-06: con ArcMap abierto, el plano con marco OLE colgó al arcpy de fuera y
    arrastró a los procesos de otros .mxd. Con ArcMap abierto ni se lanza; cerrado, sí."""

    def test_con_arcmap_abierto_no_se_lanza_y_dice_por_que(self):
        r, lanzar, _ = self._lote(2, True)
        lanzar.assert_not_called()
        doc = r["documentos"][0]
        self.assertEqual(doc["marcos_ole"], 2)
        self.assertIn("ArcMap cerrado", doc["error"])
        self.assertEqual(r["resumen"]["fallidos"], 1)
        self.assertFalse(r["ok"])

    def test_con_arcmap_cerrado_se_exporta_como_siempre(self):
        r, lanzar, _ = self._lote(2, False)
        lanzar.assert_called_once()
        self.assertNotIn("marcos_ole", r["documentos"][0])

    def test_sin_ole_ni_se_pregunta_por_arcmap(self):
        _, lanzar, tasklist = self._lote(0, True)
        tasklist.assert_not_called()
        lanzar.assert_called_once()

    def test_arcmap_se_consulta_una_vez_por_lote(self):
        r, lanzar, tasklist = self._lote(1, True, n_docs=3)
        self.assertEqual(tasklist.call_count, 1)
        self.assertEqual(r["resumen"]["fallidos"], 3)
        lanzar.assert_not_called()


class TestExportMxdLoteCapasRotas(_LoteFalso, unittest.TestCase):
    """2026-10-06: cuatro planos con «ok» y sin cantones. Las capas rotas que salen en
    el plano llegan a la fila y a un aviso arriba, también si el «ok» del hijo no se
    sostiene (la comprobación del fichero reescribe la respuesta)."""

    ROTAS = (b'{"ok": true, "bytes": 5, "capas_rotas": [{"nombre": "Cantones", '
             b'"data_frame": "PLANO", "fuente": "C:\\\\d\\\\c.shp"}]}')

    def test_las_rotas_llegan_a_la_fila_y_al_aviso(self):
        r, _, _ = self._lote(0, False, respuesta=self.ROTAS)
        doc = r["documentos"][0]
        self.assertEqual(doc["capas_rotas"][0]["nombre"], "Cantones")
        self.assertEqual(r["resumen"]["con_capas_rotas"], 1)
        self.assertIn("capas_rotas", r["aviso"])

    def test_sin_rotas_no_hay_aviso(self):
        r, _, _ = self._lote(0, False, respuesta=b'{"ok": true, "capas_rotas": []}')
        self.assertNotIn("aviso", r)
        self.assertNotIn("capas_rotas", r["documentos"][0])

    def test_modo_desconocido_se_rechaza(self):
        r, lanzar, _ = self._lote(0, False, si_capas_rotas="avisar")
        self.assertFalse(r["ok"])
        self.assertIn("si_capas_rotas", r["error"])
        lanzar.assert_not_called()

    def test_el_modo_llega_al_trabajo(self):
        vistos = []

        def leer_trabajo(args, *a, **k):
            # Durante la llamada: al terminar el lote, el trabajo ya está borrado.
            with open(args[2], encoding="utf-8") as f:
                vistos.append(json.load(f)["si_capas_rotas"])
            return 0, b'{"ok": false, "error": "x"}', b"", None
        self._lote(0, False, efecto=leer_trabajo, si_capas_rotas="FALLAR")
        self.assertEqual(vistos, ["fallar"])


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
        "get_ole_tables": "lee las tablas OLE del fichero; activarlas en ArcMap bloquea el puente",
        "edit_mxd_lote": "modo disco (ADR-010): un arcmap-mcp-disco.exe por .mxd, con ArcMap sin ese documento",
        "compare_mxd": "lee los dos .mxd con arcmap-mcp-disco.exe en modo inventario; sin sesión viva",
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
            # _exportar es _client.send con el reintento de E_PENDING (los tres export y,
            # desde la 2.16.0, la captura del lienzo).
            if "_client.send" in cuerpo or "_exportar(" in cuerpo:
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


class TestMarcosOle(unittest.TestCase):
    """Un .mxd con marcos OLE deja colgado al arcpy de fuera mientras ArcMap está abierto
    (medido 2026-09-26). Se detectan sin abrirlo por el CLSID de la clase en el layout."""

    def _streams(self, **streams):
        p = unittest.mock.patch.object(servidor, "_mxd_streams",
                                       return_value=({k.replace("_", " "): v for k, v in streams.items()}, None))
        p.start()
        self.addCleanup(p.stop)

    def test_cuenta_en_el_pagelayout(self):
        h = servidor._CLSID_MARCO_OLE
        self._streams(PageLayout=b"xx" + h + b"yy" + h)
        self.assertEqual(servidor._mxd_marcos_ole("x.mxd"), (2, None))

    def test_si_hay_mx_document_manda_ese(self):
        """Lo que guarda ArcObjects desde fuera va a `Mx Document` y deja el `PageLayout`
        antiguo: sin esta regla, la copia ya limpia seguía contando dos."""
        h = servidor._CLSID_MARCO_OLE
        self._streams(PageLayout=h + h, Mx_Document=b"limpio")
        self.assertEqual(servidor._mxd_marcos_ole("x.mxd"), (0, None))

    def test_la_verificacion_del_guardado_no_se_lanza(self):
        r = {"ok": True, "result": {"ruta": "C:\\x.mxd", "marcos_ole": [{"donde": "layout"}]}}
        with unittest.mock.patch.object(servidor, "_verificar_guardado") as verificar:
            servidor._tras_guardar(r, "ruta", True)
        verificar.assert_not_called()
        self.assertFalse(r["result"]["verificacion"]["reabierto"])
        self.assertIn("marco(s) OLE", r["result"]["verificacion"]["motivo"])

    def _con_mxd(self, nombre, n_ole):
        import tempfile
        d = tempfile.mkdtemp()
        ruta = os.path.join(d, nombre)
        open(ruta, "wb").close()
        p = unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(n_ole, None))
        p.start()
        self.addCleanup(p.stop)
        espia = ClienteEspia()
        q = unittest.mock.patch.object(servidor, "_client", espia)
        q.start()
        self.addCleanup(q.stop)
        return ruta, espia

    def test_execute_arcpy_no_abre_un_mxd_con_ole(self):
        """2026-10-01: guardar con arcpy de fuera 8 planos con tablas OLE corrompió uno. Por
        el MCP (ArcMap abierto) ni llegaría a guardar: se cuelga al cargarlo."""
        ruta, espia = self._con_mxd("Plano 04.mxd", 2)
        for code in ('m = arcpy.mapping.MapDocument(r"%s")\nm.save()' % ruta,
                     "m = arcpy.mapping.MapDocument('%s')" % ruta.replace("\\", "\\\\")):
            r = servidor.execute_arcpy(code)
            self.assertFalse(r["ok"], code)
            self.assertTrue(r["error"].startswith("marcos_ole: "))
            self.assertIn("Plano 04.mxd", r["error"])
            # 2026-10-06: se pidió solo LEER las capas y el error no decía cómo.
            self.assertIn("audit_folder", r["error"])
            self.assertIn("open_mxd", r["error"])
        self.assertEqual(espia.llamadas, [], "no debe llegar al puente")

    def test_execute_arcpy_sin_ole_sigue_igual(self):
        ruta, espia = self._con_mxd("limpio.mxd", 0)
        servidor.execute_arcpy('m = arcpy.mapping.MapDocument(r"%s")' % ruta)
        self.assertEqual(len(espia.llamadas), 1)

    def test_una_ruta_mxd_sin_mapdocument_no_frena(self):
        # Un print o una lista de rutas no abre nada: frenar ahí sería ruido.
        ruta, espia = self._con_mxd("Plano 04.mxd", 2)
        servidor.execute_arcpy('RESULT = r"%s"' % ruta)
        self.assertEqual(len(espia.llamadas), 1)

    def test_el_clsid_es_el_del_add_in(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Handlers", "MarcosOle.cs")
        with open(ruta, encoding="utf-8") as fh:
            cs = fh.read()
        self.assertIn('internal const string Prefijo = "marcos_ole: ";', cs)
        import uuid
        self.assertEqual(servidor._CLSID_MARCO_OLE,
                         uuid.UUID("f6705e85-523b-11d1-86e7-0000f8751720").bytes_le)

    def test_las_copias_fuera_de_arcmap_miran_antes_los_marcos(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "Handlers", "PythonHandlers.cs")
        with open(ruta, encoding="utf-8") as fh:
            cs = fh.read()
        con_copia = cs[cs.index("private static JObject RunJobConSnapshot("):]
        self.assertLess(con_copia.index("MarcosOle.ComprobarAntesDeCopia("),
                        con_copia.index("Snapshot(serializarSesion)"))


class TestNoEjecutarDentroDeUnDibujado(unittest.TestCase):
    """ArcMap bombea mensajes mientras dibuja y los comandos entraban en mitad de un
    dibujado: 72 veces en una pasada de la regresión (2026-09-26). Hipótesis de la caída
    en MaplexAnnotation.dll. El dispatcher aplaza el comando si hay un dibujado en curso."""

    def setUp(self):
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.AddIn", "StaDispatcher.cs")
        with open(ruta, encoding="utf-8") as fh:
            self.cs = fh.read()

    def test_si_esta_dibujando_no_ejecuta_el_handler(self):
        invoke = self.cs[self.cs.index("public static JObject Invoke("):self.cs.index("private static JObject Ejecutar(")]
        self.assertIn("VigiaDibujo.Asegurar();", invoke)
        self.assertLess(invoke.index("if (VigiaDibujo.Dibujando && !forzar)"),
                        invoke.index("return Ejecutar(handler"))

    def test_la_espera_tiene_tope(self):
        """Un DisplayFinished que no llega no puede dejar el puente parado."""
        self.assertIn("AplazamientoMaximo = TimeSpan.FromSeconds(20)", self.cs)


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


class TestSubprocesosSinVentana(unittest.TestCase):
    """Claude Desktop y Cowork arrancan el servidor sin consola, y entonces cada
    subproceso sin CREATE_NO_WINDOW abre una ventana que roba el foco (2026-09-29: un
    lote de MXD en un equipo compartido). Se mira el código fuente, no se ejecuta: el
    defecto es un argumento que falta, y vuelve en cuanto alguien añade una llamada."""

    LANZADORES = {"run", "Popen", "call", "check_call", "check_output"}

    def _llamadas(self):
        with open(os.path.join(RAIZ, "src", "arcmap_mcp_server.py"), encoding="utf-8") as f:
            arbol = ast.parse(f.read())
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                    and isinstance(nodo.func.value, ast.Name)
                    and nodo.func.value.id == "subprocess"
                    and nodo.func.attr in self.LANZADORES):
                yield nodo

    def test_todas_las_llamadas_van_sin_ventana(self):
        llamadas = list(self._llamadas())
        # Si baja de 4 es que el detector ya no ve las llamadas, no que estén bien:
        # un test que no encuentra nada pasa siempre.
        self.assertGreaterEqual(len(llamadas), 4)
        for nodo in llamadas:
            # Solo vale SIN_VENTANA: un `creationflags=` suelto quita la ventana pero
            # deja al hijo heredando el stdin del servidor (ver test de abajo).
            con_flag = any(
                k.arg is None and isinstance(k.value, ast.Name) and k.value.id == "SIN_VENTANA"
                for k in nodo.keywords)
            self.assertTrue(con_flag, "subprocess.%s sin SIN_VENTANA en la línea %d"
                            % (nodo.func.attr, nodo.lineno))

    @unittest.skipUnless(os.name == "nt", "CREATE_NO_WINDOW solo existe en Windows")
    def test_flag_es_create_no_window_y_no_detached(self):
        flags = servidor.SIN_VENTANA["creationflags"]
        self.assertTrue(flags & 0x08000000)
        self.assertFalse(flags & 0x00000008, "DETACHED_PROCESS hace que cada nieto abra ventana")

    def test_el_hijo_no_hereda_el_stdin(self):
        import subprocess
        self.assertIs(servidor.SIN_VENTANA.get("stdin"), subprocess.DEVNULL)


# Padre que imita al servidor MCP: un hilo bloqueado leyendo su stdin (FastMCP
# esperando el siguiente mensaje JSON-RPC) mientras lanza un hijo trivial.
_PADRE_CON_LECTURA_PENDIENTE = r"""
import os, sys, threading, time
sys.path.insert(0, sys.argv[1])
import arcmap_mcp_server as S
if sys.argv[2] == "heredando":
    S.SIN_VENTANA = {k: v for k, v in S.SIN_VENTANA.items() if k != "stdin"}
threading.Thread(target=lambda: sys.stdin.buffer.read(1), daemon=True).start()
time.sleep(0.5)
t = time.monotonic()
rc, out, err, corte = S._ejecutar_vigilando([sys.executable, "-c", "print(1)"], 6, 0, 0.2)
print("%s|%.1f" % (corte or "ok", time.monotonic() - t), flush=True)
os._exit(0)
"""


@unittest.skipUnless(os.name == "nt", "el bloqueo es de los pipes síncronos de Windows")
class TestHijoNoSeBloqueaConStdinPendiente(unittest.TestCase):
    """2026-10-06: export_mxd_lote cortaba 13 de 16 planos con 0,0 s de CPU porque el
    hijo heredaba el stdin del servidor, que FastMCP tiene siempre con una lectura
    pendiente, y se bloqueaba en su arranque. Con control: heredando, el mismo montaje
    se cuelga; si no se cuelga, el test no estaría probando nada."""

    def _lanzar(self, modo):
        import subprocess
        p = subprocess.Popen([sys.executable, "-c", _PADRE_CON_LECTURA_PENDIENTE,
                              os.path.join(RAIZ, "src"), modo],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        # readline y no communicate(): communicate cierra el stdin, eso termina la
        # lectura pendiente del padre y el control dejaría de reproducir nada. Si el
        # padre casca, su stdout se cierra y readline devuelve b"".
        try:
            out = p.stdout.readline()
        finally:
            p.kill()
            p.wait()
            p.stdin.close()
        return out.decode("utf-8", "replace").strip()

    def test_con_devnull_el_hijo_arranca(self):
        estado, segundos = self._lanzar("normal").split("|")
        self.assertEqual(estado, "ok")
        self.assertLess(float(segundos), 5)

    def test_control_heredando_el_hijo_se_cuelga(self):
        estado, _ = self._lanzar("heredando").split("|")
        self.assertTrue(estado.startswith("TIMEOUT"), estado)


class TestHuecosAuditoria(unittest.TestCase):
    """Huecos 11-15 de la auditoría de scripts a mano (2026-10-10): los parámetros nuevos
    viajan solo si se dan, y lo que el lote admite coincide con lo que el ejecutable sabe hacer."""

    def setUp(self):
        self.espia = ClienteEspia()
        self.original = servidor._client
        servidor._client = self.espia
        self.addCleanup(setattr, servidor, "_client", self.original)

    def _llamar(self, tool, args):
        import asyncio
        asyncio.run(servidor.mcp.call_tool(tool, args))
        return self.espia.llamadas[-1]

    def test_definition_query_anadir_solo_si_se_pide(self):
        self.assertNotIn("anadir", self._llamar("set_definition_query", {"capa": "R", "query": "a = 1"})["params"])
        ll = self._llamar("set_definition_query", {"capa": "R", "query": "a = 1", "anadir": True})
        self.assertIs(ll["params"]["anadir"], True)

    def test_add_layer_si_existe_solo_si_no_es_el_de_siempre(self):
        self.assertNotIn("si_existe", self._llamar("add_layer", {"fuente": "C:/a.shp"})["params"])
        ll = self._llamar("add_layer", {"fuente": "C:/a.shp", "si_existe": "omitir"})
        self.assertEqual(ll["params"]["si_existe"], "omitir")

    def test_simbolo_trama_false_viaja(self):
        """trama=False quita la trama: si se filtrara como None, no haría nada sin decirlo."""
        ll = self._llamar("edit_symbol", {"capa": "P", "trama": False})
        self.assertIs(ll["params"]["trama"], False)
        ll = self._llamar("set_single_symbology", {"capa": "L", "patron_linea": [9, 4.5]})
        self.assertEqual(ll["params"]["patron_linea"], [9, 4.5])
        self.assertNotIn("trama", ll["params"])

    def test_leyenda_formato_viaja_y_negrita_false_tambien(self):
        ll = self._llamar("set_legend", {"fuente": "Arial", "negrita_etiquetas": False, "parche": [16, 9],
                                         "separaciones": {"entre_entradas": 3}})
        self.assertEqual(ll["params"], {"fuente": "Arial", "negrita_etiquetas": False, "parche": [16, 9],
                                        "separaciones": {"entre_entradas": 3}})

    def test_copiar_elementos_de_un_origen_con_ole_no_se_envia(self):
        """Abrir desde fuera un .mxd con marcos OLE, con ArcMap abierto, se queda colgado."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mxd", delete=False) as fh:
            fh.write(b"x")
        self.addCleanup(os.remove, fh.name)
        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(1, None)):
            r = servidor.copy_layout_elements(fh.name, nombres=["FEADER"])
        self.assertFalse(r["ok"])
        self.assertIn("OLE", r["error"])
        self.assertEqual(self.espia.llamadas, [])
        with unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None)):
            servidor.copy_layout_elements(fh.name, nombres=["FEADER"])
        self.assertEqual(self.espia.llamadas[-1]["params"], {"origen": fh.name, "nombres": ["FEADER"]})

    def test_las_operaciones_del_lote_son_las_del_ejecutable(self):
        """Una operación en OPERACIONES_LOTE que Program.cs no conoce falla en cada plano; una
        que Program.cs conoce y el servidor no, no se puede pedir."""
        ruta = os.path.join(RAIZ, "addin", "ArcmapMcp.Disco", "Program.cs")
        with open(ruta, encoding="utf-8-sig") as fh:
            cs = fh.read()
        bloque = cs.split("Operaciones =", 1)[1].split("};", 1)[0]
        self.assertEqual(sorted(re.findall(r'\{ "([a-z_]+)",', bloque)), sorted(servidor.OPERACIONES_LOTE))


class TestCompararInventarios(unittest.TestCase):
    """compare_mxd: la diferencia entre dos inventarios del modo disco. La forma de los
    inventarios es la que da arcmap-mcp-disco.exe (probada contra planos reales,
    donde dos hojas de la serie dan solo escala, título y nº de hoja)."""

    def _inv(self):
        return {
            "data_frame": "PLANO", "escala": 40000.0,
            "capas": [
                {"ruta": "G", "visible": True, "grupo": True},
                {"ruta": "G/Rodales", "visible": True, "valida": True, "fuente": "C:/d|rodales",
                 "definition_query": "\"MUP\" = 'M0001'", "etiquetas": True,
                 "simbologia": {"capa": "G/Rodales", "renderer": "valores_unicos", "encabezado": "",
                                "clases": [{"categoria": "a", "etiqueta": "A", "color_relleno": "#FF0000"},
                                           {"categoria": "b", "etiqueta": "B", "color_relleno": "#00FF00"}]},
                 "etiquetado": {"capa": "G/Rodales", "maplex": True,
                                "clases": [{"clase": "Predeterminado", "expresion": "[COD]", "tamano": 8.0}]}},
                {"ruta": "Ortofoto", "visible": True, "valida": True, "fuente": "C:/o|pnoa.ecw",
                 "simbologia": {"error": "No se puede implementar el método o la operación."}},
            ],
            "textos": [
                {"nombre": "", "grupo": "(grupo sin nombre)", "texto": "2 DE 4", "centro": [39.29, 17.82], "tamano": 8.0},
                {"nombre": "Titulo", "grupo": None, "texto": "HIC Sierra Ejemplo", "centro": [37.9, 20.4], "tamano": 12.0},
            ],
            "leyendas": [{"leyenda": "Legend", "data_frame": "PLANO", "caja": [34.6, 9.0, 41.0, 16.6],
                          "entradas": [{"capa": "G/Rodales", "nombre": False}, {"capa": "Ortofoto", "nombre": False}],
                          "formato": {"fuentes_nombres": ["Arial"], "parches": ["16x9"]}}],
        }

    def test_un_plano_contra_si_mismo_no_da_nada(self):
        """Control: si esto diera algo, cualquier diferencia de abajo sería ruido."""
        self.assertEqual(servidor._comparar_inventarios(self._inv(), self._inv()), {})
        self.assertEqual(servidor._contar_diferencias({}), 0)

    def test_lo_tipico_de_una_hoja_de_la_serie(self):
        import copy
        plano = copy.deepcopy(self._inv())
        plano["escala"] = 45000.0
        plano["textos"][0]["texto"] = "3 DE 4"
        plano["textos"][0]["centro"] = [39.31, 17.80]  # se ha movido un pelo al cambiar el texto
        plano["textos"][1]["centro"] = [10.0, 10.0]     # con nombre: se empareja aunque se mueva
        dif = servidor._comparar_inventarios(plano, self._inv())
        self.assertEqual(dif["general"], {"escala": [45000.0, 40000.0]})
        cambiados = dif["textos"]["cambiados"]
        self.assertEqual(len(cambiados), 2)
        nhoja = next(c for c in cambiados if c["en_referencia"] == "2 DE 4")
        self.assertEqual(nhoja["cambios"], {"texto": ["3 DE 4", "2 DE 4"]})  # sin «centro»: se movió < 0,05
        titulo = next(c for c in cambiados if c["en_referencia"] == "HIC Sierra Ejemplo")
        self.assertEqual(titulo["cambios"], {"centro": [[10.0, 10.0], [37.9, 20.4]]})
        self.assertNotIn("capas", dif)
        self.assertEqual(servidor._contar_diferencias(dif), 3)

    def test_capas_clases_etiquetas_y_leyenda(self):
        import copy
        plano = copy.deepcopy(self._inv())
        rodales = plano["capas"][1]
        rodales["definition_query"] = "\"MUP\" = 'M0003'"
        rodales["simbologia"]["clases"][1]["color_relleno"] = "#0000FF"
        rodales["simbologia"]["clases"].append({"categoria": "c", "etiqueta": "C", "color_relleno": "#000000"})
        rodales["etiquetado"]["clases"][0]["tamano"] = 9.0
        plano["capas"].append({"ruta": "Nueva", "visible": False})
        plano["leyendas"][0]["entradas"].reverse()
        plano["leyendas"][0]["formato"]["parches"] = ["30x12"]
        dif = servidor._comparar_inventarios(plano, self._inv())
        self.assertEqual(dif["capas"]["solo_en_plano"], ["Nueva"])
        self.assertEqual(dif["capas"]["cambios"]["G/Rodales"],
                         {"definition_query": ["\"MUP\" = 'M0003'", "\"MUP\" = 'M0001'"]})
        sim = dif["simbologia"]["G/Rodales"]
        self.assertEqual(sim["clases_solo_en_plano"], ["c"])
        self.assertEqual(sim["clases"], {"b": {"color_relleno": ["#0000FF", "#00FF00"]}})
        self.assertEqual(dif["etiquetado"]["G/Rodales"]["por_clase"]["Predeterminado"], {"tamano": [9.0, 8.0]})
        ley = dif["leyendas"]["leyenda_1"]
        self.assertIn("orden", ley)
        self.assertEqual(ley["formato"], {"parches": [["30x12"], ["16x9"]]})
        self.assertNotIn("Ortofoto", dif.get("simbologia", {}))  # el mismo error de lectura en los dos: igual

    def test_un_texto_sin_pareja_sale_en_su_lado(self):
        import copy
        plano = copy.deepcopy(self._inv())
        plano["textos"][0]["centro"] = [5.0, 5.0]  # sin nombre y lejos: ya no se empareja
        dif = servidor._comparar_inventarios(plano, self._inv(), ambitos=["textos"])
        self.assertEqual([t["texto"] for t in dif["textos"]["solo_en_plano"]], ["2 DE 4"])
        self.assertEqual([t["texto"] for t in dif["textos"]["solo_en_referencia"]], ["2 DE 4"])

    def test_ambito_desconocido_es_error_sin_lanzar_nada(self):
        with unittest.mock.patch.object(servidor, "_inventario_mxd") as inv:
            r = servidor.compare_mxd("a.mxd", "b.mxd", ambitos=["capas", "colores"])
        self.assertFalse(r["ok"])
        self.assertIn("colores", r["error"])
        inv.assert_not_called()


class TestAuditFolderDetalle(unittest.TestCase):
    """audit_folder(detalle=True): el inventario de cada plano entra resumido en la respuesta y
    entero en el fichero; sin el ejecutable, lo dice y la auditoría sigue."""

    def _carpeta(self):
        import tempfile
        carpeta = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(carpeta, ignore_errors=True))
        for n in ("a.mxd", "b.mxd"):
            with open(os.path.join(carpeta, n), "wb") as fh:
                fh.write(b"x")
        return carpeta

    def test_resumen_en_la_respuesta_e_inventario_en_el_fichero(self):
        inv = {"capas": [{"ruta": "G", "grupo": True}, {"ruta": "G/R", "etiquetas": True,
                                                         "simbologia": {"renderer": "valores_unicos"}}],
               "textos": [{"texto": "1 DE 4"}], "leyendas": [{"entradas": [{"capa": "G/R"}]}]}
        with unittest.mock.patch.object(servidor, "_disco_exe", return_value="disco.exe"), \
                unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None)), \
                unittest.mock.patch.object(servidor, "_inventario_mxd",
                                           return_value={"ok": True, "inventario": inv}) as leer:
            r = servidor.audit_folder(self._carpeta(), con_capas=False, detalle=True)
        self.assertEqual(leer.call_count, 2)
        d = r["documentos"][0]["detalle"]
        self.assertEqual((d["capas"], d["grupos"], d["clasificadas"], d["etiquetadas"], d["textos"]),
                         (1, 1, 1, 1, 1))
        with open(r["detalle_completo"], encoding="utf-8") as fh:
            completo = json.load(fh)
        self.addCleanup(os.remove, r["detalle_completo"])
        self.assertEqual(len(completo["inventarios"]), 2)

    def _auditar_con_capas(self, guardar=None):
        """Pasada 2 de mentira (dos capas por plano) con detalle=True. `guardar` sustituye a
        _guardar_detalle_lote (para simular que el fichero no se puede escribir)."""
        import subprocess
        capas = [{"nombre": "A", "en_plano": True}, {"nombre": "B", "en_plano": False}]

        def falso_run(args, **kw):
            datos = {"ok": True, "num_capas": 2, "num_rotas": 0, "num_rotas_en_plano": 0, "num_con_query": 0,
                     "num_con_query_en_plano": 0, "num_en_plano_desconocido": 0, "marcos": [], "capas": capas}
            return subprocess.CompletedProcess(args, 0, json.dumps(datos).encode(), b"")

        parches = [unittest.mock.patch.object(servidor, "_python27_arcgis", return_value=sys.executable),
                   unittest.mock.patch("subprocess.run", side_effect=falso_run),
                   unittest.mock.patch.object(servidor, "_disco_exe", return_value="disco.exe"),
                   unittest.mock.patch.object(servidor, "_mxd_marcos_ole", return_value=(0, None)),
                   unittest.mock.patch.object(servidor, "_inventario_mxd",
                                              return_value={"ok": True, "inventario": {"capas": []}})]
        if guardar is not None:
            parches.append(unittest.mock.patch.object(servidor, "_guardar_detalle_lote", side_effect=guardar))
        for p in parches:
            p.start()
            self.addCleanup(p.stop)
        return servidor.audit_folder(self._carpeta(), detalle=True)

    def test_con_detalle_la_lista_de_capas_va_al_fichero(self):
        """Con 6 planos reales la respuesta pesaba 106 KB y el cliente no la mostraba: el peso era
        la lista `capas` de la pasada 2 (prueba desde el cliente, 2026-10-10)."""
        r = self._auditar_con_capas()
        fila = r["documentos"][0]
        self.assertNotIn("capas", fila)
        self.assertEqual(fila["capas_en_detalle"], 2)
        self.assertEqual(fila["num_capas"], 2)  # los recuentos se quedan
        self.addCleanup(os.remove, r["detalle_completo"])
        with open(r["detalle_completo"], encoding="utf-8") as fh:
            completo = json.load(fh)
        self.assertEqual([c["nombre"] for c in completo["documentos"][0]["capas"]], ["A", "B"])
        self.assertEqual(len(completo["inventarios"]), 2)

    def test_sin_fichero_las_capas_se_quedan_en_la_respuesta(self):
        r = self._auditar_con_capas(guardar=lambda *a, **k: None)
        self.assertEqual(len(r["documentos"][0]["capas"]), 2)
        self.assertNotIn("detalle_completo", r)
        self.assertIn("aviso_detalle_completo", r)

    def test_sin_detalle_las_capas_siguen_en_la_respuesta(self):
        import subprocess

        def falso_run(args, **kw):
            datos = {"ok": True, "marcos": [], "capas": [{"nombre": "A"}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(datos).encode(), b"")

        with unittest.mock.patch.object(servidor, "_python27_arcgis", return_value=sys.executable), \
                unittest.mock.patch("subprocess.run", side_effect=falso_run):
            r = servidor.audit_folder(self._carpeta())
        self.assertEqual(r["documentos"][0]["capas"], [{"nombre": "A"}])
        self.assertNotIn("detalle_completo", r)

    def test_sin_ejecutable_lo_dice_y_sigue(self):
        with unittest.mock.patch.object(servidor, "_disco_exe", return_value=None), \
                unittest.mock.patch.object(servidor, "_inventario_mxd") as leer:
            r = servidor.audit_folder(self._carpeta(), con_capas=False, detalle=True)
        self.assertTrue(r["ok"])
        self.assertIn("arcmap-mcp-disco.exe", r["aviso_detalle"])
        self.assertNotIn("detalle", r["documentos"][0])
        leer.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
