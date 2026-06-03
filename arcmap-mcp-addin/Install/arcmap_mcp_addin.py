# -*- coding: utf-8 -*-
"""
arcmap_mcp_addin.py  ──  Python Add-In de ArcMap 10.5.

Crea la barra "arcmap-mcp" con cuatro botones: Iniciar / Detener / Estado / Acerca de.
La lógica del socket vive en arcmap_bridge.py (en src/ del repo); este módulo
solo lo importa y llama a start()/stop(). Importar NO arranca el puente (el
auto-arranque del puente está guardado con __name__ == "__main__").

Estado de un vistazo: mientras el puente está activo, el botón Iniciar se muestra
"pulsado" (checked) y Detener habilitado; parado, al revés. ArcMap re-consulta
esas propiedades al refrescar la barra.

"Acerca de" escribe una ficha HTML (con la identidad visual de pedralcg.dev) en
%TEMP% y la abre lanzando un proceso cmd DESACOPLADO (DETACHED_PROCESS): ArcMap
solo arranca el proceso y sigue, sin esperar -> no bloquea el hilo (a diferencia
de Tkinter.mainloop, webbrowser.open y os.startfile, que cuelgan ArcMap en este
equipo). Cada paso se registra en %TEMP%\\arcmap_mcp_about.log.
"""

import os
import sys
import tempfile
import traceback

import pythonaddins

