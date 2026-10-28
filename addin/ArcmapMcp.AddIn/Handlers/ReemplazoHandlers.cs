using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.DataSourcesRaster;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// replace_text: buscar y reemplazar por diccionario en el plano —textos del layout
    /// (también dentro de grupos), nombres de capa, etiquetas de clase y encabezados de la
    /// simbología— conservando las mayúsculas. Es la op `word_fix` de una serie de montes, que corrigió
    /// las tildes de los 386 planos («Area» → «Área», «AREA» → «ÁREA»). Por la costura
    /// Documento vale en vivo y en edit_mxd_lote. Las capas, clases y encabezados son los del
    /// data frame ACTIVO; los textos, los de todo el layout.
    /// </summary>
    internal static partial class LayoutHandlers
    {
        private static readonly Regex EtiquetaFormato = new Regex("<[^>]*>");

        public static JObject ReplaceText(JObject parameters)
        {
            JObject cambios = parameters["cambios"] as JObject;
            if (cambios == null || cambios.Count == 0)
                throw new ArgumentException("'cambios' es un diccionario {\"buscar\": \"poner\"}: {\"Area\": \"Área\"}.");
            bool palabraCompleta = Parametros.LeerBool(parameters["palabra_completa"], "palabra_completa", true);
            bool conservar = Parametros.LeerBool(parameters["conservar_mayusculas"], "conservar_mayusculas", true);
            bool simular = Parametros.LeerBool(parameters["simular"], "simular", false);
            var ambitos = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { "textos", "capas", "clases", "encabezados" };
            if (Parametros.Dado(parameters["ambitos"]))
            {
                JArray a = parameters["ambitos"] as JArray;
                if (a == null || a.Count == 0)
                    throw new ArgumentException("'ambitos' es una lista: textos, capas, clases, encabezados.");
                var pedidos = new HashSet<string>(a.Select(x => (string)x), StringComparer.OrdinalIgnoreCase);
                var raros = pedidos.Where(p => !ambitos.Contains(p)).ToList();
                if (raros.Count > 0)
                    throw new ArgumentException("Ámbitos desconocidos: " + string.Join(", ", raros)
                        + ". Valen: textos, capas, clases, encabezados.");
                ambitos = pedidos;
            }

            // UNA expresión con una alternativa por entrada, de la más larga a la más corta: «Area
            // Natural» antes que «Area», para que la frase gane a la palabra. Y en UNA pasada: con
            // una expresión por entrada aplicadas en cadena, {"Area": "Zona", "Zona": "Área"}
            // convertía «Area» en «Área» (revisión del 2026-10-25).
            var alternativas = new List<string>();
            var poner = new List<string>();
            foreach (var p in cambios.Properties().OrderByDescending(p => p.Name.Length))
            {
                if (p.Name.Length == 0)
                    throw new ArgumentException("'cambios' tiene una clave vacía.");
                string patron = Regex.Escape(p.Name);
                if (palabraCompleta)
                    patron = @"(?<![\p{L}\p{N}_])" + patron + @"(?![\p{L}\p{N}_])";
                alternativas.Add("(?<r" + poner.Count + ">" + patron + ")");
                poner.Add((string)p.Value ?? "");
            }
            var reglas = new Regex(string.Join("|", alternativas),
                                   RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

            Documento doc = Documento.Actual();
            var hechos = new JArray();
            Func<string, string> aplicar = s => Reemplazar(s, reglas, poner, conservar);
            Action<string, string, string, string> anotar = (ambito, donde, antes, ahora) =>
                hechos.Add(new JObject { ["ambito"] = ambito, ["donde"] = donde, ["antes"] = antes, ["ahora"] = ahora });

            // Textos del layout, sin tocar las etiquetas de formato (<BOL>, <FNT name='Arial'>).
            if (ambitos.Contains("textos"))
            {
                IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
                var raices = new List<IElement>();
                foreach (ElementoLayout e in Elementos(gc))
                {
                    ITextElement te = e.Elemento as ITextElement;
                    if (te == null)
                        continue;
                    string antes = te.Text ?? "";
                    string ahora = FueraDeEtiquetas(antes, aplicar);
                    if (ahora == antes)
                        continue;
                    anotar("textos", string.IsNullOrEmpty(e.Grupo) ? "(suelto)" : "grupo " + e.Grupo, antes, ahora);
                    if (!simular)
                    {
                        te.Text = ahora;
                        if (!raices.Any(r => ReferenceEquals(r, e.Raiz)))
                            raices.Add(e.Raiz);
                    }
                }
                foreach (IElement r in raices)
                {
                    try { gc.UpdateElement(r); } catch { }
                }
            }

            IMap map = doc.FocusMap;
            foreach (MapHandlers.CapaEnMapa c in MapHandlers.CapasConRuta(map))
            {
                if (ambitos.Contains("capas"))
                {
                    string antes = c.Capa.Name ?? "";
                    string ahora = aplicar(antes);
                    if (ahora != antes)
                    {
                        anotar("capas", c.Ruta, antes, ahora);
                        if (!simular)
                            c.Capa.Name = ahora;
                    }
                }
                if (ambitos.Contains("clases") || ambitos.Contains("encabezados"))
                    CorregirSimbologia(c, ambitos.Contains("clases"), ambitos.Contains("encabezados"),
                                       aplicar, simular, anotar);
            }

            if (!simular && hechos.Count > 0)
            {
                MapHandlers.NotificarCambioContenido(map, doc);
                doc.RefrescarGraficosLayout();
                doc.Refrescar();
            }
            var porAmbito = new JObject();
            foreach (string a in new[] { "textos", "capas", "clases", "encabezados" })
                if (ambitos.Contains(a))
                    porAmbito[a] = hechos.Count(h => (string)h["ambito"] == a);
            return Protocol.Result(new JObject
            {
                ["simulado"] = simular,
                ["cambiados"] = hechos.Count,
                ["por_ambito"] = porAmbito,
                ["cambios"] = hechos
            });
        }

        private static string FueraDeEtiquetas(string texto, Func<string, string> aplicar)
        {
            var sb = new System.Text.StringBuilder();
            int i = 0;
            foreach (Match m in EtiquetaFormato.Matches(texto))
            {
                sb.Append(aplicar(texto.Substring(i, m.Index - i)));
                sb.Append(m.Value);
                i = m.Index + m.Length;
            }
            sb.Append(aplicar(texto.Substring(i)));
            return sb.ToString();
        }

        private static string Reemplazar(string s, Regex reglas, List<string> poner, bool conservar)
        {
            if (string.IsNullOrEmpty(s))
                return s ?? "";
            return reglas.Replace(s, m =>
            {
                int i = 0;
                while (!m.Groups["r" + i].Success)
                    i++;
                return conservar ? ConMayusculasDe(m.Value, poner[i]) : poner[i];
            });
        }

        /// <summary>«AREA» → «ÁREA», «Area» → «Área», «area» → «área»; si el original mezcla, el
        /// reemplazo tal cual se escribió. Nunca se bajan a minúscula las mayúsculas PROPIAS del
        /// reemplazo (siglas, nombres): {"Murcia": "Región de Murcia"} daba «Región de murcia» y
        /// {"Rn2000": "RN2000"} dejaba «Rn2000» contando 0 cambios (revisión del 2026-10-25).</summary>
        private static string ConMayusculasDe(string original, string poner)
        {
            bool hayLetras = original.Any(char.IsLetter);
            if (!hayLetras || poner.Length == 0)
                return poner;
            // Mayúsculas más allá de la primera letra: el reemplazo trae su propia grafía.
            bool ponerConMayusculasPropias = poner.Where(char.IsLetter).Skip(1).Any(char.IsUpper);
            if (original.Where(char.IsLetter).All(char.IsUpper) && original.Count(char.IsLetter) > 1)
                return poner.ToUpperInvariant();
            if (original.Where(char.IsLetter).All(char.IsLower))
                return ponerConMayusculasPropias ? poner : poner.ToLowerInvariant();
            char primera = original.First(char.IsLetter);
            if (char.IsUpper(primera) && original.Where(char.IsLetter).Skip(1).All(char.IsLower))
                return char.ToUpperInvariant(poner[0]) + poner.Substring(1);
            return poner;
        }

        private static void CorregirSimbologia(MapHandlers.CapaEnMapa c, bool clases, bool encabezados,
            Func<string, string> aplicar, bool simular, Action<string, string, string, string> anotar)
        {
            object renderer = null;
            IGeoFeatureLayer gfl = c.Capa as IGeoFeatureLayer;
            IRasterLayer rl = c.Capa as IRasterLayer;
            if (gfl != null) renderer = gfl.Renderer;
            else if (rl != null) renderer = rl.Renderer;
            if (renderer == null)
                return;
            bool tocadoRaster = false;

            if (clases)
            {
                ISimpleRenderer simple = renderer as ISimpleRenderer;
                IUniqueValueRenderer uv = renderer as IUniqueValueRenderer;
                IClassBreaksRenderer cb = renderer as IClassBreaksRenderer;
                IRasterClassifyColorRampRenderer rc = renderer as IRasterClassifyColorRampRenderer;
                IRasterUniqueValueRenderer ru = renderer as IRasterUniqueValueRenderer;
                Action<string, Action<string>> uno = (antes, poner) =>
                {
                    string ahora = aplicar(antes ?? "");
                    if (ahora == (antes ?? ""))
                        return;
                    anotar("clases", c.Ruta, antes, ahora);
                    if (!simular)
                        poner(ahora);
                };
                if (simple != null)
                    uno(simple.Label, t => simple.Label = t);
                else if (uv != null)
                {
                    // En valores únicos la etiqueta vive en el renderer (uv.Label[v]), no solo en
                    // ILegendInfo: se cambia ahí (serie de montes).
                    for (int i = 0; i < uv.ValueCount; i++)
                    {
                        string v = uv.get_Value(i);
                        uno(uv.get_Label(v), t => uv.set_Label(v, t));
                    }
                    if (uv.UseDefaultSymbol)
                        uno(uv.DefaultLabel, t => uv.DefaultLabel = t);
                }
                else if (cb != null)
                {
                    for (int i = 0; i < cb.BreakCount; i++)
                    {
                        int k = i;
                        uno(cb.get_Label(k), t => cb.set_Label(k, t));
                    }
                }
                else if (rc != null)
                {
                    for (int i = 0; i < rc.ClassCount; i++)
                    {
                        int k = i;
                        uno(rc.get_Label(k), t => { rc.set_Label(k, t); tocadoRaster = true; });
                    }
                }
                else if (ru != null)
                {
                    for (int i = 0; i < ru.get_ClassCount(0); i++)
                    {
                        int k = i;
                        uno(ru.get_Label(0, k), t => { ru.set_Label(0, k, t); tocadoRaster = true; });
                    }
                }
            }

            // Encabezados: los de ILegendInfo. En un ráster, DESPUÉS del Update (lo regenera).
            if (tocadoRaster && !simular)
            {
                ILegendInfo liAntes = renderer as ILegendInfo;
                var guardados = new List<string>();
                try
                {
                    for (int g = 0; liAntes != null && g < liAntes.LegendGroupCount; g++)
                        guardados.Add(liAntes.get_LegendGroup(g).Heading);
                }
                catch { }
                ((IRasterRenderer)renderer).Update();
                try
                {
                    for (int g = 0; liAntes != null && g < liAntes.LegendGroupCount && g < guardados.Count; g++)
                        liAntes.get_LegendGroup(g).Heading = guardados[g];
                }
                catch { }
                rl.Renderer = (IRasterRenderer)renderer;
            }
            if (encabezados)
            {
                ILegendInfo li = renderer as ILegendInfo;
                if (li == null)
                    return;
                for (int g = 0; g < li.LegendGroupCount; g++)
                {
                    ILegendGroup grupo = li.get_LegendGroup(g);
                    string antes = grupo.Heading ?? "";
                    string ahora = aplicar(antes);
                    if (ahora == antes)
                        continue;
                    anotar("encabezados", c.Ruta, antes, ahora);
                    if (!simular)
                    {
                        grupo.Heading = ahora;
                        IRasterUniqueValueRenderer ru = renderer as IRasterUniqueValueRenderer;
                        if (ru != null) { try { ru.set_Heading(0, ahora); } catch { } }
                    }
                }
            }
        }
    }
}
