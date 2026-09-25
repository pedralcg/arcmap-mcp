using System;
using System.IO;
using System.Windows.Forms;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// "Acerca de": escribe la ficha HTML (identidad visual pedralcg.dev) en
    /// %TEMP% y la abre con el visor por defecto. En .NET Process.Start con
    /// UseShellExecute no bloquea el hilo de ArcMap. Fallback: MessageBox.
    /// </summary>
    internal static class AboutFicha
    {
        private const string Autor = "Pedro Alcoba Gómez";
        private const string Tagline = "Del dato ambiental al producto digital";
        private const string Web = "https://pedralcg.dev";
        private const string Email = "pedro@pedralcg.dev";
        private const string GitHub = "https://github.com/pedralcg";
        private const string LinkedIn = "https://www.linkedin.com/in/pedro-alcoba-gomez/";
        private const string Lugar = "Bullas, Murcia";
        private const string Repo = "https://github.com/pedralcg/arcmap-mcp";

        private const string Issues = "https://github.com/pedralcg/arcmap-mcp/issues";
        private const string Catalogo = "https://github.com/pedralcg/arcmap-mcp/blob/main/docs/TOOLS.md";
        private const string Novedades = "https://github.com/pedralcg/arcmap-mcp/blob/main/CHANGELOG.md";

        // Tools que el servidor MCP resuelve SIN el puente: describe_mxd, audit_folder y
        // export_mxd_lote (2.13.0) trabajan con .mxd del disco y no tienen comando en
        // McpServer. Son el +3 sobre los comandos del puente. Si se añade otra tool sin
        // puente en el servidor, hay que subir este número: el test
        // TestContratoDeTools.SIN_PUENTE de tests/test_client_protocol.py es la lista.
        private const int ToolsSinPuente = 3;

        // Derivado y no escrito a mano: estaba en 48 cuando ya eran 57, porque un número
        // suelto en un literal no se actualiza al añadir una tool. La correspondencia es
        // 1:1 — cada comando del puente es una @mcp.tool de src/arcmap_mcp_server.py
        // (execute_code es la de execute_arcpy) — verificada el 2026-09-23:
        // 46 nativos + 9 de fondo + 3 sin puente = 58 tools. 2.14.0: 48 nativos
        // (add_group, set_legend_item) = 60.
        private static int NumHerramientas
        {
            get { return McpServer.NumComandos + ToolsSinPuente; }
        }

        private const string SvgWeb =
            @"<svg aria-hidden=""true"" viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""none"" stroke=""#1e5c2e"" stroke-width=""2""><circle cx=""12"" cy=""12"" r=""9""/><path d=""M3 12h18M12 3c2.5 2.7 2.5 15.3 0 18M12 3c-2.5 2.7-2.5 15.3 0 18""/></svg>";
        private const string SvgMail =
            @"<svg aria-hidden=""true"" viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""none"" stroke=""#1e5c2e"" stroke-width=""2""><rect x=""3"" y=""5"" width=""18"" height=""14"" rx=""2""/><path d=""M3 7l9 6 9-6""/></svg>";
        private const string SvgGh =
            @"<svg aria-hidden=""true"" viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""#1e5c2e""><path d=""M12 2C6.5 2 2 6.6 2 12.3c0 4.5 2.9 8.3 6.8 9.7.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.4-3.4-1.4-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.6 2.4 1.1 3 .9.1-.7.3-1.1.6-1.4-2.2-.3-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.3 9.3 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.3 4.7-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10.3 10.3 0 0 0 22 12.3C22 6.6 17.5 2 12 2z""/></svg>";
        private const string SvgLi =
            @"<svg aria-hidden=""true"" viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""#1e5c2e""><path d=""M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zM9 9h3.8v1.7h.1c.5-.9 1.8-1.9 3.6-1.9 3.9 0 4.6 2.5 4.6 5.8V21h-4v-5.3c0-1.3 0-2.9-1.8-2.9s-2 1.4-2 2.8V21H9z""/></svg>";

        private const string Html = @"<!DOCTYPE html>
<html lang=""es""><head><meta charset=""utf-8"">
<meta name=""viewport"" content=""width=device-width, initial-scale=1"">
<title>arcmap-mcp &middot; pedralcg.dev</title>
<link rel=""preconnect"" href=""https://fonts.googleapis.com"">
<link rel=""stylesheet"" href=""https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&amp;display=swap"">
<style>
 /* Tokens de pedralcg.dev, copiados de la web desplegada (BaseLayout.css, 2026-09-23):
    forest, forest-mid, earth, sage-lt, bg, text, text-muted, border, surface y radios.
    Los nombres internos (--green, --amber...) se conservan de la ficha original. */
 :root{--green:#1e5c2e;--green2:#2d7a3a;--amber:#c9882a;--soft:#d4e8d0;--sage:#a8c8a0;
        --cream:#faf3e6;--bg:#f1f6f1;--text:#1a2a1a;--muted:#445044;--border:#e2ede2;
        --panel:#f9fbf9;--radius-md:14px;--radius-lg:22px;}
 *{box-sizing:border-box;}
 body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);
      font-family:'Inter',system-ui,-apple-system,'Segoe UI',Arial,sans-serif;
      display:flex;align-items:center;justify-content:center;padding:40px 32px;}
 /* Ancho de escritorio como el contenedor de pedralcg.dev (~1000 px): la ficha de
    560 px se leía como una página de móvil en una ventana de navegador normal. */
 .wrap{width:100%;max-width:1040px;}
 .cols{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:40px;
        align-items:start;}
 .cols > div > h2:first-child{margin-top:4px;}
 .autor{margin-top:28px;}
 .autor h2{margin:0 4px 12px;}
 /* Cabecera como la de pedralcg.dev: barra verde bosque con la marca en blanco y
    '.dev' en tierra, y el filo inferior de verde a tierra que ya tenía la ficha. */
 .topbar{background:var(--green);border-radius:var(--radius-md) var(--radius-md) 0 0;
          padding:12px 18px;}
 .topbar i{display:block;height:3px;margin:12px -18px -12px;
            background:linear-gradient(90deg,var(--green) 0%,var(--green2) 55%,var(--amber) 100%);}
 .brand{font-weight:800;font-size:16px;color:#fff;letter-spacing:-.2px;}
 .brand b{color:var(--amber);font-weight:800;}
 .hero{margin-top:14px;border:1px solid var(--border);border-radius:var(--radius-lg);padding:40px 44px;
        background:linear-gradient(135deg,#e9f1e4 0%,#f2f1e6 55%,var(--cream) 100%);
        box-shadow:0 14px 40px rgba(20,60,30,.10);}
 .hero h1{margin:0;font-size:38px;font-weight:800;color:var(--green);letter-spacing:-.8px;}
 .hero .kicker{margin:6px 0 0;font-size:13px;font-weight:700;letter-spacing:1.2px;
                text-transform:uppercase;color:var(--amber);}
 .hero p.desc{margin:18px 0 0;font-size:16px;line-height:1.65;color:var(--text);}
 .hero p.desc b{color:var(--green);}
 .quote{margin:20px 0 4px;padding:4px 0 4px 14px;border-left:3px solid var(--green2);
         font-style:italic;color:var(--muted);font-size:14px;}
 .cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;}
 a.card{display:flex;flex-direction:column;align-items:center;text-align:center;gap:8px;
         text-decoration:none;background:#fff;box-shadow:0 2px 8px #0000000d;
         border:1px solid var(--border);border-radius:var(--radius-md);padding:16px 16px 14px;
         transition:transform .12s,box-shadow .12s,border-color .12s;}
 a.card:hover{transform:translateY(-3px);box-shadow:0 12px 26px rgba(20,60,30,.12);
               border-color:var(--green2);}
 .ic{width:40px;height:40px;border-radius:11px;background:var(--soft);
      display:flex;align-items:center;justify-content:center;}
 a.card .t{font-size:15px;font-weight:700;color:var(--green);}
 a.card .s{font-size:12px;color:var(--muted);word-break:break-all;}
 .foot{margin-top:18px;text-align:center;font-size:12px;color:var(--muted);}
 .foot b{color:var(--amber);}
 .foot a{color:var(--green);text-decoration:none;font-weight:600;}
 .foot a:hover{text-decoration:underline;}
 .titulo{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;}
 .ver{font-size:12px;font-weight:700;letter-spacing:.5px;color:var(--green);
       background:var(--soft);border-radius:999px;padding:3px 10px;}
 .upd{font-size:12px;font-weight:700;letter-spacing:.5px;color:#fff;
       background:var(--amber);border-radius:999px;padding:3px 10px;text-decoration:none;}
 h2{margin:24px 0 10px;font-size:12px;font-weight:700;letter-spacing:1.2px;
    text-transform:uppercase;color:var(--amber);}
 .estado{background:var(--panel);border:1px solid var(--border);border-radius:14px;
          padding:6px 16px;margin:0;}
 .estado div{display:flex;justify-content:space-between;gap:12px;padding:8px 0;
              border-bottom:1px solid var(--border);font-size:13px;}
 .estado div:last-child{border-bottom:0;}
 .estado dt{color:var(--muted);}
 .estado dd{margin:0;font-weight:600;text-align:right;word-break:break-all;}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;
       vertical-align:1px;background:var(--amber);}
 .dot.on{background:var(--green2);}
 .prompts{margin:0;padding:0;list-style:none;display:grid;gap:8px;}
 .prompts li{background:var(--panel);border:1px solid var(--border);border-left:3px solid var(--green2);
              border-radius:10px;padding:9px 12px;font-size:13px;line-height:1.5;}
 .links{display:flex;flex-wrap:wrap;gap:8px;margin:0;}
 /* Botones de la web: primario verde relleno, secundario blanco con borde. */
 .links a{font-size:13px;font-weight:700;color:var(--green);text-decoration:none;
           background:#fff;border:1px solid var(--border);border-radius:8px;padding:8px 14px;
           box-shadow:0 2px 8px #0000000d;transition:border-color .12s,background .12s;}
 .links a:hover{border-color:var(--green2);background:var(--panel);}
 .links a.primario{background:var(--green);border-color:var(--green);color:#fff;}
 .links a.primario:hover{background:var(--green2);}
 a:focus-visible{outline:3px solid var(--amber);outline-offset:2px;}
 @media (max-width:860px){.cols{grid-template-columns:1fr;gap:0;}
   .cols > div + div > h2:first-child{margin-top:26px;}
   .cards{grid-template-columns:1fr 1fr;} .hero{padding:30px 28px;} .hero h1{font-size:30px;}}
 @media (max-width:480px){body{padding:16px;} .hero{padding:24px 20px;}
   .cards{grid-template-columns:1fr;} .hero h1{font-size:26px;}}
 @media (prefers-reduced-motion:reduce){a.card{transition:none;} a.card:hover{transform:none;}}
</style></head><body>
 <div class=""wrap"">
  <div class=""topbar""><span class=""brand"">pedralcg<b>.dev</b></span><i></i></div>
  <main class=""hero"">
   <div class=""cols"">
   <div>
   <div class=""titulo""><h1>arcmap-mcp</h1><span class=""ver"">v__VERSION__</span>__UPDATE__</div>
   <p class=""kicker"">Puente MCP para ArcMap</p>
   <p class=""desc"">Conduce la sesi&oacute;n viva de ArcMap desde un agente IA:
      <b>__NUM_TOOLS__ herramientas</b> para listar y simbolizar capas, consultar datos,
      encuadrar, geoprocesar y exportar series de planos.
      Software libre creado por <b>__AUTOR__</b> &middot; __LUGAR__.</p>
   <p class=""quote"">__TAGLINE__</p>

   <h2>Enlaces &uacute;tiles</h2>
   <p class=""links"">
    <a class=""primario"" href=""__ACTUALIZAR__"">C&oacute;mo actualizar &rarr;</a>
    <a href=""__CATALOGO__"">Cat&aacute;logo de herramientas</a>
    <a href=""__NOVEDADES__"">Novedades</a>
    <a href=""__ISSUES__"">Informar de un problema</a>
   </p>
   </div>

   <div>
   <h2>Estado de esta sesi&oacute;n</h2>
   <dl class=""estado"">
    <div><dt>Puente</dt><dd>__PUENTE__</dd></div>
    <div><dt>Arranque autom&aacute;tico</dt><dd>__AUTOARRANQUE__</dd></div>
    <div><dt>Python 2.7 (arcpy)</dt><dd>__PY27__</dd></div>
    <div><dt>Registro de actividad</dt><dd>__LOG__</dd></div>
   </dl>

   <h2>Pru&eacute;balo en tu asistente</h2>
   <ul class=""prompts"">
    <li>&laquo;&iquest;Est&aacute; vivo el puente de ArcMap? Dime qu&eacute; documento tengo abierto.&raquo;</li>
    <li>&laquo;Lista las capas del mapa y dime qu&eacute; campos tiene la primera.&raquo;</li>
    <li>&laquo;Exporta el layout a PDF en C:\temp\plano.pdf a 300 ppp.&raquo;</li>
   </ul>
   </div>
   </div>
  </main>

  <section class=""autor"">
   <h2>Autor</h2>
   <div class=""cards"">
    <a class=""card"" href=""__WEB__""><span class=""ic"">__SVG_WEB__</span>
       <span class=""t"">Web</span><span class=""s"">pedralcg.dev</span></a>
    <a class=""card"" href=""mailto:__EMAIL__""><span class=""ic"">__SVG_MAIL__</span>
       <span class=""t"">Email</span><span class=""s"">__EMAIL__</span></a>
    <a class=""card"" href=""__GITHUB__""><span class=""ic"">__SVG_GH__</span>
       <span class=""t"">GitHub</span><span class=""s"">@pedralcg</span></a>
    <a class=""card"" href=""__LINKEDIN__""><span class=""ic"">__SVG_LI__</span>
       <span class=""t"">LinkedIn</span><span class=""s"">pedro-alcoba-gomez</span></a>
   </div>
   <p class=""foot"">C&oacute;digo abierto (licencia MIT) en
      <a href=""__REPO__"">github.com/pedralcg/arcmap-mcp</a><br>
      arcmap-mcp v__VERSION__ &middot; <b>pedralcg.dev</b></p>
  </section>
 </div>
</body></html>";

        private static string Esc(string texto)
        {
            return System.Net.WebUtility.HtmlEncode(texto ?? "");
        }

        public static void Show()
        {
            try
            {
                // Versión leída del ensamblado: la ficha no se queda desfasada.
                string version = typeof(AboutFicha).Assembly.GetName().Version.ToString(3);
                // Badge de actualización, solo si el chequeo en 2º plano encontró una nueva.
                string update = Actualizaciones.HayNueva
                    ? @"<a class=""upd"" href=""" + Actualizaciones.ActualizarUrl + @""">&#8593; v"
                      + Actualizaciones.UltimaDisponible + " disponible</a>"
                    : "";

                // Estado de la sesión: lo que alguien que no entiende del tema necesita
                // poder leer (o copiar) cuando algo no va. Todo va escapado: una ruta
                // puede traer '&' o '<'.
                McpExtension ext = McpExtension.Instance;
                bool activo = ext != null && ext.IsRunning;
                string puente = activo
                    ? @"<span class=""dot on""></span>Activo en 127.0.0.1:" + McpServer.Port
                    : @"<span class=""dot""></span>Parado &middot; pulsa <b>Iniciar</b> en la barra";
                string autoarranque = Ajustes.Autoarranque
                    ? "S&iacute;" : "No &middot; act&iacute;valo en el desplegable de la barra";

                string html = Html
                    .Replace("__PUENTE__", puente)
                    .Replace("__AUTOARRANQUE__", autoarranque)
                    .Replace("__PY27__", Esc(Diagnostico.DescribirPython27()))
                    .Replace("__LOG__", Esc(Log.PathInfo))
                    .Replace("__ACTUALIZAR__", Actualizaciones.ActualizarUrl)
                    .Replace("__CATALOGO__", Catalogo)
                    .Replace("__NOVEDADES__", Novedades)
                    .Replace("__ISSUES__", Issues)
                    .Replace("__VERSION__", version)
                    .Replace("__UPDATE__", update)
                    .Replace("__NUM_TOOLS__", NumHerramientas.ToString())
                    .Replace("__REPO__", Repo)
                    .Replace("__AUTOR__", Autor)
                    .Replace("__TAGLINE__", Tagline)
                    .Replace("__LUGAR__", Lugar)
                    .Replace("__WEB__", Web)
                    .Replace("__EMAIL__", Email)
                    .Replace("__GITHUB__", GitHub)
                    .Replace("__LINKEDIN__", LinkedIn)
                    .Replace("__SVG_WEB__", SvgWeb)
                    .Replace("__SVG_MAIL__", SvgMail)
                    .Replace("__SVG_GH__", SvgGh)
                    .Replace("__SVG_LI__", SvgLi);
                string ruta = Path.Combine(Path.GetTempPath(), "arcmap_mcp_about.html");
                File.WriteAllText(ruta, html, new System.Text.UTF8Encoding(false));
                System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                {
                    FileName = ruta,
                    UseShellExecute = true
                });
            }
            catch (Exception ex)
            {
                Log.Error("Acerca de: no se pudo abrir la ficha HTML", ex);
                MessageBox.Show(
                    "arcmap-mcp " + typeof(AboutFicha).Assembly.GetName().Version.ToString(3)
                    + " - Puente MCP para ArcMap (" + NumHerramientas + " herramientas)\n\n"
                    + "Autor:    " + Autor + "\n"
                    + "Repo:     " + Repo + "  (licencia MIT)\n"
                    + "Web:      " + Web + "\n"
                    + "Email:    " + Email + "\n"
                    + "GitHub:   " + GitHub + "\n"
                    + "LinkedIn: " + LinkedIn + "\n\n"
                    + Lugar,
                    "arcmap-mcp · Acerca de");
            }
        }
    }
}