# Ruta donde vive arcmap_bridge.py (en src/). Default C:\mcp\arcmap-mcp; se puede
# sobreescribir con la variable de entorno ARCMAP_MCP_DIR (p. ej. en desarrollo,
# si el repo vive en otra unidad). Define la env var ANTES de abrir ArcMap.
REPO_DIR = os.environ.get("ARCMAP_MCP_DIR", r"C:\mcp\arcmap-mcp")
SRC_DIR = os.path.join(REPO_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.append(SRC_DIR)

# --- Datos del autor (se muestran en "Acerca de") ---
AUTOR = "Pedro Alcoba Gomez"
TAGLINE = "Del dato ambiental al producto digital"
WEB = "https://pedralcg.dev"
EMAIL = "pedro@pedralcg.dev"
GITHUB = "https://github.com/pedralcg"
LINKEDIN = "https://www.linkedin.com/in/pedro-alcoba-gomez/"
LUGAR = "Bullas, Murcia"

ABOUT_TEXT = (
    "arcmap-mcp - Puente MCP para ArcMap\n\n"
    "Autor:    %s\n"
    "Web:      %s\n"
    "Email:    %s\n"
    "GitHub:   %s\n"
    "LinkedIn: %s\n\n"
    "%s"
) % (AUTOR, WEB, EMAIL, GITHUB, LINKEDIN, LUGAR)

# SVG inline (trazo/relleno verde bosque #1e5631) para las tarjetas de enlace.
_SVG_WEB = (u'<svg viewBox="0 0 24 24" width="22" height="22" fill="none" '
            u'stroke="#1e5631" stroke-width="2"><circle cx="12" cy="12" r="9"/>'
            u'<path d="M3 12h18M12 3c2.5 2.7 2.5 15.3 0 18M12 3c-2.5 2.7-2.5 15.3 0 18"/></svg>')
_SVG_MAIL = (u'<svg viewBox="0 0 24 24" width="22" height="22" fill="none" '
             u'stroke="#1e5631" stroke-width="2"><rect x="3" y="5" width="18" height="14" rx="2"/>'
             u'<path d="M3 7l9 6 9-6"/></svg>')
_SVG_GH = (u'<svg viewBox="0 0 24 24" width="22" height="22" fill="#1e5631">'
           u'<path d="M12 2C6.5 2 2 6.6 2 12.3c0 4.5 2.9 8.3 6.8 9.7.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.4-3.4-1.4-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.6 2.4 1.1 3 .9.1-.7.3-1.1.6-1.4-2.2-.3-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.3 9.3 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.3 4.7-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10.3 10.3 0 0 0 22 12.3C22 6.6 17.5 2 12 2z"/></svg>')
_SVG_LI = (u'<svg viewBox="0 0 24 24" width="22" height="22" fill="#1e5631">'
           u'<path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zM9 9h3.8v1.7h.1c.5-.9 1.8-1.9 3.6-1.9 3.9 0 4.6 2.5 4.6 5.8V21h-4v-5.3c0-1.3 0-2.9-1.8-2.9s-2 1.4-2 2.8V21H9z"/></svg>')

_ABOUT_HTML = u"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>arcmap-mcp &middot; pedralcg.dev</title>
<style>
 :root{{--green:#1e5631;--green2:#2e7d46;--amber:#d98324;--soft:#d7e8d0;
        --cream:#faf3e6;--bg:#eef3ea;--text:#3a4a3c;--muted:#6f8071;--border:#e0e8dc;}}
 *{{box-sizing:border-box;}}
 body{{margin:0;min-height:100vh;background:var(--bg);color:var(--text);
      font-family:'Segoe UI',system-ui,-apple-system,Arial,sans-serif;
      display:flex;align-items:center;justify-content:center;padding:32px;}}
 .wrap{{width:100%;max-width:560px;}}
 .topbar{{height:5px;border-radius:6px 6px 0 0;background:var(--green);
          border-bottom:0;}}
 .topbar i{{display:block;height:5px;border-radius:6px 6px 0 0;
            background:linear-gradient(90deg,var(--green) 0%,var(--green) 70%,var(--amber) 100%);}}
 .brand{{font-weight:700;font-size:15px;color:var(--green);padding:14px 4px 0;}}
 .brand b{{color:var(--amber);}}
 .hero{{margin-top:10px;border:1px solid var(--border);border-radius:18px;padding:30px 32px;
        background:linear-gradient(135deg,#e9f1e4 0%,#f2f1e6 55%,var(--cream) 100%);
        box-shadow:0 14px 40px rgba(20,60,30,.10);}}
 .hero h1{{margin:0;font-size:30px;font-weight:800;color:var(--green);letter-spacing:-.5px;}}
 .hero .kicker{{margin:6px 0 0;font-size:13px;font-weight:700;letter-spacing:1.2px;
                text-transform:uppercase;color:var(--amber);}}
 .hero p.desc{{margin:16px 0 0;font-size:15px;line-height:1.6;color:var(--text);}}
 .hero p.desc b{{color:var(--green);}}
 .quote{{margin:20px 0 4px;padding:4px 0 4px 14px;border-left:3px solid var(--green2);
         font-style:italic;color:var(--muted);font-size:14px;}}
 .cards{{margin-top:18px;display:grid;grid-template-columns:1fr 1fr;gap:14px;}}
 a.card{{display:flex;flex-direction:column;gap:8px;text-decoration:none;background:#fff;
         border:1px solid var(--border);border-radius:14px;padding:16px 16px 14px;
         transition:transform .12s,box-shadow .12s,border-color .12s;}}
 a.card:hover{{transform:translateY(-3px);box-shadow:0 12px 26px rgba(20,60,30,.12);
               border-color:var(--green2);}}
 .ic{{width:40px;height:40px;border-radius:11px;background:var(--soft);
      display:flex;align-items:center;justify-content:center;}}
 a.card .t{{font-size:15px;font-weight:700;color:var(--green);}}
 a.card .s{{font-size:12px;color:var(--muted);word-break:break-all;}}
 .foot{{margin-top:18px;text-align:center;font-size:12px;color:var(--muted);}}
 .foot b{{color:var(--amber);}}
</style></head><body>
 <div class="wrap">
  <div class="topbar"><i></i></div>
  <div class="brand">pedralcg<b>.dev</b></div>
  <div class="hero">
   <h1>arcmap-mcp</h1>
   <p class="kicker">Puente MCP para ArcMap</p>
   <p class="desc">Conduce la sesi&oacute;n viva de ArcMap desde un agente IA.
      Software libre creado por <b>{autor}</b> &middot; {lugar}.</p>
   <p class="quote">{tagline}</p>
   <div class="cards">
    <a class="card" href="{web}"><span class="ic">{svg_web}</span>
       <span class="t">Web</span><span class="s">pedralcg.dev</span></a>
    <a class="card" href="mailto:{email}"><span class="ic">{svg_mail}</span>
       <span class="t">Email</span><span class="s">{email}</span></a>
    <a class="card" href="{github}"><span class="ic">{svg_gh}</span>
       <span class="t">GitHub</span><span class="s">@pedralcg</span></a>
    <a class="card" href="{linkedin}"><span class="ic">{svg_li}</span>
       <span class="t">LinkedIn</span><span class="s">pedro-alcoba-gomez</span></a>
   </div>
   <p class="foot">arcmap-mcp &middot; <b>pedralcg.dev</b></p>
  </div>
 </div>
</body></html>"""

LOG = os.path.join(tempfile.gettempdir(), "arcmap_mcp_about.log")

# Import diferido y tolerante: si falla, lo reportamos al pulsar el botón.
_bridge = {"mod": None, "error": None}
# Referencias a las instancias de botón que crea ArcMap, para reflejar estado.
_buttons = {}


def _set_running(run):
    """Refleja el estado del puente en la apariencia de los botones."""
    b = _buttons.get("start")
    if b is not None:
        b.checked = bool(run)
    b = _buttons.get("stop")
    if b is not None:
        b.enabled = bool(run)


def _log(s):
    try:
        f = open(LOG, "a")
        try:
            f.write(s + "\n")
            f.flush()
        finally:
            f.close()
    except Exception:
        pass


def _get_bridge():
    if _bridge["mod"] is not None:
        return _bridge["mod"]
    try:
        import arcmap_bridge
        # Por si el repo se editó en caliente durante la sesión de ArcMap:
        reload(arcmap_bridge)
        _bridge["mod"] = arcmap_bridge
        _bridge["error"] = None
    except Exception:
        _bridge["error"] = traceback.format_exc()
    return _bridge["mod"]


def _msg(texto, titulo="arcmap-mcp"):
    pythonaddins.MessageBox(texto, titulo, 0)


def _open_detached(ruta):
    """Abre 'ruta' lanzando un cmd desacoplado que NO bloquea ArcMap."""
    import subprocess
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    subprocess.Popen('start "" "%s"' % ruta, shell=True, close_fds=True,
                     creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)


def _show_about():
    _log("=== onClick Acerca de ===")
    try:
        html = _ABOUT_HTML.format(autor=AUTOR, tagline=TAGLINE, lugar=LUGAR,
                                  web=WEB, email=EMAIL, github=GITHUB, linkedin=LINKEDIN,
                                  svg_web=_SVG_WEB, svg_mail=_SVG_MAIL,
                                  svg_gh=_SVG_GH, svg_li=_SVG_LI)
        ruta = os.path.join(tempfile.gettempdir(), "arcmap_mcp_about.html")
        _log("escribiendo HTML en: " + ruta)
        f = open(ruta, "wb")
        try:
            f.write(html.encode("utf-8"))
        finally:
            f.close()
        _log("HTML escrito OK; lanzando proceso desacoplado...")
        _open_detached(ruta)
        _log("Popen retorno OK (proceso lanzado)")
    except Exception:
        _log("EXCEPCION:\n" + traceback.format_exc())
        _msg(ABOUT_TEXT, u"arcmap-mcp · Acerca de")


class ButtonStart(object):
    def __init__(self):
        self.enabled = True
        self.checked = False
        _buttons["start"] = self

    def onClick(self):
        mod = _get_bridge()
        if mod is None:
            _msg(u"No se ha podido cargar arcmap_bridge.py desde:\n%s\n\n%s"
                 % (REPO_DIR, _bridge["error"]), u"arcmap-mcp · Error")
            return
        try:
            mod.start()
            _set_running(True)
            _msg(u"El puente MCP está activo y escuchando en %s:%s.\n"
                 u"Mantén ArcMap abierto mientras lo utilizas.\n\n"
                 u"pedralcg.dev" % (mod.HOST, mod.PORT),
                 u"arcmap-mcp · Puente iniciado")
        except Exception:
            _msg(u"No se ha podido iniciar el puente:\n%s" % traceback.format_exc(),
                 u"arcmap-mcp · Error")


class ButtonStop(object):
    def __init__(self):
        self.enabled = False        # gris hasta que el puente arranque
        self.checked = False
        _buttons["stop"] = self

    def onClick(self):
        mod = _get_bridge()
        if mod is None:
            _msg(u"El puente MCP no está cargado; no hay nada que detener.",
                 u"arcmap-mcp · Estado del puente")
            return
        try:
            mod.stop()
            _set_running(False)
            _msg(u"El puente MCP se ha detenido correctamente.",
                 u"arcmap-mcp · Puente detenido")
        except Exception:
            _msg(u"No se ha podido detener el puente:\n%s" % traceback.format_exc(),
                 u"arcmap-mcp · Error")


class ButtonStatus(object):
    def __init__(self):
        self.enabled = True
        self.checked = False
        _buttons["status"] = self

    def onClick(self):
        mod = _bridge["mod"]
        if mod is None:
            _msg(u"El puente MCP aún no se ha iniciado.\n"
                 u"Pulsa «Iniciar MCP» para activarlo.",
                 u"arcmap-mcp · Estado del puente")
            return
        run = bool(mod._server.get("running"))
        _set_running(run)
        estado = u"ACTIVO" if run else u"PARADO"
        _msg(u"Estado del puente MCP: %s\nDirección: %s:%s"
             % (estado, mod.HOST, mod.PORT),
             u"arcmap-mcp · Estado del puente")


class ButtonAbout(object):
    def __init__(self):
        self.enabled = True
        self.checked = False
        _buttons["about"] = self

    def onClick(self):
        _show_about()
