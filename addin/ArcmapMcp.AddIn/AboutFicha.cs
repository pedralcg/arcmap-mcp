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
        private const string Autor = "Pedro Alcoba Gomez";
        private const string Tagline = "Del dato ambiental al producto digital";
        private const string Web = "https://pedralcg.dev";
        private const string Email = "pedro@pedralcg.dev";
        private const string GitHub = "https://github.com/pedralcg";
        private const string LinkedIn = "https://www.linkedin.com/in/pedro-alcoba-gomez/";
        private const string Lugar = "Bullas, Murcia";

        private const string SvgWeb =
            @"<svg viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""none"" stroke=""#1e5631"" stroke-width=""2""><circle cx=""12"" cy=""12"" r=""9""/><path d=""M3 12h18M12 3c2.5 2.7 2.5 15.3 0 18M12 3c-2.5 2.7-2.5 15.3 0 18""/></svg>";
        private const string SvgMail =
            @"<svg viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""none"" stroke=""#1e5631"" stroke-width=""2""><rect x=""3"" y=""5"" width=""18"" height=""14"" rx=""2""/><path d=""M3 7l9 6 9-6""/></svg>";
        private const string SvgGh =
            @"<svg viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""#1e5631""><path d=""M12 2C6.5 2 2 6.6 2 12.3c0 4.5 2.9 8.3 6.8 9.7.5.1.7-.2.7-.5v-1.7c-2.8.6-3.4-1.4-3.4-1.4-.5-1.2-1.1-1.5-1.1-1.5-.9-.6.1-.6.1-.6 1 .1 1.5 1 1.5 1 .9 1.6 2.4 1.1 3 .9.1-.7.3-1.1.6-1.4-2.2-.3-4.6-1.1-4.6-5 0-1.1.4-2 1-2.7-.1-.3-.4-1.3.1-2.7 0 0 .8-.3 2.7 1a9.3 9.3 0 0 1 5 0c1.9-1.3 2.7-1 2.7-1 .5 1.4.2 2.4.1 2.7.6.7 1 1.6 1 2.7 0 3.9-2.3 4.7-4.6 5 .4.3.7.9.7 1.9v2.8c0 .3.2.6.7.5A10.3 10.3 0 0 0 22 12.3C22 6.6 17.5 2 12 2z""/></svg>";
        private const string SvgLi =
            @"<svg viewBox=""0 0 24 24"" width=""22"" height=""22"" fill=""#1e5631""><path d=""M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zM9 9h3.8v1.7h.1c.5-.9 1.8-1.9 3.6-1.9 3.9 0 4.6 2.5 4.6 5.8V21h-4v-5.3c0-1.3 0-2.9-1.8-2.9s-2 1.4-2 2.8V21H9z""/></svg>";

        private const string Html = @"<!DOCTYPE html>
<html lang=""es""><head><meta charset=""utf-8"">
<meta name=""viewport"" content=""width=device-width, initial-scale=1"">
<title>arcmap-mcp &middot; pedralcg.dev</title>
<style>
 :root{--green:#1e5631;--green2:#2e7d46;--amber:#d98324;--soft:#d7e8d0;
        --cream:#faf3e6;--bg:#eef3ea;--text:#3a4a3c;--muted:#6f8071;--border:#e0e8dc;}
 *{box-sizing:border-box;}
 body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);
      font-family:'Segoe UI',system-ui,-apple-system,Arial,sans-serif;
      display:flex;align-items:center;justify-content:center;padding:32px;}
 .wrap{width:100%;max-width:560px;}
 .topbar{height:5px;border-radius:6px 6px 0 0;background:var(--green);
          border-bottom:0;}
 .topbar i{display:block;height:5px;border-radius:6px 6px 0 0;
            background:linear-gradient(90deg,var(--green) 0%,var(--green) 70%,var(--amber) 100%);}
 .brand{font-weight:700;font-size:15px;color:var(--green);padding:14px 4px 0;}
 .brand b{color:var(--amber);}
 .hero{margin-top:10px;border:1px solid var(--border);border-radius:18px;padding:30px 32px;
        background:linear-gradient(135deg,#e9f1e4 0%,#f2f1e6 55%,var(--cream) 100%);
        box-shadow:0 14px 40px rgba(20,60,30,.10);}
 .hero h1{margin:0;font-size:30px;font-weight:800;color:var(--green);letter-spacing:-.5px;}
 .hero .kicker{margin:6px 0 0;font-size:13px;font-weight:700;letter-spacing:1.2px;
                text-transform:uppercase;color:var(--amber);}
 .hero p.desc{margin:16px 0 0;font-size:15px;line-height:1.6;color:var(--text);}
 .hero p.desc b{color:var(--green);}
 .quote{margin:20px 0 4px;padding:4px 0 4px 14px;border-left:3px solid var(--green2);
         font-style:italic;color:var(--muted);font-size:14px;}
 .cards{margin-top:18px;display:grid;grid-template-columns:1fr 1fr;gap:14px;}
 a.card{display:flex;flex-direction:column;gap:8px;text-decoration:none;background:#fff;
         border:1px solid var(--border);border-radius:14px;padding:16px 16px 14px;
         transition:transform .12s,box-shadow .12s,border-color .12s;}
 a.card:hover{transform:translateY(-3px);box-shadow:0 12px 26px rgba(20,60,30,.12);
               border-color:var(--green2);}
 .ic{width:40px;height:40px;border-radius:11px;background:var(--soft);
      display:flex;align-items:center;justify-content:center;}
 a.card .t{font-size:15px;font-weight:700;color:var(--green);}
 a.card .s{font-size:12px;color:var(--muted);word-break:break-all;}
 .foot{margin-top:18px;text-align:center;font-size:12px;color:var(--muted);}
 .foot b{color:var(--amber);}
</style></head><body>
 <div class=""wrap"">
  <div class=""topbar""><i></i></div>
  <div class=""brand"">pedralcg<b>.dev</b></div>
  <div class=""hero"">
   <h1>arcmap-mcp</h1>
   <p class=""kicker"">Puente MCP para ArcMap</p>
   <p class=""desc"">Conduce la sesi&oacute;n viva de ArcMap desde un agente IA.
      Software libre creado por <b>__AUTOR__</b> &middot; __LUGAR__.</p>
   <p class=""quote"">__TAGLINE__</p>
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
   <p class=""foot"">arcmap-mcp &middot; <b>pedralcg.dev</b></p>
  </div>
 </div>
</body></html>";

        public static void Show()
        {
            try
            {
                string html = Html
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
                    "arcmap-mcp - Puente MCP para ArcMap\n\n"
                    + "Autor:    " + Autor + "\n"
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
