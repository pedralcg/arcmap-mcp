using System;
using System.Collections.Generic;
using System.Linq;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.Framework;
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
    internal static class LayoutHandlers
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
        private sealed class ElementoLayout
        {
            public IElement Elemento;
            /// <summary>Elemento de PRIMER NIVEL que lo contiene (él mismo si no está
            /// agrupado): es el que entiende IGraphicsContainer.UpdateElement.</summary>
            public IElement Raiz;
            public string Nombre;
            /// <summary>Ruta de grupos que lo contiene, null si está en primer nivel.</summary>
            public string Grupo;
        }

        private static List<ElementoLayout> Elementos(IGraphicsContainer gc)
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

            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);

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
            JToken tGrupo = parameters["grupo"];
            if (tGrupo != null && tGrupo.Type != JTokenType.Null)
            {
                string grupo = (string)tGrupo ?? "";
                candidatos = candidatos
                    .Where(p => string.Equals(p.Grupo ?? "", grupo, StringComparison.OrdinalIgnoreCase))
                    .ToList();
                if (candidatos.Count == 0)
                    throw new ArgumentException("Ningún elemento de texto " + descripcion
                        + " está en el grupo '" + grupo + "' (usa grupo=\"\" para los sueltos).");
            }
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

        public static JObject SetTextElement(JObject parameters)
        {
            string nombre = (string)parameters["nombre"];
            string buscar = (string)parameters["buscar"];
            string texto = (string)parameters["texto"] ?? "";

            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);

            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            var elementos = new List<ElementoLayout>();
            foreach (ElementoLayout e in Elementos(gc))
                if (e.Elemento is ITextElement)
                    elementos.Add(e);

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
                objetivo = Desempatar(candidatos, parameters, "que coinciden con '" + buscar + "'");
            }
            else
            {
                throw new ArgumentException(
                    "Indica 'nombre' (name del elemento) o 'buscar' (su texto actual).");
            }

            ITextElement te = (ITextElement)objetivo.Elemento;
            string anterior = te.Text;
            te.Text = texto;

            // UpdateElement sobre el elemento de PRIMER NIVEL: sin esto el cambio no
            // se consolida en el contenedor de gráficos y el texto viejo reaparece al
            // redibujar o al exportar. Para un texto agrupado, el que conoce el
            // contenedor es el grupo, no el texto.
            try { gc.UpdateElement(objetivo.Raiz); }
            catch { /* elemento sin contenedor propio: el refresco de abajo basta */ }

            // Refrescar la capa de gráficos del layout y la vista activa.
            IActiveView layoutView = (IActiveView)doc.PageLayout;
            layoutView.PartialRefresh(esriViewDrawPhase.esriViewGraphics, null, null);
            doc.ActiveView.Refresh();

            return Protocol.Result(new JObject
            {
                ["nombre"] = objetivo.Nombre,
                ["grupo"] = objetivo.Grupo,
                ["texto_anterior"] = anterior,
                ["texto_nuevo"] = texto
            });
        }

        private static string Texto(ElementoLayout e)
        {
            return ((ITextElement)e.Elemento).Text ?? "";
        }

        // ------------------------------------------------------------------ leyendas

        /// <summary>Leyendas del layout que dibujan este mapa (también las que viven
        /// dentro de un grupo de elementos).</summary>
        internal static List<ILegend> LeyendasDelMapa(IMxDocument doc, IMap map)
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
        /// estilo entero: en el 23_02 de ID2018 puso el nombre en negrita grande y los
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

            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
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

            if (mostrarNombre == null && mostrarEncabezado == null && mostrarEtiquetas == null)
                return Protocol.Result(EstadoItem(objetivo, null));

            JObject antes = Interruptores(objetivo.Item);
            if (mostrarNombre.HasValue) objetivo.Item.ShowLayerName = mostrarNombre.Value;
            if (mostrarEncabezado.HasValue) objetivo.Item.ShowHeading = mostrarEncabezado.Value;
            if (mostrarEtiquetas.HasValue) objetivo.Item.ShowLabels = mostrarEtiquetas.Value;

            // Refresh reconstruye la leyenda con los interruptores nuevos; sin el
            // UpdateElement del elemento de primer nivel, el cambio no se consolida
            // y el export sale con la leyenda vieja (mismo motivo que set_text_element).
            objetivo.Leyenda.Refresh();
            try { gc.UpdateElement(objetivo.Elemento.Raiz); }
            catch { /* sin contenedor propio: basta el refresco */ }
            ((IActiveView)doc.PageLayout).PartialRefresh(esriViewDrawPhase.esriViewGraphics, null, null);
            doc.ActiveView.Refresh();

            return Protocol.Result(EstadoItem(objetivo, antes));
        }

        private static JObject Interruptores(ILegendItem it)
        {
            return new JObject
            {
                ["mostrar_nombre"] = it.ShowLayerName,
                ["mostrar_encabezado"] = it.ShowHeading,
                ["mostrar_etiquetas"] = it.ShowLabels
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
