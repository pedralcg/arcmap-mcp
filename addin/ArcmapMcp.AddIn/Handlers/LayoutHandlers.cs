using System;
using System.Collections.Generic;
using System.Linq;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// set_text_element / list_layout_elements — contrato JSON que esperan los
    /// schemas del servidor MCP. Lógica de selección de set_text_element:
    /// selector 'nombre' (name del elemento) o 'buscar' (texto actual; exacto
    /// primero, subcadena después; ambigüedad = error que lista coincidencias).
    /// list_layout_elements devuelve los mismos nombres de clase que arcpy
    /// (TextElement, LegendElement, ...) y acepta los mismos filtros tipo/patron.
    ///
    /// El recorrido DESCIENDE por los IGroupElement: `IGraphicsContainer.Next()`
    /// solo da el nivel superior, y en una serie de planos el cajetín es un grupo,
    /// así que los textos de dentro —el título, la hoja, la escala, justo los que
    /// hay que rotular por página— ni se listaban ni se podían editar.
    /// </summary>
    internal static partial class LayoutHandlers
    {
        // Tipos arcpy admitidos en el filtro 'tipo' -> nombre de clase del item.
        private static readonly Dictionary<string, string> _tiposArcpy =
            new Dictionary<string, string>
            {
                { "TEXT_ELEMENT",        "TextElement" },
                { "LEGEND_ELEMENT",      "LegendElement" },
                { "PICTURE_ELEMENT",     "PictureElement" },
                { "MAPSURROUND_ELEMENT", "MapsurroundElement" },
                { "DATAFRAME_ELEMENT",   "DataFrameElement" },
                { "GRAPHIC_ELEMENT",     "GraphicElement" },
            };

        /// <summary>Un elemento del layout con lo que hace falta para localizarlo y
        /// para refrescarlo si se toca.</summary>
        internal sealed class ElementoLayout
        {
            public IElement Elemento;
            /// <summary>Elemento de PRIMER NIVEL que lo contiene (él mismo si no está
            /// agrupado): es el que entiende IGraphicsContainer.UpdateElement.</summary>
            public IElement Raiz;
            public string Nombre;
            /// <summary>Ruta de grupos que lo contiene, null si está en primer nivel.</summary>
            public string Grupo;
        }

        internal static List<ElementoLayout> Elementos(IGraphicsContainer gc)
        {
            var salida = new List<ElementoLayout>();
            gc.Reset();
            IElement el;
            while ((el = gc.Next()) != null)
                Recoger(el, el, null, salida);
            return salida;
        }

        private static void Recoger(IElement el, IElement raiz, string grupo, List<ElementoLayout> destino)
        {
            if (el == null)
                return;
            IElementProperties props = el as IElementProperties;
            string nombre = props != null ? (props.Name ?? "") : "";
            destino.Add(new ElementoLayout
            {
                Elemento = el,
                Raiz = raiz,
                Nombre = nombre,
                Grupo = grupo
            });

            IGroupElement grp = el as IGroupElement;
            if (grp == null)
                return;
            int n;
            try { n = grp.ElementCount; }
            catch { return; }
            string etiqueta = nombre.Length > 0 ? nombre : "(grupo sin nombre)";
            string rutaGrupo = grupo == null ? etiqueta : grupo + "/" + etiqueta;
            for (int i = 0; i < n; i++)
            {
                IElement hijo;
                try { hijo = grp.get_Element(i); }
                catch { continue; }
                Recoger(hijo, raiz, rutaGrupo, destino);
            }
        }

        public static JObject ListLayoutElements(JObject parameters)
        {
            string tipo = (string)parameters["tipo"];
            string patron = (string)parameters["patron"];
            if (!string.IsNullOrEmpty(tipo))
            {
                tipo = tipo.ToUpperInvariant();
                if (!_tiposArcpy.ContainsKey(tipo))
                    throw new ArgumentException("Tipo no válido: " + tipo
                        + ". Admitidos: " + string.Join(", ", _tiposArcpy.Keys));
            }

            System.Text.RegularExpressions.Regex regexPatron = null;
            if (!string.IsNullOrEmpty(patron) && patron != "*")
            {
                // Traducción wildcard -> regex (equivalente al fnmatch de Python).
                string rx = "^" + System.Text.RegularExpressions.Regex.Escape(patron)
                    .Replace(@"\*", ".*").Replace(@"\?", ".") + "$";
                regexPatron = new System.Text.RegularExpressions.Regex(
                    rx, System.Text.RegularExpressions.RegexOptions.IgnoreCase);
            }

            Documento doc = Documento.Actual();

            var elementos = new JArray();
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            foreach (ElementoLayout e in Elementos(gc))
            {
                string clase = ClasificarElemento(e.Elemento);
                if (tipo != null && _tiposArcpy[tipo] != clase)
                    continue;
                if (regexPatron != null && !regexPatron.IsMatch(e.Nombre))
                    continue;
                var item = new JObject
                {
                    ["nombre"] = e.Nombre,
                    ["tipo"] = clase,
                    ["grupo"] = e.Grupo
                };
                ITextElement te = e.Elemento as ITextElement;
                if (te != null)
                    item["texto"] = te.Text;
                elementos.Add(item);
            }

            return Protocol.Result(new JObject
            {
                ["num"] = elementos.Count,
                ["elementos"] = elementos
            });
        }

        private static string ClasificarElemento(IElement el)
        {
            if (el is ITextElement)
                return "TextElement";
            if (el is IMapFrame)
                return "DataFrameElement";
            IMapSurroundFrame msf = el as IMapSurroundFrame;
            if (msf != null)
                return msf.MapSurround is ILegend ? "LegendElement" : "MapsurroundElement";
            if (el is IPictureElement)
                return "PictureElement";
            return "GraphicElement";
        }

        /// <summary>
        /// De varios candidatos, UNO — o un error que dice cómo elegir. Nunca se elige
        /// por el usuario. Los desempates son `grupo` (nombre del grupo que lo contiene;
        /// "" = suelto, fuera de todo grupo) y, como último recurso, `indice` (1-based,
        /// en el orden en que el propio error los lista). El `indice` hace falta de
        /// verdad: un cajetín copiado de otro plano arrastra nombre Y texto, así que dos
        /// elementos pueden ser idénticos en todo lo demás; sin él quedaban inoperables
        /// (verificación adversarial del 2026-09-20).
        /// </summary>
        private static ElementoLayout Desempatar(List<ElementoLayout> candidatos, JObject parameters,
                                                 string descripcion)
        {
            candidatos = FiltrarPorGrupo(candidatos, parameters, descripcion);
            if (candidatos.Count == 1)
                return candidatos[0];

            JToken tIndice = parameters["indice"];
            if (tIndice != null && tIndice.Type != JTokenType.Null)
            {
                int indice = Parametros.LeerEntero(tIndice, "indice", 1, 1, candidatos.Count);
                return candidatos[indice - 1];
            }

            int n = 0;
            string lista = string.Join(" | ", candidatos.Select(p =>
                "[" + (++n) + "] \"" + Texto(p) + "\""
                + (string.IsNullOrEmpty(p.Grupo) ? " (suelto)" : " (grupo " + p.Grupo + ")")));
            throw new ArgumentException(
                "Hay " + candidatos.Count + " elementos de texto " + descripcion + " y no se elige"
                + " por ti. Afina 'buscar', indica 'grupo', o pasa 'indice' con el número de esta"
                + " lista: " + lista);
        }

        /// <summary>
        /// Deja solo los candidatos del `grupo` pedido ("" = sueltos), o todos si no se pide.
        /// Vale también con `todos=True`: ahí no se desempata, pero el grupo sigue acotando.
        /// Antes solo lo aplicaba Desempatar, y `patron` + `todos` + `grupo="Cajetin"`
        /// cambiaba los textos de todo el layout respondiendo ok (revisión del 2026-10-25).
        /// </summary>
        private static List<ElementoLayout> FiltrarPorGrupo(List<ElementoLayout> candidatos, JObject parameters,
                                                            string descripcion)
        {
            JToken tGrupo = parameters["grupo"];
            if (tGrupo == null || tGrupo.Type == JTokenType.Null)
                return candidatos;
            string grupo = (string)tGrupo ?? "";
            var filtrados = candidatos
                .Where(p => string.Equals(p.Grupo ?? "", grupo, StringComparison.OrdinalIgnoreCase))
                .ToList();
            if (filtrados.Count == 0)
                throw new ArgumentException("Ningún elemento de texto " + descripcion
                    + " está en el grupo '" + grupo + "' (usa grupo=\"\" para los sueltos).");
            return filtrados;
        }

        public static JObject SetTextElement(JObject parameters)
        {
            string nombre = (string)parameters["nombre"];
            string buscar = (string)parameters["buscar"];
            // Sin 'texto' el texto no se toca: mover o cambiar el tamaño de un título no
            // debe dejarlo en blanco. Un texto vacío explícito ("") sí lo vacía.
            string texto = Parametros.Dado(parameters["texto"]) ? (string)parameters["texto"] : null;
            double? x = Parametros.LeerDouble(parameters["x"], "x", -10000, 10000);
            double? y = Parametros.LeerDouble(parameters["y"], "y", -10000, 10000);
            double? tamano = Parametros.LeerDouble(parameters["tamano"], "tamano", 1, 500);
            Documento docAspecto = Documento.Actual();
            var aspecto = new Aspecto
            {
                X = x, Y = y, Tamano = tamano,
                Mayusculas = LeerMayusculas(parameters["mayusculas"]),
                CentrarEn = LeerRecuadro(parameters["centrar_en"], "centrar_en"),
                EncajarEn = LeerRecuadro(parameters["encajar_en"], "encajar_en"),
                Margen = Parametros.LeerDouble(parameters["margen"], "margen", 0, 1000) ?? 0,
                TamanoMin = Parametros.LeerDouble(parameters["tamano_min"], "tamano_min", 1, 500) ?? 4,
                Doc = docAspecto
            };
            if (aspecto.CentrarEn != null && (x != null || y != null))
                throw new ArgumentException("'centrar_en' y 'x'/'y' se excluyen: o se coloca en un punto o se centra en"
                    + " un recuadro.");
            if (aspecto.EncajarEn == null && (Parametros.Dado(parameters["margen"]) || Parametros.Dado(parameters["tamano_min"])))
                throw new ArgumentException("'margen' y 'tamano_min' van con 'encajar_en'.");
            // Elegir el texto por su POSICIÓN en la hoja: en una serie real el número de plano es un
            // «3» que se repite, y se elegía por una caja de coordenadas (2026-10-06).
            double[] region = LeerRecuadro(parameters["region"], "region");
            // Sustituir UNA PARTE del texto (la fecha, el expediente, una errata) sin conocer el
            // texto entero de cada plano: lo de `text_sub` en una serie de montes (Cowork, 2026-10-06).
            System.Text.RegularExpressions.Regex patron = null;
            string reemplazo = null;
            if (Parametros.Dado(parameters["patron"]))
            {
                if (texto != null)
                    throw new ArgumentException("'texto' y 'patron' se excluyen: 'texto' sustituye el texto entero y"
                        + " 'patron' + 'reemplazo' solo la parte que casa.");
                if (!Parametros.Dado(parameters["reemplazo"]))
                    throw new ArgumentException("'patron' va con 'reemplazo' (puede ser \"\" para borrar lo que casa).");
                try { patron = new System.Text.RegularExpressions.Regex((string)parameters["patron"]); }
                catch (ArgumentException ex)
                {
                    throw new ArgumentException("'patron' no es una expresión regular válida: " + ex.Message);
                }
                reemplazo = (string)parameters["reemplazo"];
            }
            else if (Parametros.Dado(parameters["reemplazo"]))
                throw new ArgumentException("'reemplazo' va con 'patron'.");
            bool todos = Parametros.LeerBool(parameters["todos"], "todos", false);
            if (texto == null && patron == null && !aspecto.AlgoQueCambiar)
                throw new ArgumentException("No hay nada que cambiar: indica 'texto', 'patron' + 'reemplazo', o"
                    + " 'x'/'y' (posición), 'tamano' (puntos), 'mayusculas', 'centrar_en' o 'encajar_en'.");

            Documento doc = docAspecto;

            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            var elementos = new List<ElementoLayout>();
            foreach (ElementoLayout e in Elementos(gc))
                if (e.Elemento is ITextElement)
                    elementos.Add(e);
            if (region != null)
            {
                var todosLosTextos = elementos;
                elementos = elementos.Where(e => CentroDentro(e.Elemento, region, doc)).ToList();
                if (elementos.Count == 0)
                    throw new ArgumentException("Ningún texto tiene su centro dentro de la región [" + string.Join(", ",
                        region.Select(v => v.ToString(System.Globalization.CultureInfo.InvariantCulture)))
                        + "] (unidades de página). Textos y sus centros: " + string.Join(" | ", todosLosTextos.Select(e =>
                        "\"" + Recortar(Texto(e)) + "\" " + CentroTexto(e.Elemento, doc))));
            }

            // Con 'patron' y sin otro selector, los candidatos son los textos que casan.
            if (patron != null && string.IsNullOrEmpty(nombre) && buscar == null)
            {
                var casan = elementos.Where(p => patron.IsMatch(Texto(p))).ToList();
                if (casan.Count == 0)
                    throw new ArgumentException("Ningún texto del layout casa con el patrón '" + patron + "'. Ojo: el"
                        + " patrón ve también las etiquetas de formato (<BOL>, <FNT ...>).");
                return Protocol.Result(Sustituir(todos ? FiltrarPorGrupo(casan, parameters, "que casan con el patrón")
                    : new List<ElementoLayout> {
                    Desempatar(casan, parameters, "que casan con el patrón") }, patron, reemplazo, aspecto, gc, doc));
            }

            ElementoLayout objetivo = null;

            if (!string.IsNullOrEmpty(nombre))
            {
                // Mismo criterio que con las capas (MapHandlers.FindLayer): con dos
                // elementos del mismo nombre NO se elige por el usuario. Es más probable
                // desde que se desciende a los grupos: un cajetín copiado de otro plano
                // arrastra sus nombres, y quedarse con el primero en silencio cambia el
                // texto equivocado en una serie entera sin que nada avise.
                var homonimos = elementos
                    .Where(p => string.Equals(p.Nombre, nombre, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                if (homonimos.Count > 0 && patron != null && todos)
                    return Protocol.Result(Sustituir(FiltrarPorGrupo(homonimos, parameters, "llamados '" + nombre + "'"),
                                                     patron, reemplazo, aspecto, gc, doc));
                if (homonimos.Count > 0)
                    objetivo = Desempatar(homonimos, parameters, "llamados '" + nombre + "'");
                if (objetivo == null)
                {
                    string disponibles = string.Join(", ",
                        elementos.Where(p => !string.IsNullOrEmpty(p.Nombre)).Select(p => p.Nombre));
                    throw new ArgumentException(
                        "Elemento de texto no encontrado por nombre: " + nombre
                        + ". Nombrados disponibles: "
                        + (disponibles.Length > 0 ? disponibles : "(ninguno)"));
                }
            }
            else if (buscar != null)
            {
                var exactos = elementos.Where(p => Texto(p) == buscar).ToList();
                var candidatos = exactos.Count > 0
                    ? exactos
                    : elementos.Where(p => Texto(p).Contains(buscar)).ToList();
                if (candidatos.Count == 0)
                    throw new ArgumentException("Ningún elemento de texto coincide con: " + buscar);
                if (patron != null && todos)
                    return Protocol.Result(Sustituir(FiltrarPorGrupo(candidatos, parameters,
                        "que coinciden con '" + buscar + "'"), patron, reemplazo, aspecto, gc, doc));
                objetivo = Desempatar(candidatos, parameters, "que coinciden con '" + buscar + "'");
            }
            else if (region != null)
            {
                objetivo = Desempatar(elementos, parameters, "con el centro en la región");
            }
            else
            {
                throw new ArgumentException(
                    "Indica 'nombre' (name del elemento), 'buscar' (su texto actual), 'patron' o 'region'.");
            }

            if (patron != null)
                return Protocol.Result(Sustituir(new List<ElementoLayout> { objetivo }, patron, reemplazo,
                                                 aspecto, gc, doc));

            ITextElement te = (ITextElement)objetivo.Elemento;
            string anterior = te.Text;
            JObject geoAntes = Geometria(objetivo.Elemento, te, aspecto);
            var foto = new FotoTexto(objetivo.Elemento, te);
            try
            {
                if (texto != null)
                    te.Text = texto;
                CambiarAspecto(objetivo.Elemento, te, aspecto);
            }
            catch
            {
                foto.Restaurar();
                throw;
            }

            // UpdateElement sobre el elemento de PRIMER NIVEL: sin esto el cambio no
            // se consolida en el contenedor de gráficos y el texto viejo reaparece al
            // redibujar o al exportar. Para un texto agrupado, el que conoce el
            // contenedor es el grupo, no el texto.
            try { gc.UpdateElement(objetivo.Raiz); }
            catch { /* elemento sin contenedor propio: el refresco de abajo basta */ }

            // Refrescar la capa de gráficos del layout y la vista activa.
            doc.RefrescarGraficosLayout();
            doc.Refrescar();

            var r = new JObject
            {
                ["nombre"] = objetivo.Nombre,
                ["grupo"] = objetivo.Grupo,
                ["texto_anterior"] = anterior,
                ["texto_nuevo"] = te.Text
            };
            // Leído de vuelta: con un selector por texto es fácil mover el elemento que
            // no era, y esto deja verlo sin exportar.
            if (aspecto.AlgoQueCambiar)
            {
                r["antes"] = geoAntes;
                r["ahora"] = Geometria(objetivo.Elemento, te, aspecto);
                if (aspecto.Encaje != null)
                    r["encaje"] = aspecto.Encaje;
                try { r["unidades_pagina"] = doc.PageLayout.Page.Units.ToString().Replace("esri", ""); }
                catch { }
            }
            return Protocol.Result(r);
        }

        /// <summary>Texto, símbolo y geometría de un elemento antes de tocarlo, para dejarlo como
        /// estaba si la operación falla a mitad. Es todo lo que set_text_element cambia.</summary>
        private sealed class FotoTexto
        {
            private readonly IElement el;
            private readonly ITextElement te;
            private readonly string texto;
            private readonly ITextSymbol simbolo;
            private readonly IGeometry geometria;

            public FotoTexto(IElement el, ITextElement te)
            {
                this.el = el;
                this.te = te;
                texto = te.Text;
                simbolo = (ITextSymbol)((IClone)te.Symbol).Clone();
                geometria = (IGeometry)((IClone)el.Geometry).Clone();
            }

            public void Restaurar()
            {
                try
                {
                    te.Text = texto;
                    te.Symbol = simbolo;
                    el.Geometry = geometria;
                }
                catch { /* se relanza el error original, que es el que explica qué pasó */ }
            }
        }

        /// <summary>Lo que set_text_element cambia del aspecto de un texto, aparte del texto.</summary>
        private sealed class Aspecto
        {
            public double? X, Y, Tamano;
            public esriTextCase? Mayusculas;
            public double[] CentrarEn, EncajarEn;
            public double Margen, TamanoMin;
            public Documento Doc;
            /// <summary>Resultado del encaje del último texto tratado.</summary>
            public JObject Encaje;

            public bool AlgoQueCambiar
            {
                get { return X != null || Y != null || Tamano != null || Mayusculas != null || CentrarEn != null || EncajarEn != null; }
            }

            public bool Mide
            {
                get { return CentrarEn != null || EncajarEn != null; }
            }
        }

        private static esriTextCase? LeerMayusculas(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            switch (((string)t ?? "").Trim().ToLowerInvariant())
            {
                case "normal": return esriTextCase.esriTCNormal;
                case "mayusculas": case "mayúsculas": return esriTextCase.esriTCAllCaps;
                case "minusculas": case "minúsculas": return esriTextCase.esriTCLowercase;
                case "versalitas": return esriTextCase.esriTCSmallCaps;
            }
            throw new ArgumentException("'mayusculas' es \"normal\" (el texto tal cual se escribe), \"mayusculas\","
                + " \"minusculas\" o \"versalitas\".");
        }

        private static string NombreMayusculas(esriTextCase c)
        {
            switch (c)
            {
                case esriTextCase.esriTCAllCaps: return "mayusculas";
                case esriTextCase.esriTCLowercase: return "minusculas";
                case esriTextCase.esriTCSmallCaps: return "versalitas";
                default: return "normal";
            }
        }

        /// <summary>[xmin, ymin, xmax, ymax] en unidades de página, o null si no viene.</summary>
        private static double[] LeerRecuadro(JToken t, string nombre)
        {
            if (!Parametros.Dado(t))
                return null;
            JArray a = t as JArray;
            if (a == null || a.Count != 4)
                throw new ArgumentException("'" + nombre + "' es [xmin, ymin, xmax, ymax] en unidades de página.");
            double[] v = a.Select(x => (double)x).ToArray();
            if (v[2] <= v[0] || v[3] <= v[1])
                throw new ArgumentException("'" + nombre + "': xmax tiene que ser mayor que xmin, e ymax que ymin.");
            return v;
        }

        /// <summary>Lo que ocupa el texto DIBUJADO (QueryBounds), no su geometría de anclaje.</summary>
        private static IEnvelope Limites(IElement el, Documento doc)
        {
            IEnvelope env = new EnvelopeClass();
            el.QueryBounds(doc.PantallaLayout(), env);
            return env;
        }

        private static bool CentroDentro(IElement el, double[] r, Documento doc)
        {
            IEnvelope e = Limites(el, doc);
            double cx = (e.XMin + e.XMax) / 2, cy = (e.YMin + e.YMax) / 2;
            return cx >= r[0] && cx <= r[2] && cy >= r[1] && cy <= r[3];
        }

        private static string CentroTexto(IElement el, Documento doc)
        {
            try
            {
                IEnvelope e = Limites(el, doc);
                var inv = System.Globalization.CultureInfo.InvariantCulture;
                return "(" + Math.Round((e.XMin + e.XMax) / 2, 2).ToString(inv) + ", "
                    + Math.Round((e.YMin + e.YMax) / 2, 2).ToString(inv) + ")";
            }
            catch { return "(?)"; }
        }

        private static string Recortar(string s)
        {
            s = (s ?? "").Replace("\r", " ").Replace("\n", " ");
            return s.Length > 40 ? s.Substring(0, 40) + "…" : s;
        }

        private static void PonerTamano(ITextElement te, double tamano)
        {
            // Sobre una copia del símbolo y reasignada: la fuente, el color y el halo se
            // quedan como estaban.
            ITextSymbol ts = (ITextSymbol)((IClone)te.Symbol).Clone();
            ts.Size = tamano;
            te.Symbol = ts;
        }

        private static void CambiarAspecto(IElement el, ITextElement te, Aspecto a)
        {
            if (a.Tamano != null)
                PonerTamano(te, a.Tamano.Value);
            if (a.Mayusculas != null)
            {
                // Las mayúsculas «forzadas» son una propiedad del símbolo, no del texto: el texto
                // escrito en minúsculas se ve en mayúsculas (serie de montes, 88 cajetines).
                ITextSymbol ts = (ITextSymbol)((IClone)te.Symbol).Clone();
                IFormattedTextSymbol fts = ts as IFormattedTextSymbol;
                if (fts == null)
                    throw new ArgumentException("El símbolo de este texto no admite mayúsculas/minúsculas.");
                fts.Case = a.Mayusculas.Value;
                te.Symbol = ts;
            }
            if (a.EncajarEn != null)
            {
                // Bajar la letra de 0,25 en 0,25 hasta que quepa en el recuadro menos el margen,
                // sin bajar de tamano_min: lo de rn2000_plano1.py de una serie real.
                double ancho = a.EncajarEn[2] - a.EncajarEn[0] - 2 * a.Margen;
                double alto = a.EncajarEn[3] - a.EncajarEn[1] - 2 * a.Margen;
                if (ancho <= 0 || alto <= 0)
                    throw new ArgumentException("'encajar_en' menos dos veces 'margen' no deja sitio.");
                double tam = te.Symbol.Size;
                double inicial = tam;
                IEnvelope e = Limites(el, a.Doc);
                while ((e.Width > ancho || e.Height > alto) && tam - 0.25 >= a.TamanoMin - 1e-9)
                {
                    // Al múltiplo de 0,25 inmediatamente inferior: de 9,0097 a 9, luego 8,75...
                    tam = Math.Ceiling(tam / 0.25 - 1 - 1e-9) * 0.25;
                    PonerTamano(te, tam);
                    e = Limites(el, a.Doc);
                }
                bool cabe = e.Width <= ancho && e.Height <= alto;
                a.Encaje = new JObject
                {
                    ["cabe"] = cabe,
                    ["tamano_inicial"] = inicial,
                    ["tamano"] = tam,
                    ["ancho"] = Math.Round(e.Width, 3),
                    ["alto"] = Math.Round(e.Height, 3),
                    ["sitio"] = new JArray(Math.Round(ancho, 3), Math.Round(alto, 3))
                };
                if (!cabe)
                    // `tam` no siempre es tamano_min: se baja de 0,25 en 0,25 desde el inicial y se para
                    // en el último que no pasa de él (revisión del 2026-10-25).
                    a.Encaje["aviso"] = "Ni a " + tam + " pt cabe (tamano_min = " + a.TamanoMin + "): se queda así."
                        + " Acorta el texto o baja tamano_min.";
                // Encajar sin centrar deja el texto donde estaba: se centra en el recuadro.
                if (a.CentrarEn == null && a.X == null && a.Y == null)
                    Centrar(el, a.EncajarEn, a.Doc);
            }
            if (a.X != null || a.Y != null)
            {
                // x/y es el punto de ANCLAJE del texto (el que se ve en «Tamaño y
                // posición» de ArcMap), en unidades de página. Se mueve por diferencia
                // con ITransform2D, que vale igual para un texto suelto y para uno de
                // párrafo (cuya geometría es un polígono, no un punto).
                IPoint ancla = Ancla(el);
                double dx = a.X != null ? a.X.Value - ancla.X : 0;
                double dy = a.Y != null ? a.Y.Value - ancla.Y : 0;
                ((ITransform2D)el).Move(dx, dy);
            }
            if (a.CentrarEn != null)
                Centrar(el, a.CentrarEn, a.Doc);
        }

        /// <summary>Mueve el texto para que el centro de lo DIBUJADO caiga en el del recuadro.</summary>
        private static void Centrar(IElement el, double[] r, Documento doc)
        {
            IEnvelope e = Limites(el, doc);
            double dx = (r[0] + r[2]) / 2 - (e.XMin + e.XMax) / 2;
            double dy = (r[1] + r[3]) / 2 - (e.YMin + e.YMax) / 2;
            ((ITransform2D)el).Move(dx, dy);
        }

        /// <summary>
        /// set_text_element con 'patron' + 'reemplazo': sustituye la parte que casa en cada
        /// elemento (expresión regular de .NET; los grupos van como $1 en el reemplazo). Un
        /// elemento elegido en el que el patrón no casa es error, no un «ok» sin cambios.
        /// Se comprueba en TODOS antes de tocar ninguno: o cambian todos, o ninguno.
        /// </summary>
        private static JObject Sustituir(List<ElementoLayout> objetivos, System.Text.RegularExpressions.Regex patron,
                                         string reemplazo, Aspecto aspecto,
                                         IGraphicsContainer gc, Documento doc)
        {
            var sinCasar = objetivos.Where(o => !patron.IsMatch(Texto(o))).ToList();
            if (sinCasar.Count > 0)
                throw new ArgumentException("El patrón '" + patron + "' no casa con " + sinCasar.Count + " de los "
                    + objetivos.Count + " elementos elegidos: " + string.Join(" | ", sinCasar.Select(o => "\"" + Texto(o)
                    + "\"")) + ". No se ha cambiado ninguno.");
            var cambios = new JArray();
            var raices = new List<IElement>();
            // «O cambian todos, o ninguno» también si CambiarAspecto lanza a mitad (un símbolo sin
            // mayúsculas, un encajar_en sin sitio): se deshacen los ya tocados (revisión 2026-10-25).
            var fotos = new List<FotoTexto>();
            foreach (ElementoLayout o in objetivos)
            {
                ITextElement te = (ITextElement)o.Elemento;
                string antes = te.Text;
                fotos.Add(new FotoTexto(o.Elemento, te));
                try
                {
                    te.Text = patron.Replace(antes, reemplazo);
                    CambiarAspecto(o.Elemento, te, aspecto);
                }
                catch
                {
                    for (int k = fotos.Count - 1; k >= 0; k--)
                        fotos[k].Restaurar();
                    throw;
                }
                if (!raices.Any(ya => ReferenceEquals(ya, o.Raiz)))
                    raices.Add(o.Raiz);
                cambios.Add(new JObject
                {
                    ["nombre"] = o.Nombre,
                    ["grupo"] = o.Grupo,
                    ["antes"] = antes,
                    ["ahora"] = te.Text
                });
            }
            foreach (IElement raiz in raices)
            {
                try { gc.UpdateElement(raiz); }
                catch { /* sin contenedor propio: basta el refresco */ }
            }
            doc.RefrescarGraficosLayout();
            doc.Refrescar();
            var r = new JObject { ["cambiados"] = cambios.Count, ["elementos"] = cambios };
            if (cambios.Count == 1)
            {
                r["texto_anterior"] = cambios[0]["antes"];
                r["texto_nuevo"] = cambios[0]["ahora"];
            }
            return r;
        }

        private static IPoint Ancla(IElement el)
        {
            IPoint p = el.Geometry as IPoint;
            if (p != null)
                return p;
            IEnvelope env = el.Geometry.Envelope;
            return env.LowerLeft;
        }

        private static JObject Geometria(IElement el, ITextElement te, Aspecto aspecto)
        {
            var g = new JObject();
            try
            {
                IPoint a = Ancla(el);
                g["x"] = Math.Round(a.X, 3);
                g["y"] = Math.Round(a.Y, 3);
            }
            catch { }
            try { g["tamano"] = te.Symbol.Size; } catch { }
            IFormattedTextSymbol fts = te.Symbol as IFormattedTextSymbol;
            if (fts != null)
                g["mayusculas"] = NombreMayusculas(fts.Case);
            if (aspecto != null && aspecto.Mide)
            {
                try
                {
                    IEnvelope e = Limites(el, aspecto.Doc);
                    g["dibujado"] = new JArray(Math.Round(e.XMin, 3), Math.Round(e.YMin, 3),
                                               Math.Round(e.XMax, 3), Math.Round(e.YMax, 3));
                }
                catch { }
            }
            return g;
        }

        private static string Texto(ElementoLayout e)
        {
            return ((ITextElement)e.Elemento).Text ?? "";
        }

        // ------------------------------------------------------------------ leyendas

        /// <summary>Leyendas del layout que dibujan este mapa (también las que viven
        /// dentro de un grupo de elementos).</summary>
        internal static List<ILegend> LeyendasDelMapa(Documento doc, IMap map)
        {
            var salida = new List<ILegend>();
            foreach (ElementoLayout e in Elementos((IGraphicsContainer)doc.PageLayout))
            {
                ILegend lg = Leyenda(e);
                if (lg != null && ReferenceEquals(lg.Map, map))
                    salida.Add(lg);
            }
            return salida;
        }

        /// <summary>Reconstruye las leyendas que dibujan este mapa y las consolida en el
        /// layout (Refresh + UpdateElement del elemento de primer nivel, como en
        /// set_legend_item): sin el UpdateElement, el export sale con la leyenda vieja.
        /// Devuelve cuántas.</summary>
        internal static int RefrescarLeyendas(Documento doc, IMap map)
        {
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            int n = 0;
            foreach (ElementoLayout e in Elementos(gc))
            {
                ILegend lg = Leyenda(e);
                if (lg == null || !ReferenceEquals(lg.Map, map))
                    continue;
                lg.Refresh();
                try { gc.UpdateElement(e.Raiz); }
                catch { /* sin contenedor propio: basta el refresco */ }
                n++;
            }
            if (n > 0)
                doc.RefrescarGraficosLayout();
            return n;
        }

        /// <summary>
        /// Pone en cada leyenda de este mapa las entradas de `capa` (y, si es un grupo, las de
        /// todo lo que lleva dentro, en bloque y en su orden) donde dice la TOC: detrás de la
        /// entrada de la capa más cercana que tiene ENCIMA en la TOC, o delante de la de debajo
        /// si encima no hay ninguna con entrada. La leyenda no sigue a la TOC al mover capas
        /// (serie real, 2026-10-06), y hasta ahora hacía falta un set_legend_item(despues_de) por
        /// cada movimiento. Se reinsertan los MISMOS objetos: conservan formato e interruptores.
        /// </summary>
        internal static JArray ColocarEntradasSegunToc(Documento doc, IMap map, ILayer capa)
        {
            var toc = MapHandlers.CapasConRuta(map).Select(c => c.Capa).ToList();
            int pos = toc.FindIndex(l => ReferenceEquals(l, capa));
            var bloque = new List<ILayer> { capa };
            for (int i = pos + 1; i < toc.Count && EstaDentro(toc[i], capa); i++)
                bloque.Add(toc[i]);
            Func<ILayer, bool> enBloque = l => bloque.Any(b => ReferenceEquals(b, l));

            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            var salida = new JArray();
            int sinNombre = 0;
            foreach (ElementoLayout e in Elementos(gc))
            {
                ILegend lg = Leyenda(e);
                if (lg == null)
                    continue;
                string nombreLeyenda = e.Nombre.Length > 0 ? e.Nombre : "(leyenda sin nombre #" + (++sinNombre) + ")";
                if (!ReferenceEquals(lg.Map, map))
                    continue;
                var mias = new List<ILegendItem>();
                for (int k = 0; k < lg.ItemCount; k++)
                    if (lg.get_Item(k).Layer != null && enBloque(lg.get_Item(k).Layer))
                        mias.Add(lg.get_Item(k));
                if (mias.Count == 0)
                {
                    salida.Add(new JObject { ["leyenda"] = nombreLeyenda, ["aviso"] = "la capa no tiene entrada en esta leyenda" });
                    continue;
                }
                // Fuera las del bloque; el resto de la leyenda queda en su orden.
                for (int k = lg.ItemCount - 1; k >= 0; k--)
                    if (lg.get_Item(k).Layer != null && enBloque(lg.get_Item(k).Layer))
                        lg.RemoveItem(k);
                int destino = -1;
                for (int i = pos - 1; i >= 0 && destino < 0; i--)
                    destino = IndiceEntrada(lg, toc[i], 1);
                for (int i = pos + bloque.Count; i < toc.Count && destino < 0; i++)
                    destino = IndiceEntrada(lg, toc[i], 0);
                if (destino < 0)
                    destino = 0;
                for (int j = 0; j < mias.Count; j++)
                    lg.InsertItem(destino + j, mias[j]);
                lg.Refresh();
                try { gc.UpdateElement(e.Raiz); }
                catch { /* sin contenedor propio: basta el refresco */ }
                salida.Add(new JObject
                {
                    ["leyenda"] = nombreLeyenda,
                    ["posicion"] = destino + 1,
                    ["entradas_movidas"] = mias.Count,
                    ["orden"] = Orden(lg)
                });
            }
            doc.RefrescarGraficosLayout();
            doc.Refrescar();
            return salida;
        }

        /// <summary>Índice de la ÚLTIMA entrada de `capa` en la leyenda más `desplazamiento`
        /// (1 = justo detrás); -1 si no tiene.</summary>
        private static int IndiceEntrada(ILegend lg, ILayer capa, int desplazamiento)
        {
            int ultima = -1;
            for (int k = 0; k < lg.ItemCount; k++)
                if (ReferenceEquals(lg.get_Item(k).Layer, capa))
                    ultima = k;
            if (ultima < 0)
                return -1;
            if (desplazamiento == 0)
            {
                for (int k = 0; k < lg.ItemCount; k++)
                    if (ReferenceEquals(lg.get_Item(k).Layer, capa))
                        return k;
            }
            return ultima + desplazamiento;
        }

        private static bool EstaDentro(ILayer capa, ILayer grupo)
        {
            ICompositeLayer comp = grupo as ICompositeLayer;
            if (comp == null)
                return false;
            for (int i = 0; i < comp.Count; i++)
            {
                ILayer h = comp.get_Layer(i);
                if (ReferenceEquals(h, capa) || EstaDentro(capa, h))
                    return true;
            }
            return false;
        }

        private static ILegend Leyenda(ElementoLayout e)
        {
            IMapSurroundFrame msf = e.Elemento as IMapSurroundFrame;
            return msf != null ? msf.MapSurround as ILegend : null;
        }

        private static bool? LeerInterruptor(JObject parameters, string nombre)
        {
            JToken t = parameters[nombre];
            if (t == null || t.Type == JTokenType.Null)
                return null;
            return Parametros.LeerBool(t, nombre, false);
        }

        private sealed class ItemEnLeyenda
        {
            public ElementoLayout Elemento;
            public string NombreLeyenda;
            public ILegend Leyenda;
            public int Indice;
            public ILegendItem Item;
        }

        /// <summary>
        /// set_legend_item — cambia QUÉ muestra la entrada de una capa en la leyenda
        /// (nombre de capa, encabezado, etiquetas de clase) sin tocar CÓMO se ve.
        ///
        /// La vía de arcpy (`legend.updateItem` con un estilo de ESRI.style) aplica el
        /// estilo entero: en el 23_02 de una serie de montes puso el nombre en negrita grande y los
        /// años pequeños, y hubo que revertir. Aquí solo se tocan los interruptores de
        /// ILegendItem; los símbolos de texto no se escriben, y se devuelven leídos
        /// para que se vea que no han cambiado.
        /// </summary>
        public static JObject SetLegendItem(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa': el nombre de la capa tal como aparece en la leyenda.");
            string filtroLeyenda = (string)parameters["leyenda"];
            bool? mostrarNombre = LeerInterruptor(parameters, "mostrar_nombre");
            bool? mostrarEncabezado = LeerInterruptor(parameters, "mostrar_encabezado");
            bool? mostrarEtiquetas = LeerInterruptor(parameters, "mostrar_etiquetas");
            // «Colocar en columna nueva»: se pierde al sustituir una capa (la entrada de
            // leyenda nueva nace sin él) y la leyenda del Plano 11 de un expediente real pasó de
            // dos columnas a una, bajo la tabla (2026-10-01).
            bool? columnaNueva = LeerInterruptor(parameters, "columna_nueva");
            // Quitar la entrada sin quitar la capa del mapa, y moverla dentro de la
            // leyenda: la leyenda no sigue la TOC al mover capas (serie real, 2026-10-06).
            bool quitar = LeerInterruptor(parameters, "quitar") == true;
            JToken tPosicion = parameters["posicion"];
            bool hayPosicion = tPosicion != null && tPosicion.Type != JTokenType.Null;
            string despuesDe = (string)parameters["despues_de"];
            bool hayDespuesDe = !string.IsNullOrEmpty(despuesDe);
            if (hayPosicion && hayDespuesDe)
                throw new ArgumentException("Indica 'posicion' o 'despues_de', no los dos.");
            if (quitar && (hayPosicion || hayDespuesDe || mostrarNombre != null || mostrarEncabezado != null
                           || mostrarEtiquetas != null || columnaNueva != null))
                throw new ArgumentException("'quitar' va solo: una entrada que se quita no se cambia ni se mueve.");

            Documento doc = Documento.Actual();
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;

            var leyendas = new List<Tuple<ElementoLayout, string>>();
            int n = 0;
            foreach (ElementoLayout e in Elementos(gc))
                if (Leyenda(e) != null)
                    leyendas.Add(Tuple.Create(e, e.Nombre.Length > 0 ? e.Nombre : "(leyenda sin nombre #" + (++n) + ")"));
            if (leyendas.Count == 0)
                throw new ArgumentException("El layout no tiene ninguna leyenda.");
            if (!string.IsNullOrEmpty(filtroLeyenda))
            {
                var filtradas = leyendas
                    .Where(l => string.Equals(l.Item2, filtroLeyenda, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                if (filtradas.Count == 0)
                    throw new ArgumentException("No hay ninguna leyenda llamada '" + filtroLeyenda
                        + "'. Leyendas del layout: " + string.Join(", ", leyendas.Select(l => l.Item2)));
                leyendas = filtradas;
            }

            var candidatos = new List<ItemEnLeyenda>();
            var contenido = new List<string>();
            foreach (var l in leyendas)
            {
                ILegend lg = Leyenda(l.Item1);
                var nombres = new List<string>();
                for (int k = 0; k < lg.ItemCount; k++)
                {
                    ILegendItem it = lg.get_Item(k);
                    string nombreCapa = it.Layer != null ? it.Layer.Name : "";
                    nombres.Add(nombreCapa);
                    if (string.Equals(nombreCapa, capa, StringComparison.OrdinalIgnoreCase))
                        candidatos.Add(new ItemEnLeyenda
                        {
                            Elemento = l.Item1, NombreLeyenda = l.Item2, Leyenda = lg, Indice = k, Item = it
                        });
                }
                contenido.Add(l.Item2 + ": " + (nombres.Count > 0 ? string.Join(" | ", nombres) : "(vacía)"));
            }

            if (candidatos.Count == 0)
                throw new ArgumentException("La capa '" + capa + "' no tiene entrada en "
                    + (string.IsNullOrEmpty(filtroLeyenda) ? "ninguna leyenda" : "la leyenda '" + filtroLeyenda + "'")
                    + ". Contenido: " + string.Join(" · ", contenido));

            ItemEnLeyenda objetivo;
            JToken tIndice = parameters["indice"];
            // En varias leyendas (una por data frame, a menudo con el mismo nombre): sin más
            // indicación, la del data frame activo si es una sola, como en set_legend.
            if (candidatos.Count > 1 && !Parametros.Dado(tIndice) && string.IsNullOrEmpty(filtroLeyenda))
            {
                IMap activo = doc.FocusMap;
                var delActivo = candidatos.Where(c => ReferenceEquals(c.Leyenda.Map, activo)).ToList();
                if (delActivo.Count == 1)
                    candidatos = delActivo;
            }
            if (candidatos.Count == 1)
                objetivo = candidatos[0];
            else if (tIndice != null && tIndice.Type != JTokenType.Null)
                objetivo = candidatos[Parametros.LeerEntero(tIndice, "indice", 1, 1, candidatos.Count) - 1];
            else
            {
                int i = 0;
                throw new ArgumentException("La capa '" + capa + "' tiene " + candidatos.Count
                    + " entradas de leyenda y no se elige por ti. Indica 'leyenda' o pasa 'indice' con el"
                    + " número de esta lista: " + string.Join(" | ", candidatos.Select(c =>
                        "[" + (++i) + "] " + c.NombreLeyenda + ", posición " + (c.Indice + 1))));
            }

            if (quitar)
            {
                ILegend lgq = objetivo.Leyenda;
                lgq.RemoveItem(objetivo.Indice);
                lgq.Refresh();
                try { gc.UpdateElement(objetivo.Elemento.Raiz); }
                catch { /* sin contenedor propio: basta el refresco */ }
                doc.RefrescarGraficosLayout();
                doc.Refrescar();
                return Protocol.Result(new JObject
                {
                    ["leyenda"] = objetivo.NombreLeyenda,
                    ["capa"] = capa,
                    ["quitada"] = true,
                    ["posicion_antes"] = objetivo.Indice + 1,
                    ["capa_sigue_en_el_mapa"] = true,
                    ["orden"] = Orden(lgq)
                });
            }

            // Índice de destino (base 0) en la leyenda YA sin la entrada.
            int? destino = null;
            if (hayPosicion)
            {
                int total = objetivo.Leyenda.ItemCount;
                destino = Parametros.LeerEntero(tPosicion, "posicion", 1, 1, total) - 1;
            }
            else if (hayDespuesDe)
            {
                var refs = new List<int>();
                for (int k = 0; k < objetivo.Leyenda.ItemCount; k++)
                {
                    ILegendItem it = objetivo.Leyenda.get_Item(k);
                    if (k != objetivo.Indice && it.Layer != null
                        && string.Equals(it.Layer.Name, despuesDe, StringComparison.OrdinalIgnoreCase))
                        refs.Add(k);
                }
                if (refs.Count == 0)
                    throw new ArgumentException("'" + despuesDe + "' no tiene entrada en la leyenda '"
                        + objetivo.NombreLeyenda + "'. Orden actual: " + string.Join(" | ", Orden(objetivo.Leyenda)));
                if (refs.Count > 1)
                    throw new ArgumentException("'" + despuesDe + "' tiene " + refs.Count + " entradas en la leyenda '"
                        + objetivo.NombreLeyenda + "' y no se elige por ti: usa 'posicion'. Orden actual: "
                        + string.Join(" | ", Orden(objetivo.Leyenda)));
                int j = refs[0];
                destino = (j < objetivo.Indice ? j : j - 1) + 1;
            }

            if (mostrarNombre == null && mostrarEncabezado == null && mostrarEtiquetas == null
                && columnaNueva == null && destino == null)
                return Protocol.Result(EstadoItem(objetivo, null));

            JObject antes = Interruptores(objetivo.Item);
            antes["posicion_en_leyenda"] = objetivo.Indice + 1;
            if (mostrarNombre.HasValue) objetivo.Item.ShowLayerName = mostrarNombre.Value;
            if (mostrarEncabezado.HasValue) objetivo.Item.ShowHeading = mostrarEncabezado.Value;
            if (mostrarEtiquetas.HasValue) objetivo.Item.ShowLabels = mostrarEtiquetas.Value;
            if (columnaNueva.HasValue) objetivo.Item.NewColumn = columnaNueva.Value;
            if (destino.HasValue && destino.Value != objetivo.Indice)
            {
                // Se reinserta el MISMO objeto: conserva interruptores y formato.
                objetivo.Leyenda.RemoveItem(objetivo.Indice);
                objetivo.Leyenda.InsertItem(destino.Value, objetivo.Item);
                // Leído de vuelta: la entrada que hay en el destino tiene que ser la de esta capa.
                ILegendItem enDestino = objetivo.Leyenda.get_Item(destino.Value);
                if (enDestino == null || !ReferenceEquals(enDestino.Layer, objetivo.Item.Layer))
                    throw new InvalidOperationException("La entrada no ha quedado en la posición "
                        + (destino.Value + 1) + ". Orden actual: " + string.Join(" | ", Orden(objetivo.Leyenda)));
                objetivo.Indice = destino.Value;
            }

            // Refresh reconstruye la leyenda con los interruptores nuevos; sin el
            // UpdateElement del elemento de primer nivel, el cambio no se consolida
            // y el export sale con la leyenda vieja (mismo motivo que set_text_element).
            objetivo.Leyenda.Refresh();
            try { gc.UpdateElement(objetivo.Elemento.Raiz); }
            catch { /* sin contenedor propio: basta el refresco */ }
            doc.RefrescarGraficosLayout();
            doc.Refrescar();

            return Protocol.Result(EstadoItem(objetivo, antes));
        }

        /// <summary>
        /// set_legend — lo que es del ELEMENTO leyenda, no de una entrada: tamaño de letra
        /// de todas sus entradas, número de columnas y encajar el marco en un recuadro sin
        /// escalar la letra.
        ///
        /// Por qué no `elementHeight` de arcpy: escala la letra (de 9,75 a ~6 pt en una
        /// serie real, 2026-10-06), y los planos exigen ≥ 8 pt. Aquí la letra se fija y el
        /// marco se pone al tamaño NATURAL de la leyenda, medido.
        ///
        /// Los getters de ILegendItem (LayerNameSymbol, HeadingSymbol, LegendClassFormat
        /// y sus símbolos) devuelven COPIAS: hay que modificar y volver a asignar, o se
        /// guarda sin error y no cambia nada. Por eso los tamaños se devuelven leídos.
        /// </summary>
        public static JObject SetLegend(JObject parameters)
        {
            string filtroLeyenda = (string)parameters["leyenda"];
            double? tamano = Parametros.LeerDouble(parameters["tamano"], "tamano", 4, 72);
            JToken tColumnas = parameters["columnas"];
            int? columnas = Parametros.Dado(tColumnas)
                ? Parametros.LeerEntero(tColumnas, "columnas", 1, 1, 20) : (int?)null;
            double[] recuadro = null;
            JToken tRecuadro = parameters["recuadro"];
            if (Parametros.Dado(tRecuadro))
            {
                JArray a = tRecuadro as JArray;
                if (a == null || a.Count != 4)
                    throw new ArgumentException("'recuadro' es [xmin, ymin, xmax, ymax] en unidades de página.");
                recuadro = a.Select(v => (double)v).ToArray();
                if (recuadro[2] <= recuadro[0] || recuadro[3] <= recuadro[1])
                    throw new ArgumentException("'recuadro' mal formado: xmax debe ser mayor que xmin e ymax que ymin.");
            }
            // Formato (leyenda8_ao.py y grupo_nombre_ao.py de una serie real): fuente, negrita de nombres y
            // encabezados aparte de la de las etiquetas, tamaño de parche y separaciones.
            string fuente = Parametros.Dado(parameters["fuente"]) ? ((string)parameters["fuente"]).Trim() : null;
            if (fuente != null && fuente.Length == 0)
                throw new ArgumentException("'fuente' vacía.");
            bool? negritaNombres = Parametros.Dado(parameters["negrita_nombres"])
                ? Parametros.LeerBool(parameters["negrita_nombres"], "negrita_nombres", false) : (bool?)null;
            bool? negritaEtiquetas = Parametros.Dado(parameters["negrita_etiquetas"])
                ? Parametros.LeerBool(parameters["negrita_etiquetas"], "negrita_etiquetas", false) : (bool?)null;
            double[] parche = null;
            if (Parametros.Dado(parameters["parche"]))
            {
                JArray a = parameters["parche"] as JArray;
                if (a == null || a.Count != 2)
                    throw new ArgumentException("'parche' es [ancho, alto] en puntos: [16, 9].");
                parche = new[] { Parametros.LeerDouble(a[0], "parche[0]", 1, 200).Value,
                                 Parametros.LeerDouble(a[1], "parche[1]", 1, 200).Value };
            }
            var separaciones = new Dictionary<string, double>();
            if (Parametros.Dado(parameters["separaciones"]))
            {
                JObject sep = parameters["separaciones"] as JObject;
                if (sep == null || !sep.Properties().Any())
                    throw new ArgumentException("'separaciones' es un objeto, en puntos: {\"entre_entradas\": 3, ...}."
                        + " Claves: " + string.Join(", ", Separaciones.Keys) + ".");
                foreach (var p in sep.Properties())
                {
                    if (!Separaciones.ContainsKey(p.Name))
                        throw new ArgumentException("Separación desconocida '" + p.Name + "'. Claves: "
                            + string.Join(", ", Separaciones.Keys) + ".");
                    separaciones[p.Name] = Parametros.LeerDouble(p.Value, "separaciones." + p.Name, 0, 200).Value;
                }
            }
            bool hayFormato = fuente != null || negritaNombres != null || negritaEtiquetas != null || parche != null
                || separaciones.Count > 0;

            Documento doc = Documento.Actual();
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;

            var leyendas = new List<Tuple<ElementoLayout, string>>();
            int n = 0;
            foreach (ElementoLayout e in Elementos(gc))
                if (Leyenda(e) != null)
                    leyendas.Add(Tuple.Create(e, e.Nombre.Length > 0 ? e.Nombre : "(leyenda sin nombre #" + (++n) + ")"));
            if (leyendas.Count == 0)
                throw new ArgumentException("El layout no tiene ninguna leyenda.");
            if (!string.IsNullOrEmpty(filtroLeyenda))
                leyendas = leyendas
                    .Where(l => string.Equals(l.Item2, filtroLeyenda, StringComparison.OrdinalIgnoreCase))
                    .ToList();
            if (leyendas.Count == 0)
                throw new ArgumentException("No hay ninguna leyenda llamada '" + filtroLeyenda + "'.");
            // Varias, a menudo con el MISMO nombre ("Legend" y "Legend" en los planos de una serie real,
            // una por data frame): por nombre no se distinguían y ninguna era alcanzable.
            string elegida = null;
            JToken tIndice = parameters["indice"];
            if (leyendas.Count > 1 && Parametros.Dado(tIndice))
            {
                leyendas = new List<Tuple<ElementoLayout, string>> {
                    leyendas[Parametros.LeerEntero(tIndice, "indice", 1, 1, leyendas.Count) - 1] };
                elegida = "por 'indice'";
            }
            else if (leyendas.Count > 1)
            {
                IMap activo = doc.FocusMap;
                var delActivo = leyendas.Where(l => ReferenceEquals(Leyenda(l.Item1).Map, activo)).ToList();
                if (delActivo.Count == 1)
                {
                    leyendas = delActivo;
                    elegida = "la del data frame activo ('" + activo.Name + "'): el layout tiene más de una";
                }
            }
            if (leyendas.Count > 1)
            {
                int k = 0;
                throw new ArgumentException("El layout tiene " + leyendas.Count + " leyendas y no se elige por ti: "
                    + "indica 'leyenda' o pasa 'indice' con el número de esta lista: " + string.Join(" | ",
                        leyendas.Select(l => "[" + (++k) + "] " + l.Item2 + " (data frame '" + Leyenda(l.Item1).Map.Name
                            + "', esquina superior izquierda en x=" + Math.Round(l.Item1.Elemento.Geometry.Envelope.XMin, 2)
                            + ", y=" + Math.Round(l.Item1.Elemento.Geometry.Envelope.YMax, 2) + ", "
                            + Leyenda(l.Item1).ItemCount + " entradas)")));
            }

            ElementoLayout el = leyendas[0].Item1;
            string nombre = leyendas[0].Item2;
            ILegend lg = Leyenda(el);
            JObject antes = EstadoLeyenda(doc, el, lg);
            if (tamano == null && columnas == null && recuadro == null && !hayFormato)
            {
                antes["leyenda"] = nombre;
                if (elegida != null)
                    antes["elegida"] = elegida;
                return Protocol.Result(antes);
            }

            // Las leyendas de 10.1+ pueden tener «marco fijo» con «ajuste automático»: ArcMap
            // ENCOGE la letra al dibujar para que quepa. Con eso activo, el tamaño escrito no
            // es el dibujado (una leyenda «a 8 pt» sale a 6 en el JPG), así que fijar el tamaño
            // lo apaga. Visto en la primera prueba en vivo (2026-10-06).
            ILegend3 lg3 = lg as ILegend3;
            if (tamano.HasValue || fuente != null || negritaNombres != null || negritaEtiquetas != null || parche != null)
            {
                if (tamano.HasValue && lg3 != null && lg3.AutoFit)
                    lg3.AutoFit = false;
                for (int k = 0; k < lg.ItemCount; k++)
                {
                    ILegendItem it = lg.get_Item(k);
                    ITextSymbol s = it.LayerNameSymbol;
                    if (s != null) it.LayerNameSymbol = Formatear(s, tamano, fuente, negritaNombres);
                    s = it.HeadingSymbol;
                    if (s != null) it.HeadingSymbol = Formatear(s, tamano, fuente, negritaNombres);
                    ILegendClassFormat lcf = it.LegendClassFormat;
                    if (lcf != null)
                    {
                        s = lcf.LabelSymbol;
                        if (s != null) lcf.LabelSymbol = Formatear(s, tamano, fuente, negritaEtiquetas);
                        s = lcf.DescriptionSymbol;
                        if (s != null) lcf.DescriptionSymbol = Formatear(s, tamano, fuente, negritaEtiquetas);
                        if (parche != null)
                        {
                            lcf.PatchWidth = parche[0];
                            lcf.PatchHeight = parche[1];
                        }
                        it.LegendClassFormat = lcf;
                    }
                }
            }
            if (parche != null || separaciones.Count > 0)
            {
                // El formato de la leyenda: el parche por defecto (el de las entradas que entren
                // después) y las separaciones. Mismo cuidado que con los símbolos: se reasigna.
                ILegendFormat fmt = lg.Format;
                if (parche != null)
                {
                    fmt.DefaultPatchWidth = parche[0];
                    fmt.DefaultPatchHeight = parche[1];
                }
                foreach (var kv in separaciones)
                    Separaciones[kv.Key].Item2(fmt, kv.Value);
                lg.Format = fmt;
            }
            if (columnas.HasValue)
            {
                // Con columnas automáticas, ArcMap reparte a su aire y el número pedido no dura.
                if (lg3 != null && lg3.AutoColumns)
                    lg3.AutoColumns = false;
                ((ILegend2)lg).AdjustColumns(columnas.Value);
            }
            lg.Refresh();

            JObject encaje = null;
            if (recuadro != null)
            {
                // Sin marco fijo, el marco sigue al contenido y nunca lo escala.
                if (lg3 != null && lg3.FixedFrame)
                    lg3.FixedFrame = false;
                lg.Refresh();
                IEnvelope natural = TamanoNatural(doc, el);
                double w = natural.Width, h = natural.Height;
                // El marco al tamaño natural, arriba a la izquierda del recuadro: al
                // redibujar, la leyenda crece hacia abajo desde ese borde.
                IElementProperties3 ep = el.Elemento as IElementProperties3;
                if (ep != null)
                    ep.AnchorPoint = esriAnchorPointEnum.esriTopLeftCorner;
                IEnvelope env = new EnvelopeClass();
                env.PutCoords(recuadro[0], recuadro[3] - h, recuadro[0] + w, recuadro[3]);
                el.Elemento.Geometry = env;
                double anchoRec = recuadro[2] - recuadro[0], altoRec = recuadro[3] - recuadro[1];
                encaje = new JObject
                {
                    ["recuadro"] = new JArray(recuadro),
                    ["cabe_ancho"] = w <= anchoRec + 1e-6,
                    ["cabe_alto"] = h <= altoRec + 1e-6,
                    ["sobra_ancho"] = Math.Round(anchoRec - w, 3),
                    ["sobra_alto"] = Math.Round(altoRec - h, 3)
                };
            }

            try { gc.UpdateElement(el.Raiz); }
            catch { /* sin contenedor propio: basta el refresco */ }
            doc.RefrescarGraficosLayout();
            doc.Refrescar();

            JObject ahora = EstadoLeyenda(doc, el, lg);
            var r = new JObject
            {
                ["leyenda"] = nombre,
                ["cambiado"] = true,
                ["antes"] = antes,
                ["ahora"] = ahora
            };
            if (elegida != null)
                r["elegida"] = elegida;
            if (encaje != null)
            {
                encaje["cabe"] = (bool)encaje["cabe_ancho"] && (bool)encaje["cabe_alto"];
                r["encaje"] = encaje;
            }
            if (columnas.HasValue)
                r["aviso_columnas"] = "AdjustColumns reparte las entradas y reescribe el «colocar en columna nueva» de cada una.";
            return Protocol.Result(r);
        }

        /// <summary>Las separaciones de la leyenda con los nombres de la pestaña «Diseño» del diálogo
        /// de ArcMap («Espaciado entre»), en puntos: lectura y escritura sobre ILegendFormat.</summary>
        private static readonly Dictionary<string, Tuple<Func<ILegendFormat, double>, Action<ILegendFormat, double>>>
            Separaciones = new Dictionary<string, Tuple<Func<ILegendFormat, double>, Action<ILegendFormat, double>>>
            {
                // título y entradas
                { "titulo", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.TitleGap, (f, v) => f.TitleGap = v) },
                // entre una entrada (capa) y la siguiente
                { "entre_entradas", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.VerticalItemGap, (f, v) => f.VerticalItemGap = v) },
                // entre columnas
                { "entre_columnas", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.HorizontalItemGap, (f, v) => f.HorizontalItemGap = v) },
                // entre las clases de una entrada, en vertical
                { "entre_clases", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.VerticalPatchGap, (f, v) => f.VerticalPatchGap = v) },
                // entre el encabezado y las clases
                { "encabezado", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.HeadingGap, (f, v) => f.HeadingGap = v) },
                // entre el nombre de capa y lo que va debajo
                { "nombre_capa", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.LayerNameGap, (f, v) => f.LayerNameGap = v) },
                // entre grupos de clases
                { "grupos", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.GroupGap, (f, v) => f.GroupGap = v) },
                // entre el parche y su etiqueta
                { "parche_texto", Tuple.Create<Func<ILegendFormat, double>, Action<ILegendFormat, double>>(f => f.TextGap, (f, v) => f.TextGap = v) },
            };

        /// <summary>El símbolo de texto con lo pedido (tamaño, fuente, negrita). La fuente se modifica
        /// y se REASIGNA: el getter puede dar una copia. Una fuente que no está instalada es error.</summary>
        private static ITextSymbol Formatear(ITextSymbol s, double? tamano, string fuente, bool? negrita)
        {
            // El tamaño se pone DESPUÉS de reasignar la fuente: el IFontDisp lo redondea a píxeles
            // de pantalla (8 → 8,25; 8,5 → 8,25, por debajo de lo pedido), y pasar solo una negrita
            // movía un tamaño que nadie había pedido tocar (prueba desde el cliente, 2026-10-10).
            double tam = tamano ?? s.Size;
            if (fuente != null || negrita != null)
            {
                stdole.IFontDisp f = s.Font;
                if (fuente != null) f.Name = fuente;
                if (negrita != null) f.Bold = negrita.Value;
                s.Font = f;
                if (fuente != null && !string.Equals(s.Font.Name, fuente, StringComparison.OrdinalIgnoreCase))
                    throw new ArgumentException("La fuente '" + fuente + "' no está instalada (queda '" + s.Font.Name + "').");
            }
            s.Size = tam;
            return s;
        }

        /// <summary>Lo que dice el formato: fuentes y negritas que hay (de nombres y de etiquetas), el
        /// parche por defecto y las separaciones. Las fuentes se dan como lista de las distintas.</summary>
        internal static JObject FormatoLeyenda(ILegend lg)
        {
            var fNombres = new SortedSet<string>();
            var fEtiquetas = new SortedSet<string>();
            var bNombres = new HashSet<bool>();
            var bEtiquetas = new HashSet<bool>();
            var parches = new SortedSet<string>();
            for (int k = 0; k < lg.ItemCount; k++)
            {
                ILegendItem it = lg.get_Item(k);
                foreach (ITextSymbol s in new[] { it.LayerNameSymbol, it.HeadingSymbol })
                    if (s != null) { fNombres.Add(s.Font.Name); bNombres.Add(s.Font.Bold); }
                ILegendClassFormat lcf = it.LegendClassFormat;
                if (lcf == null)
                    continue;
                foreach (ITextSymbol s in new[] { lcf.LabelSymbol, lcf.DescriptionSymbol })
                    if (s != null) { fEtiquetas.Add(s.Font.Name); bEtiquetas.Add(s.Font.Bold); }
                // 0 x 0 es «el parche por defecto» de la leyenda, no un parche de tamaño cero.
                parches.Add(lcf.PatchWidth == 0 && lcf.PatchHeight == 0 ? "por_defecto"
                    : Math.Round(lcf.PatchWidth, 2).ToString(System.Globalization.CultureInfo.InvariantCulture) + "x"
                      + Math.Round(lcf.PatchHeight, 2).ToString(System.Globalization.CultureInfo.InvariantCulture));
            }
            Func<HashSet<bool>, JToken> negrita = h => h.Count == 1 ? (JToken)h.First() : h.Count == 0 ? null : "mixta";
            ILegendFormat fmt = lg.Format;
            var sep = new JObject();
            foreach (var kv in Separaciones)
                sep[kv.Key] = Math.Round(kv.Value.Item1(fmt), 2);
            return new JObject
            {
                ["fuentes_nombres"] = new JArray(fNombres.ToArray()),
                ["fuentes_etiquetas"] = new JArray(fEtiquetas.ToArray()),
                ["negrita_nombres"] = negrita(bNombres),
                ["negrita_etiquetas"] = negrita(bEtiquetas),
                ["parches"] = new JArray(parches.ToArray()),
                ["parche_por_defecto"] = new JArray(Math.Round(fmt.DefaultPatchWidth, 2), Math.Round(fmt.DefaultPatchHeight, 2)),
                ["separaciones"] = sep
            };
        }

        /// <summary>Tamaño que ocupa la leyenda dibujada sin escalar, en unidades de página.
        /// Con «marco fijo», QueryBounds devuelve el marco y no el contenido: se quita un
        /// momento para medir y se deja como estaba.</summary>
        private static IEnvelope TamanoNatural(Documento doc, ElementoLayout el)
        {
            IMapSurroundFrame msf = (IMapSurroundFrame)el.Elemento;
            ILegend3 lg3 = msf.MapSurround as ILegend3;
            bool fijo = lg3 != null && lg3.FixedFrame;
            IEnvelope natural = new EnvelopeClass();
            try
            {
                if (fijo)
                {
                    lg3.FixedFrame = false;
                    ((ILegend)lg3).Refresh();
                }
                msf.MapSurround.QueryBounds(doc.PantallaLayout(), el.Elemento.Geometry.Envelope, natural);
            }
            finally
            {
                if (fijo)
                {
                    lg3.FixedFrame = true;
                    ((ILegend)lg3).Refresh();
                }
            }
            double anchoPag, altoPag;
            doc.PageLayout.Page.QuerySize(out anchoPag, out altoPag);
            // Sin una pantalla válida QueryBounds devuelve medidas absurdas en vez de fallar.
            if (natural.IsEmpty || natural.Width <= 0 || natural.Height <= 0
                || natural.Width > anchoPag * 2 || natural.Height > altoPag * 2)
                throw new InvalidOperationException("No se ha podido medir la leyenda (medida "
                    + (natural.IsEmpty ? "vacía" : natural.Width + " x " + natural.Height)
                    + "): no se toca el marco.");
            return natural;
        }

        private static JObject EstadoLeyenda(Documento doc, ElementoLayout el, ILegend lg)
        {
            double min = double.MaxValue, max = double.MinValue;
            int columnasLeidas = 1;
            for (int k = 0; k < lg.ItemCount; k++)
            {
                ILegendItem it = lg.get_Item(k);
                if (k > 0 && it.NewColumn)
                    columnasLeidas++;
                var simbolos = new List<ITextSymbol> { it.LayerNameSymbol, it.HeadingSymbol };
                ILegendClassFormat lcf = it.LegendClassFormat;
                if (lcf != null)
                {
                    simbolos.Add(lcf.LabelSymbol);
                    simbolos.Add(lcf.DescriptionSymbol);
                }
                foreach (ITextSymbol s in simbolos)
                {
                    if (s == null) continue;
                    min = Math.Min(min, s.Size);
                    max = Math.Max(max, s.Size);
                }
            }
            IEnvelope marco = el.Elemento.Geometry.Envelope;
            var r = new JObject
            {
                ["entradas"] = lg.ItemCount,
                ["columnas"] = columnasLeidas,
                ["tamano_min"] = lg.ItemCount > 0 ? Math.Round(min, 2) : (double?)null,
                ["tamano_max"] = lg.ItemCount > 0 ? Math.Round(max, 2) : (double?)null,
                ["marco"] = new JObject
                {
                    ["xmin"] = Math.Round(marco.XMin, 3), ["ymin"] = Math.Round(marco.YMin, 3),
                    ["ancho"] = Math.Round(marco.Width, 3), ["alto"] = Math.Round(marco.Height, 3)
                },
                ["formato"] = FormatoLeyenda(lg)
            };
            try
            {
                IEnvelope natural = TamanoNatural(doc, el);
                r["natural"] = new JObject
                {
                    ["ancho"] = Math.Round(natural.Width, 3), ["alto"] = Math.Round(natural.Height, 3)
                };
            }
            catch (Exception ex) { r["natural"] = ex.Message; }
            ILegend3 lg3 = lg as ILegend3;
            if (lg3 != null)
            {
                r["marco_fijo"] = lg3.FixedFrame;
                r["ajuste_automatico"] = lg3.AutoFit;
                r["letra_minima_del_ajuste"] = lg3.MinFontSize;
                r["columnas_automaticas"] = lg3.AutoColumns;
                // Lo que de verdad se dibuja puede ser MENOR que tamano_min.
                if (lg3.FixedFrame && lg3.AutoFit)
                    r["aviso"] = "Marco fijo con ajuste automático: ArcMap encoge la letra al dibujar (hasta "
                        + lg3.MinFontSize + " pt) para que quepa. tamano_min es el escrito, no el dibujado.";
            }
            try { r["unidades_pagina"] = doc.PageLayout.Page.Units.ToString().Replace("esri", ""); }
            catch { }
            return r;
        }

        private static JObject Interruptores(ILegendItem it)
        {
            return new JObject
            {
                ["mostrar_nombre"] = it.ShowLayerName,
                ["mostrar_encabezado"] = it.ShowHeading,
                ["mostrar_etiquetas"] = it.ShowLabels,
                ["columna_nueva"] = it.NewColumn
            };
        }

        private static JObject EstadoItem(ItemEnLeyenda o, JObject antes)
        {
            var r = new JObject
            {
                ["leyenda"] = o.NombreLeyenda,
                ["capa"] = o.Item.Layer != null ? o.Item.Layer.Name : null,
                ["posicion_en_leyenda"] = o.Indice + 1,
                ["cambiado"] = antes != null,
                ["ahora"] = Interruptores(o.Item),
                ["orden"] = Orden(o.Leyenda),
                // Leídos, nunca escritos: la prueba de que el cambio no ha tocado el aspecto.
                ["fuentes"] = new JObject
                {
                    ["nombre_capa"] = Fuente(o.Item.LayerNameSymbol),
                    ["encabezado"] = Fuente(o.Item.HeadingSymbol),
                    ["etiquetas"] = o.Item.LegendClassFormat != null
                        ? Fuente(o.Item.LegendClassFormat.LabelSymbol) : null
                }
            };
            if (antes != null)
                r["antes"] = antes;
            return r;
        }

        /// <summary>Las entradas de la leyenda en su orden, leídas de la leyenda.</summary>
        private static JArray Orden(ILegend lg)
        {
            var orden = new JArray();
            for (int k = 0; k < lg.ItemCount; k++)
            {
                ILegendItem it = lg.get_Item(k);
                orden.Add(it.Layer != null ? it.Layer.Name : "");
            }
            return orden;
        }

        private static JToken Fuente(ITextSymbol sym)
        {
            if (sym == null)
                return null;
            try
            {
                stdole.IFontDisp f = sym.Font;
                return new JObject
                {
                    ["fuente"] = f.Name,
                    ["tamano"] = (double)f.Size,
                    ["negrita"] = f.Bold,
                    ["cursiva"] = f.Italic
                };
            }
            catch
            {
                return new JObject { ["tamano"] = sym.Size };
            }
        }
    }
}
