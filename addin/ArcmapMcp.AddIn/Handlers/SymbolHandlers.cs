using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Símbolo único y retoque de la simbología existente.
    ///
    /// El hueco que cubren (2026-09-23, capturas del post): no había forma de poner
    /// una capa con UN color. Forzar la graduada o los valores únicos a un solo color
    /// pintaba bien el mapa pero llenaba la TOC con una entrada por entidad, y la
    /// única salida era recargar la capa, que entra con un color ALEATORIO. Y para
    /// cambiar un color de una leyenda ya montada había que rehacer la clasificación.
    ///
    /// - set_single_symbology sustituye el renderer por uno simple.
    /// - edit_symbol cambia SOLO lo pedido en el símbolo que ya hay (simple, valores
    ///   únicos o rangos), sobre una copia del símbolo: el tipo, el patrón y lo que
    ///   no se pida se quedan como estaban.
    /// Las dos devuelven los símbolos leídos de vuelta de la capa.
    /// </summary>
    internal static class SymbolHandlers
    {
        public static JObject SetSingleSymbology(JObject parameters)
        {
            Documento doc;
            IMap map;
            IGeoFeatureLayer gfl = CapaDeEntidades(parameters, out doc, out map);
            esriGeometryType shp = Geometria(gfl);
            Cambios c = LeerCambios(parameters, shp);

            ISymbol sym = Aplicar(SimboloNuevo(shp), c);
            ISimpleRenderer render = new SimpleRendererClass { Symbol = sym };
            string etiqueta = Parametros.Dado(parameters["etiqueta"]) ? (string)parameters["etiqueta"] : null;
            if (etiqueta != null)
                render.Label = etiqueta;
            gfl.Renderer = (IFeatureRenderer)render;
            AplicarTransparencia(gfl, c);

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(Estado(gfl, null));
        }

        public static JObject EditSymbol(JObject parameters)
        {
            Documento doc;
            IMap map;
            // Un ráster va por su camino: solo encabezado, etiqueta y color de una clase.
            string nombreCapa = (string)parameters["capa"];
            if (!string.IsNullOrEmpty(nombreCapa))
            {
                map = MapHandlers.FocusMap(out doc);
                IRasterLayer rl = MapHandlers.FindLayer(map, nombreCapa) as IRasterLayer;
                if (rl != null)
                    return EditarRaster(parameters, rl, map, doc);
            }
            IGeoFeatureLayer gfl = CapaDeEntidades(parameters, out doc, out map);
            // Una capa rota se puede leer y retocar igual: su simbología está en el .mxd, no en
            // la fuente. La geometría se deduce entonces del símbolo (prueba del 2026-10-10:
            // leer la simbología de una capa rota fallaba, justo al ir a repararla).
            bool rota = gfl.FeatureClass == null;
            esriGeometryType shp = rota ? GeometriaDelSimbolo(gfl, SoloLectura(parameters)) : Geometria(gfl);
            Cambios c = LeerCambios(parameters, shp);
            string categoria = Parametros.Dado(parameters["categoria"]) ? (string)parameters["categoria"] : null;
            string nuevaEtiqueta = Parametros.Dado(parameters["etiqueta"]) ? (string)parameters["etiqueta"] : null;
            bool quitar = Parametros.LeerBool(parameters["quitar"], "quitar", false);
            // El texto que sale encima de las clases; "" lo deja vacío.
            string encabezado = parameters["encabezado"] != null && parameters["encabezado"].Type != JTokenType.Null
                ? (string)parameters["encabezado"] : null;
            JArray anadir = parameters["anadir_valores"] as JArray;
            if (Parametros.Dado(parameters["anadir_valores"]) && (anadir == null || anadir.Count == 0))
                throw new ArgumentException("'anadir_valores' es una lista de valores: [\"0\", \"99\"].");
            if (nuevaEtiqueta != null && nuevaEtiqueta.Trim().Length == 0)
                throw new ArgumentException("'etiqueta' vacía: la leyenda mostraría una entrada en blanco."
                    + " Pasa el texto que quieres que se vea.");

            if (quitar)
            {
                if (c.AlgoQueCambiar || nuevaEtiqueta != null || anadir != null || encabezado != null)
                    throw new ArgumentException("'quitar' va solo, con 'categoria': quitar una clase y cambiarle"
                        + " el símbolo o la etiqueta a la vez no tiene sentido.");
                return QuitarCategoria(gfl, map, doc, categoria);
            }
            if (!c.AlgoQueCambiar && nuevaEtiqueta == null && anadir == null && encabezado == null)
                return Protocol.Result(ConAvisoDeRota(Estado(gfl, null), rota));

            JObject antes = Estado(gfl, null);
            IFeatureRenderer fr = gfl.Renderer;
            if (anadir != null)
                AnadirValores(fr as IUniqueValueRenderer, categoria, anadir);
            if (encabezado != null)
                PonerEncabezado(fr as ILegendInfo, encabezado);
            if (!c.AlgoQueCambiar && nuevaEtiqueta == null)
            {
                MapHandlers.NotificarCambioContenido(map, doc);
                return Protocol.Result(ConAvisoDeRota(Estado(gfl, antes), rota));
            }

            ISimpleRenderer simple = fr as ISimpleRenderer;
            IUniqueValueRenderer unicos = fr as IUniqueValueRenderer;
            IClassBreaksRenderer rangos = fr as IClassBreaksRenderer;
            var tocadas = new List<string>();

            if (simple != null)
            {
                if (categoria != null)
                    throw new ArgumentException("La capa tiene símbolo único: no hay categorías que elegir."
                        + " Quita 'categoria'.");
                if (c.AlgoQueCambiar)
                    simple.Symbol = Retocado(simple.Symbol, c);
                if (nuevaEtiqueta != null)
                    simple.Label = nuevaEtiqueta;
                tocadas.Add("(símbolo único)");
            }
            else if (unicos != null || rangos != null)
            {
                List<Clase> clases = Clases(unicos, rangos);
                List<Clase> objetivo;
                // Sin clases, «todas» es ninguna: respondía ok sin cambiar nada (2026-10-10, una
                // capa de valores únicos de un .mxd real que dibuja solo «todos los demás valores»).
                if (clases.Count == 0 && categoria == null)
                    throw new ArgumentException("La capa está " + (unicos != null ? "por valores únicos" : "por rangos")
                        + " pero sin ninguna clase: no hay categorías a las que aplicar el cambio (lo que se"
                        + " dibuja, si algo, es «todos los demás valores»). Para darle un símbolo, set_single_symbology.");
                if (categoria != null)
                {
                    objetivo = ElegirClases(clases, categoria);
                    if (objetivo.Count == 0)
                        throw new ArgumentException("No hay ninguna categoría '" + categoria + "'. Categorías (valor ="
                            + " etiqueta): " + string.Join(" | ", clases.ConvertAll(k => k.Valor + " = " + k.Etiqueta)));
                }
                else if (nuevaEtiqueta != null)
                {
                    throw new ArgumentException("'etiqueta' cambia el texto de UNA clase en la leyenda: indica"
                        + " 'categoria' (valor o etiqueta actual). Con " + clases.Count + " clases, ponerles a"
                        + " todas el mismo texto no es lo que nadie quiere.");
                }
                else
                {
                    // Sin categoría se aplica a TODAS, pero no el relleno: poner el mismo
                    // color de relleno a todas las clases borra la clasificación sin avisar.
                    if (c.ColorRelleno != null || c.SinRelleno)
                        throw new ArgumentException("La capa está clasificada en " + clases.Count + " categorías:"
                            + " cambiar el relleno de todas a la vez borraría la clasificación. Indica 'categoria'"
                            + " (valor o etiqueta), o usa set_single_symbology si de verdad quieres un solo color."
                            + " El borde, su grosor, el tamaño y la transparencia sí se pueden cambiar en todas.");
                    objetivo = clases;
                }
                if (nuevaEtiqueta != null && objetivo.Count > 1)
                    throw new ArgumentException("'" + categoria + "' casa con " + objetivo.Count + " clases (por"
                        + " valor y por etiqueta): ponerles el mismo texto las haría indistinguibles en la"
                        + " leyenda. Usa el VALOR de la clase: " + string.Join(" | ",
                            objetivo.ConvertAll(k => k.Valor + " = " + k.Etiqueta)));
                foreach (Clase k in objetivo)
                {
                    if (c.AlgoQueCambiar)
                        k.Poner(Retocado(k.Leer(), c));
                    if (nuevaEtiqueta != null)
                        k.PonerEtiqueta(nuevaEtiqueta);
                    tocadas.Add(k.Etiqueta);
                }
            }
            else
            {
                throw new ArgumentException("La simbología de la capa es de un tipo que edit_symbol no sabe editar ("
                    + TipoRenderer(fr) + "). Sabe: símbolo único, valores únicos y rangos.");
            }

            AplicarTransparencia(gfl, c);
            MapHandlers.NotificarCambioContenido(map, doc);
            JObject r = Estado(gfl, antes);
            r["categorias_cambiadas"] = new JArray(tocadas.ToArray());
            return Protocol.Result(ConAvisoDeRota(r, rota));
        }

        /// <summary>Sin ningún parámetro de cambio: edit_symbol solo devuelve el estado.</summary>
        private static bool SoloLectura(JObject p)
        {
            foreach (string k in new[] { "color_relleno", "color_borde", "grosor_borde", "tamano", "sin_relleno",
                                         "transparencia", "etiqueta", "quitar", "encabezado", "anadir_valores",
                                         "patron_linea", "trama", "estilo_marcador" })
                if (p[k] != null && p[k].Type != JTokenType.Null)
                    return false;
            return true;
        }

        private static JObject ConAvisoDeRota(JObject r, bool rota)
        {
            if (rota)
                r["aviso"] = "La capa está rota (su fuente no se encuentra): la simbología se lee y se edita"
                    + " igual, pero no se dibuja hasta reparar la fuente (repair_data_source).";
            return r;
        }

        /// <summary>La geometría de una capa ROTA, deducida del símbolo de su renderer (relleno,
        /// línea o marcador). Sin fuente no hay ShapeType que leer.</summary>
        private static esriGeometryType GeometriaDelSimbolo(IGeoFeatureLayer gfl, bool soloLectura)
        {
            // Para solo leer, la geometría no hace falta: no hay cambios que validar contra ella.
            if (soloLectura)
                return esriGeometryType.esriGeometryNull;
            IFeatureRenderer fr = gfl.Renderer;
            ISymbol sym = null;
            ISimpleRenderer simple = fr as ISimpleRenderer;
            if (simple != null)
                sym = simple.Symbol;
            else if (fr is IUniqueValueRenderer || fr is IClassBreaksRenderer)
            {
                List<Clase> clases = Clases(fr as IUniqueValueRenderer, fr as IClassBreaksRenderer);
                if (clases.Count > 0)
                    sym = clases[0].Leer();
                if (sym == null && fr is IUniqueValueRenderer)
                    sym = ((IUniqueValueRenderer)fr).DefaultSymbol;
            }
            if (sym is IFillSymbol) return esriGeometryType.esriGeometryPolygon;
            if (sym is ILineSymbol) return esriGeometryType.esriGeometryPolyline;
            if (sym is IMarkerSymbol) return esriGeometryType.esriGeometryPoint;
            throw new ArgumentException("La capa está rota (su fuente no se encuentra) y su simbología no deja deducir"
                + " la geometría. Repárala antes con repair_data_source.");
        }

        /// <summary>
        /// Mete valores en una clase de valores únicos que ya existe (AddReferenceValue):
        /// se dibujan con su símbolo y salen bajo su etiqueta, sin tocar el resto. Es lo de
        /// «todo lo de año 0 → Sin actuación programada» de una serie de montes (2026-10-06).
        /// </summary>
        private static void AnadirValores(IUniqueValueRenderer uvr, string categoria, JArray anadir)
        {
            if (uvr == null)
                throw new ArgumentException("'anadir_valores' es para capas por valores únicos.");
            if (categoria == null)
                throw new ArgumentException("'anadir_valores' necesita 'categoria': la clase a la que se añaden.");
            List<Clase> clases = Clases(uvr, null);
            List<Clase> objetivo = ElegirClases(clases, categoria);
            if (objetivo.Count != 1)
                throw new ArgumentException(objetivo.Count == 0
                    ? "No hay ninguna categoría '" + categoria + "'. Categorías (valor = etiqueta): "
                      + string.Join(" | ", clases.ConvertAll(k => k.Valor + " = " + k.Etiqueta))
                    : "'" + categoria + "' casa con " + objetivo.Count + " clases: usa su VALOR.");
            foreach (JToken t in anadir)
            {
                string v = Convert.ToString(((JValue)t).Value, CultureInfo.InvariantCulture);
                Clase duena = clases.Find(k => k.Valor == v || k.Agrupados.Contains(v));
                if (duena != null)
                    throw new ArgumentException("El valor '" + v + "' ya está en la clase '" + duena.Etiqueta + "'. Un valor"
                        + " va en una sola clase: si es una clase propia, quítala antes con edit_symbol(quitar=True).");
            }
            foreach (JToken t in anadir)
                uvr.AddReferenceValue(Convert.ToString(((JValue)t).Value, CultureInfo.InvariantCulture), objetivo[0].Valor);
        }

        /// <summary>
        /// edit_symbol sobre un RÁSTER clasificado o por valores únicos: encabezado,
        /// etiqueta y color de relleno de una clase. Lo que se ve en la leyenda se lee por
        /// ILegendInfo, que es lo que la leyenda dibuja. El encabezado es el caso del
        /// «&lt;VALOR&gt;» que deja replaceDataSource sobre un ráster clasificado.
        /// </summary>
        private static JObject EditarRaster(JObject p, IRasterLayer rl, IMap map, Documento doc)
        {
            foreach (string otro in new[] { "color_borde", "grosor_borde", "sin_relleno", "tamano",
                                            "transparencia", "quitar", "anadir_valores" })
                if (Parametros.Dado(p[otro]))
                    throw new ArgumentException("En un ráster, edit_symbol solo cambia 'encabezado', y la 'etiqueta' y el"
                        + " 'color_relleno' de una 'categoria'. '" + otro + "' no aplica; para rehacer la"
                        + " clasificación usa set_raster_symbology.");
            string categoria = Parametros.Dado(p["categoria"]) ? (string)p["categoria"] : null;
            string etiqueta = Parametros.Dado(p["etiqueta"]) ? (string)p["etiqueta"] : null;
            string encabezado = p["encabezado"] != null && p["encabezado"].Type != JTokenType.Null
                ? (string)p["encabezado"] : null;
            IColor color = Parametros.Dado(p["color_relleno"])
                ? Parametros.LeerColor(p["color_relleno"], "color_relleno", 0, 0, 0) : null;

            IRasterRenderer rr = rl.Renderer;
            IRasterClassifyColorRampRenderer clasif = rr as IRasterClassifyColorRampRenderer;
            IRasterUniqueValueRenderer unicos = rr as IRasterUniqueValueRenderer;
            if (etiqueta == null && color == null && encabezado == null)
                return Protocol.Result(EstadoRaster(rl, null));
            if ((etiqueta != null || color != null) && categoria == null)
                throw new ArgumentException("'etiqueta' y 'color_relleno' van con 'categoria' (su etiqueta, o su número 1..n).");
            if ((etiqueta != null || color != null) && clasif == null && unicos == null)
                throw new ArgumentException("La simbología de este ráster no tiene clases que editar (está estirada,"
                    + " o es RGB). Solo se puede cambiar el 'encabezado'.");

            JObject antes = EstadoRaster(rl, null);
            if (categoria != null && (etiqueta != null || color != null))
            {
                int n = clasif != null ? clasif.ClassCount : unicos.get_ClassCount(0);
                var etiquetas = new List<string>();
                for (int i = 0; i < n; i++)
                    etiquetas.Add(clasif != null ? clasif.get_Label(i) : unicos.get_Label(0, i));
                int k = etiquetas.FindIndex(e => string.Equals(e, categoria, StringComparison.OrdinalIgnoreCase));
                int numero;
                if (k < 0 && int.TryParse(categoria, out numero) && numero >= 1 && numero <= n)
                    k = numero - 1;
                if (k < 0)
                    throw new ArgumentException("No hay ninguna clase '" + categoria + "'. Clases (número = etiqueta): "
                        + string.Join(" | ", etiquetas.Select((e, i) => (i + 1) + " = " + e)));
                if (etiqueta != null)
                {
                    if (clasif != null) clasif.set_Label(k, etiqueta);
                    else unicos.set_Label(0, k, etiqueta);
                }
                if (color != null)
                {
                    ISymbol relleno = (ISymbol)new SimpleFillSymbolClass
                    {
                        Color = color,
                        Style = esriSimpleFillStyle.esriSFSSolid,
                        Outline = new SimpleLineSymbolClass { Style = esriSimpleLineStyle.esriSLSNull }
                    };
                    if (clasif != null) clasif.set_Symbol(k, relleno);
                    else unicos.set_Symbol(0, k, relleno);
                }
            }
            rr.Update();
            // El encabezado DESPUÉS de Update: el renderer lo regenera al actualizarse.
            if (encabezado != null)
            {
                if (unicos != null)
                    unicos.set_Heading(0, encabezado);
                PonerEncabezado(rr as ILegendInfo, encabezado);
            }
            rl.Renderer = rr;
            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(EstadoRaster(rl, antes));
        }

        private static JObject EstadoRaster(IRasterLayer rl, JObject antes)
        {
            IRasterRenderer rr = rl.Renderer;
            ILegendInfo li = rr as ILegendInfo;
            var clases = new JArray();
            if (li != null && li.LegendGroupCount > 0)
            {
                ILegendGroup g = li.get_LegendGroup(0);
                for (int i = 0; i < g.ClassCount; i++)
                {
                    ILegendClass lc = g.get_Class(i);
                    IFillSymbol fs = lc.Symbol as IFillSymbol;
                    clases.Add(new JObject
                    {
                        ["categoria"] = i + 1,
                        ["etiqueta"] = lc.Label,
                        ["color_relleno"] = fs != null ? Parametros.Hex(fs.Color) : null
                    });
                }
            }
            var r = new JObject
            {
                ["capa"] = ((ILayer)rl).Name,
                ["renderer"] = rr is IRasterClassifyColorRampRenderer ? "raster_clasificado"
                    : rr is IRasterUniqueValueRenderer ? "raster_valores_unicos"
                    : rr is IRasterStretchColorRampRenderer ? "raster_estirado" : "raster_otro",
                ["encabezado"] = Encabezado(li),
                ["clases"] = clases
            };
            if (antes != null)
                r["antes"] = antes;
            return r;
        }

        // ------------------------------------------------------------------ //

        private sealed class Cambios
        {
            public IColor ColorRelleno;
            public IColor ColorBorde;
            public double? GrosorBorde;
            public double? Tamano;
            public bool SinRelleno;
            public double? Transparencia;
            /// <summary>Guiones y huecos en puntos (guion, hueco, guion, hueco...); vacío = continua.</summary>
            public double[] Patron;
            /// <summary>Trama de líneas para el relleno; QuitarTrama la cambia por un relleno liso.</summary>
            public JObject Trama;
            public bool QuitarTrama;
            public esriSimpleMarkerStyle? EstiloMarcador;

            public bool AlgoQueCambiar
            {
                get
                {
                    return ColorRelleno != null || ColorBorde != null || GrosorBorde != null
                        || Tamano != null || SinRelleno || Transparencia != null
                        || Patron != null || Trama != null || QuitarTrama || EstiloMarcador != null;
                }
            }
        }

        private static Cambios LeerCambios(JObject p, esriGeometryType shp)
        {
            var c = new Cambios
            {
                ColorRelleno = Parametros.Dado(p["color_relleno"])
                    ? Parametros.LeerColor(p["color_relleno"], "color_relleno", 0, 0, 0) : null,
                ColorBorde = Parametros.Dado(p["color_borde"])
                    ? Parametros.LeerColor(p["color_borde"], "color_borde", 0, 0, 0) : null,
                GrosorBorde = Parametros.LeerDouble(p["grosor_borde"], "grosor_borde", 0, 50),
                Tamano = Parametros.LeerDouble(p["tamano"], "tamano", 0.1, 200),
                SinRelleno = Parametros.LeerBool(p["sin_relleno"], "sin_relleno", false),
                Transparencia = Parametros.LeerDouble(p["transparencia"], "transparencia", 0, 100)
            };
            // Lo que no aplica a la geometría es ERROR, no se ignora: si no, quien
            // llama se queda creyendo que su color se ha puesto.
            if (shp == esriGeometryType.esriGeometryPolyline && (c.ColorRelleno != null || c.SinRelleno))
                throw new ArgumentException("Una capa de líneas no tiene relleno: el color de la línea es"
                    + " 'color_borde' y su ancho 'grosor_borde'.");
            if (shp == esriGeometryType.esriGeometryPolygon && c.Tamano != null)
                throw new ArgumentException("'tamano' es para puntos. En polígonos el borde se ajusta con 'grosor_borde'.");
            if (shp == esriGeometryType.esriGeometryPolyline && c.Tamano != null)
                throw new ArgumentException("'tamano' es para puntos. En líneas el ancho es 'grosor_borde'.");
            if (c.SinRelleno && c.ColorRelleno != null)
                throw new ArgumentException("'sin_relleno' y 'color_relleno' se contradicen: pasa uno de los dos.");

            c.Patron = LeerPatron(p["patron_linea"]);
            if (c.Patron != null && shp == esriGeometryType.esriGeometryPoint)
                throw new ArgumentException("'patron_linea' es para líneas y para el borde de los polígonos, no para puntos.");
            JToken trama = p["trama"];
            if (Parametros.Dado(trama))
            {
                if (shp != esriGeometryType.esriGeometryPolygon)
                    throw new ArgumentException("'trama' es un relleno: solo para polígonos.");
                if (trama.Type == JTokenType.Boolean)
                {
                    if ((bool)trama)
                        c.Trama = new JObject();
                    else
                        c.QuitarTrama = true;
                }
                else if (trama is JObject)
                {
                    c.Trama = (JObject)trama;
                    foreach (var prop in c.Trama.Properties())
                        if (prop.Name != "angulo" && prop.Name != "separacion" && prop.Name != "color" && prop.Name != "grosor")
                            throw new ArgumentException("'trama' admite angulo, separacion, color y grosor (no '" + prop.Name + "').");
                    Parametros.LeerDouble(c.Trama["angulo"], "trama.angulo", -360, 360);
                    Parametros.LeerDouble(c.Trama["separacion"], "trama.separacion", 0.1, 200);
                    Parametros.LeerDouble(c.Trama["grosor"], "trama.grosor", 0, 50);
                    if (Parametros.Dado(c.Trama["color"]))
                        Parametros.LeerColor(c.Trama["color"], "trama.color", 0, 0, 0);
                }
                else
                    throw new ArgumentException("'trama' es {angulo, separacion, color, grosor}, true (la de por defecto)"
                        + " o false (vuelve a relleno liso).");
                if (c.Trama != null && (c.ColorRelleno != null || c.SinRelleno))
                    throw new ArgumentException("Con 'trama' el color es el de sus líneas: va en trama.color, no en"
                        + " 'color_relleno' ni 'sin_relleno'.");
            }
            string estilo = Parametros.Dado(p["estilo_marcador"]) ? (string)p["estilo_marcador"] : null;
            if (estilo != null)
            {
                if (shp != esriGeometryType.esriGeometryPoint)
                    throw new ArgumentException("'estilo_marcador' es para puntos.");
                esriSimpleMarkerStyle e;
                if (!EstilosMarcador.TryGetValue(estilo.Trim().ToLowerInvariant(), out e))
                    throw new ArgumentException("'estilo_marcador' es uno de: " + string.Join(", ", EstilosMarcador.Keys) + ".");
                c.EstiloMarcador = e;
            }
            return c;
        }

        private static readonly Dictionary<string, esriSimpleMarkerStyle> EstilosMarcador =
            new Dictionary<string, esriSimpleMarkerStyle>
            {
                { "circulo", esriSimpleMarkerStyle.esriSMSCircle },
                { "cuadrado", esriSimpleMarkerStyle.esriSMSSquare },
                { "rombo", esriSimpleMarkerStyle.esriSMSDiamond },
                { "cruz", esriSimpleMarkerStyle.esriSMSCross },
                { "x", esriSimpleMarkerStyle.esriSMSX },
            };

        /// <summary>Los patrones con nombre, en puntos. Salen de corregir_ao.py y editar24_ao.py de
        /// una serie de montes: intervalo 1,5 con guion 6 / hueco 3, y punteado 1 / 2.</summary>
        private static readonly Dictionary<string, double[]> Patrones = new Dictionary<string, double[]>
        {
            { "continua", new double[0] },
            { "discontinua", new[] { 9.0, 4.5 } },
            { "punteada", new[] { 1.5, 3.0 } },
            { "guion_punto", new[] { 9.0, 3.0, 1.5, 3.0 } },
        };

        private static double[] LeerPatron(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            if (t.Type == JTokenType.String)
            {
                double[] p;
                if (!Patrones.TryGetValue(((string)t).Trim().ToLowerInvariant(), out p))
                    throw new ArgumentException("'patron_linea' es uno de " + string.Join(", ", Patrones.Keys)
                        + ", o la lista de guiones y huecos en puntos: [9, 4.5].");
                return p;
            }
            JArray a = t as JArray;
            if (a == null || a.Count == 0 || a.Count % 2 != 0)
                throw new ArgumentException("'patron_linea' como lista lleva pares guion, hueco en puntos: [9, 4.5] o"
                    + " [9, 3, 1.5, 3].");
            var salida = new double[a.Count];
            for (int i = 0; i < a.Count; i++)
            {
                double? v = Parametros.LeerDouble(a[i], "patron_linea[" + i + "]", 0, 500);
                if (v == null)
                    throw new ArgumentException("'patron_linea' lleva números.");
                salida[i] = v.Value;
            }
            if (salida.All(v => v == 0) || Enumerable.Range(0, salida.Length / 2).All(i => salida[2 * i] == 0))
                throw new ArgumentException("'patron_linea' sin ningún guion no dibuja nada.");
            return salida;
        }

        internal static IGeoFeatureLayer CapaDeEntidades(JObject parameters, out Documento doc, out IMap map)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC, o ruta 'Grupo/Capa').");
            map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer gfl = lyr as IGeoFeatureLayer;
            if (gfl == null)
                throw new ArgumentException("'" + capa + "' " + (lyr is IRasterLayer
                    ? "es un RÁSTER; para eso usa set_raster_symbology."
                    : "no es una capa de entidades."));
            return gfl;
        }

        internal static esriGeometryType Geometria(IGeoFeatureLayer gfl)
        {
            if (gfl.FeatureClass == null)
                throw new ArgumentException("La capa no tiene fuente de datos accesible (¿rota?): no se sabe su geometría.");
            esriGeometryType t = gfl.FeatureClass.ShapeType;
            if (t == esriGeometryType.esriGeometryMultipoint)
                return esriGeometryType.esriGeometryPoint;
            if (t != esriGeometryType.esriGeometryPoint && t != esriGeometryType.esriGeometryPolyline
                && t != esriGeometryType.esriGeometryPolygon)
                throw new ArgumentException("Geometría no soportada: " + t + ".");
            return t;
        }

        /// <summary>Símbolo de partida de set_single_symbology. Colores neutros y
        /// FIJOS: la queja era justo el color aleatorio de la capa recién cargada.</summary>
        private static ISymbol SimboloNuevo(esriGeometryType shp)
        {
            IColor gris = new RgbColorClass { Red = 110, Green = 110, Blue = 110 };
            switch (shp)
            {
                case esriGeometryType.esriGeometryPolygon:
                    return (ISymbol)new SimpleFillSymbolClass
                    {
                        Style = esriSimpleFillStyle.esriSFSSolid,
                        Color = new RgbColorClass { Red = 204, Green = 204, Blue = 204 },
                        Outline = new SimpleLineSymbolClass { Color = gris, Width = 0.4 }
                    };
                case esriGeometryType.esriGeometryPolyline:
                    return (ISymbol)new SimpleLineSymbolClass { Color = gris, Width = 1.0 };
                default:
                    return (ISymbol)new SimpleMarkerSymbolClass
                    {
                        Style = esriSimpleMarkerStyle.esriSMSCircle,
                        Color = gris,
                        Size = 6
                    };
            }
        }

        /// <summary>Copia del símbolo con los cambios. Se clona para no tocar un
        /// símbolo que el renderer pueda compartir con otra clase.</summary>
        private static ISymbol Retocado(ISymbol original, Cambios c)
        {
            if (original == null)
                throw new InvalidOperationException("La clase no tiene símbolo que editar.");
            ISymbol copia = (ISymbol)((IClone)original).Clone();
            return Aplicar(copia, c);
        }

        /// <summary>Aplica los cambios y devuelve el símbolo resultante: el mismo objeto, salvo
        /// cuando el cambio pide OTRO tipo de símbolo (un patrón sobre una línea simple, una
        /// trama sobre un relleno liso, un estilo sobre un marcador que no es simple).</summary>
        private static ISymbol Aplicar(ISymbol sym, Cambios c)
        {
            IFillSymbol relleno = sym as IFillSymbol;
            ILineSymbol linea = sym as ILineSymbol;
            IMarkerSymbol marca = sym as IMarkerSymbol;
            if (relleno != null)
            {
                if (c.Trama != null || c.QuitarTrama)
                {
                    relleno = ConTrama(relleno, c);
                    sym = (ISymbol)relleno;
                }
                if (c.SinRelleno)
                    relleno.Color = new RgbColorClass { NullColor = true };
                else if (c.ColorRelleno != null)
                {
                    // El simétrico del issue #8: un color sobre un relleno con Style = esriSFSNull
                    // se guardaba y no se dibujaba. Pedir un color es pedir que se vea.
                    ISimpleFillSymbol simpleRelleno = relleno as ISimpleFillSymbol;
                    if (simpleRelleno != null && simpleRelleno.Style == esriSimpleFillStyle.esriSFSNull)
                        simpleRelleno.Style = esriSimpleFillStyle.esriSFSSolid;
                    relleno.Color = c.ColorRelleno;
                }
                if (c.ColorBorde != null || c.GrosorBorde != null || c.Patron != null)
                {
                    ILineSymbol borde = relleno.Outline ?? new SimpleLineSymbolClass();
                    if (c.Patron != null) borde = ConPatron(borde, c.Patron);
                    if (c.ColorBorde != null) borde.Color = c.ColorBorde;
                    if (c.GrosorBorde != null) borde.Width = c.GrosorBorde.Value;
                    relleno.Outline = borde; // se reasigna siempre: no se da por hecho que el getter devuelva el original
                }
            }
            else if (linea != null)
            {
                if (c.Patron != null)
                {
                    linea = ConPatron(linea, c.Patron);
                    sym = (ISymbol)linea;
                }
                if (c.ColorBorde != null) linea.Color = c.ColorBorde;
                if (c.GrosorBorde != null) linea.Width = c.GrosorBorde.Value;
            }
            else if (marca != null)
            {
                if (c.EstiloMarcador != null)
                {
                    ISimpleMarkerSymbol sm = sym as ISimpleMarkerSymbol;
                    if (sm == null)
                    {
                        // Un marcador de carácter o de imagen no tiene estilo: se cambia por uno simple
                        // con su color y su tamaño, que es lo que hacía marker_symbol de corregir_ao.py.
                        sm = new SimpleMarkerSymbolClass { Color = marca.Color, Size = marca.Size };
                        sym = (ISymbol)sm;
                        marca = sm;
                    }
                    sm.Style = c.EstiloMarcador.Value;
                }
                if (c.SinRelleno)
                    marca.Color = new RgbColorClass { NullColor = true };
                else if (c.ColorRelleno != null)
                    marca.Color = c.ColorRelleno;
                if (c.Tamano != null) marca.Size = c.Tamano.Value;
                ISimpleMarkerSymbol simple = sym as ISimpleMarkerSymbol;
                if (simple != null && (c.ColorBorde != null || c.GrosorBorde != null))
                {
                    simple.Outline = true;
                    if (c.ColorBorde != null) simple.OutlineColor = c.ColorBorde;
                    if (c.GrosorBorde != null) simple.OutlineSize = c.GrosorBorde.Value;
                }
                else if (c.ColorBorde != null || c.GrosorBorde != null)
                    throw new ArgumentException("El símbolo de punto de esta capa no es un marcador simple"
                        + " y no tiene borde editable.");
            }
            else
                throw new ArgumentException("Tipo de símbolo no editable: " + sym.GetType().Name + ".");
            return sym;
        }

        /// <summary>
        /// La línea con ese patrón de guiones (en puntos), o continua si viene vacío. Una línea
        /// SIMPLE se cambia por una CARTOGRÁFICA con su color y su ancho: el estilo Dash de la
        /// simple son guiones de píxel, y a 2 pt el plano la dibuja CONTINUA (editar24_ao.py de
        /// una serie de montes). Las que ya tienen ILineProperties (cartográfica, de tramas, de marcadores)
        /// conservan lo demás y solo cambian de plantilla.
        /// </summary>
        private static ILineSymbol ConPatron(ILineSymbol l, double[] patron)
        {
            if (l is IMultiLayerLineSymbol)
                throw new ArgumentException("La línea es un símbolo de varias capas: 'patron_linea' no sabe a cuál"
                    + " aplicarlo. Ponle antes un símbolo simple (set_single_symbology o un estilo).");
            if (patron.Length == 0)
            {
                ISimpleLineSymbol simple = l as ISimpleLineSymbol;
                if (simple != null)
                {
                    simple.Style = esriSimpleLineStyle.esriSLSSolid;
                    return l;
                }
                if (l is ICartographicLineSymbol)
                    return new SimpleLineSymbolClass { Color = l.Color, Width = l.Width, Style = esriSimpleLineStyle.esriSLSSolid };
                return l;
            }
            ILineProperties lp = l as ILineProperties;
            if (lp == null)
            {
                var carto = new CartographicLineSymbolClass { Color = l.Color, Width = l.Width };
                lp = carto;
                l = carto;
            }
            ITemplate t = new TemplateClass { Interval = 1 };
            for (int i = 0; i + 1 < patron.Length; i += 2)
                t.AddPatternElement(patron[i], patron[i + 1]);
            lp.Template = t;
            return l;
        }

        /// <summary>
        /// El relleno con trama de líneas (ILineFillSymbol), o liso si se quita. Lo que no se pida
        /// sale de lo que había: el color, del relleno liso (o de la trama anterior), para que una
        /// clase de unos valores únicos conserve el suyo; ángulo 45, separación 5 pt y grosor 1 pt
        /// si no había trama. El borde se queda. Es fill_symbol de corregir_ao.py (serie de montes).
        /// </summary>
        private static IFillSymbol ConTrama(IFillSymbol f, Cambios c)
        {
            if (f is IMultiLayerFillSymbol)
                throw new ArgumentException("El relleno es un símbolo de varias capas: 'trama' no sabe a cuál aplicarse."
                    + " Ponle antes un símbolo simple (set_single_symbology o un estilo).");
            ILineFillSymbol previa = f as ILineFillSymbol;
            if (c.QuitarTrama)
            {
                if (previa == null)
                    return f;
                IColor color = previa.LineSymbol != null ? previa.LineSymbol.Color : f.Color;
                return new SimpleFillSymbolClass { Color = color, Outline = f.Outline, Style = esriSimpleFillStyle.esriSFSSolid };
            }
            JObject t = c.Trama;
            IColor colorTrama = Parametros.Dado(t["color"]) ? Parametros.LeerColor(t["color"], "trama.color", 0, 0, 0)
                : previa != null && previa.LineSymbol != null ? previa.LineSymbol.Color
                : f.Color != null && !f.Color.NullColor ? f.Color
                : new RgbColorClass { Red = 0, Green = 0, Blue = 0 };
            double grosor = Parametros.LeerDouble(t["grosor"], "trama.grosor", 0, 50)
                ?? (previa != null && previa.LineSymbol != null ? previa.LineSymbol.Width : 1.0);
            ILineFillSymbol lf = new LineFillSymbolClass
            {
                Angle = Parametros.LeerDouble(t["angulo"], "trama.angulo", -360, 360) ?? (previa != null ? previa.Angle : 45),
                Separation = Parametros.LeerDouble(t["separacion"], "trama.separacion", 0.1, 200)
                    ?? (previa != null ? previa.Separation : 5),
                LineSymbol = new SimpleLineSymbolClass { Color = colorTrama, Width = grosor }
            };
            ((IFillSymbol)lf).Outline = f.Outline;
            return (IFillSymbol)lf;
        }

        /// <summary>El patrón de una línea como se pide: "continua", la lista de guiones y huecos
        /// en puntos, o el estilo de una línea simple que no es continua ("dash", "dot"...).</summary>
        private static JToken PatronDe(ILineSymbol l)
        {
            if (l == null)
                return null;
            ISimpleLineSymbol simple = l as ISimpleLineSymbol;
            if (simple != null)
                return simple.Style == esriSimpleLineStyle.esriSLSSolid ? "continua"
                    : simple.Style.ToString().Replace("esriSLS", "").ToLowerInvariant();
            ILineProperties lp = l as ILineProperties;
            ITemplate t = lp != null ? lp.Template : null;
            if (t == null || t.PatternElementCount == 0)
                return "continua";
            var a = new JArray();
            for (int i = 0; i < t.PatternElementCount; i++)
            {
                double guion, hueco;
                t.GetPatternElement(i, out guion, out hueco);
                a.Add(Math.Round(guion * t.Interval, 3));
                a.Add(Math.Round(hueco * t.Interval, 3));
            }
            return a;
        }

        private static void AplicarTransparencia(IGeoFeatureLayer gfl, Cambios c)
        {
            if (c.Transparencia == null)
                return;
            ILayerEffects fx = gfl as ILayerEffects;
            if (fx == null || !fx.SupportsTransparency)
                throw new ArgumentException("Esta capa no admite transparencia.");
            fx.Transparency = (short)Math.Round(c.Transparencia.Value);
        }

        // ------------------------------------------------------------------ //

        /// <summary>Una clase de un renderer de valores únicos o de rangos, con su
        /// forma de leer y escribir el símbolo (cada renderer lo indexa distinto).</summary>
        private sealed class Clase
        {
            public string Valor;
            public List<string> Agrupados = new List<string>();
            public string Etiqueta;
            public Func<ISymbol> Leer;
            public Action<ISymbol> Poner;
            public Action<string> PonerEtiqueta;
        }

        /// <summary>
        /// Quita una categoría de valores únicos y deja el resto TAL CUAL.
        ///
        /// Por arcpy (`symbology.classValues` sin el valor) ArcMap regenera la rampa y
        /// cambia el color de todas las demás clases: el 2026-10-01 (Plano 09 de
        /// un expediente real) hubo que restaurarlas una a una. RemoveValue del renderer no
        /// regenera nada.
        /// </summary>
        private static JObject QuitarCategoria(IGeoFeatureLayer gfl, IMap map, Documento doc, string categoria)
        {
            if (categoria == null)
                throw new ArgumentException("Para 'quitar' indica 'categoria' (valor o etiqueta de la clase).");
            IUniqueValueRenderer unicos = gfl.Renderer as IUniqueValueRenderer;
            if (unicos == null)
                throw new ArgumentException("'quitar' solo sirve para valores únicos (esta capa es "
                    + TipoRenderer(gfl.Renderer) + "). En rangos, quitar uno cambiaría los límites de los"
                    + " vecinos: rehaz la clasificación con set_graduated_symbology.");
            List<Clase> clases = Clases(unicos, null);
            List<Clase> objetivo = ElegirClases(clases, categoria);
            if (objetivo.Count == 0)
                throw new ArgumentException("No hay ninguna categoría '" + categoria + "'. Categorías (valor ="
                    + " etiqueta): " + string.Join(" | ", clases.ConvertAll(k => k.Valor + " = " + k.Etiqueta)));
            if (objetivo.Count > 1)
                throw new ArgumentException("'" + categoria + "' casa con " + objetivo.Count + " clases: usa el"
                    + " VALOR de la que quieres quitar: " + string.Join(" | ",
                        objetivo.ConvertAll(k => k.Valor + " = " + k.Etiqueta)));
            if (clases.Count == 1)
                throw new ArgumentException("Es la única clase de la capa: quitarla la dejaría sin simbología."
                    + " Para un solo símbolo usa set_single_symbology; para ocultarla, set_layer_visibility.");

            JObject antes = Estado(gfl, null);
            // Los valores agrupados en la clase se van con ella: sueltos en el renderer,
            // quedarían como valores sin símbolo propio.
            foreach (string agrupado in objetivo[0].Agrupados)
                unicos.RemoveValue(agrupado);
            unicos.RemoveValue(objetivo[0].Valor);
            MapHandlers.NotificarCambioContenido(map, doc);

            JObject r = Estado(gfl, antes);
            r["categoria_quitada"] = objetivo[0].Valor + " = " + objetivo[0].Etiqueta;
            // Las entidades con ese valor no desaparecen: pasan a «todos los demás
            // valores» si la capa lo usa, y si no, dejan de dibujarse.
            r["entidades_de_esa_categoria"] = unicos.UseDefaultSymbol
                ? "se dibujan con el símbolo de «todos los demás valores» ('" + (unicos.DefaultLabel ?? "") + "')"
                : "ya no se dibujan (la capa no usa símbolo para «todos los demás valores»)";
            return Protocol.Result(r);
        }

        /// <summary>
        /// Las clases que nombra `categoria`. Manda el VALOR: si alguna casa por valor exacto,
        /// son esas; la etiqueta solo se mira si ninguna casa por valor. Mirando las dos a la
        /// vez, en unos rangos con etiquetas numéricas (clase 1 = "0", clase 2 = "1") la
        /// categoría "1" casaba con dos clases, y el error pedía «usa el VALOR», que volvía a
        /// ser "1": esa clase no se podía alcanzar (revisión del 2026-10-01).
        /// </summary>
        private static List<Clase> ElegirClases(List<Clase> clases, string categoria)
        {
            List<Clase> porValor = clases.FindAll(k => string.Equals(k.Valor, categoria, StringComparison.OrdinalIgnoreCase));
            return porValor.Count > 0
                ? porValor
                : clases.FindAll(k => string.Equals(k.Etiqueta, categoria, StringComparison.OrdinalIgnoreCase));
        }

        private static List<Clase> Clases(IUniqueValueRenderer unicos, IClassBreaksRenderer rangos)
        {
            var salida = new List<Clase>();
            if (unicos != null)
            {
                // Una clase por valor PRINCIPAL; los agrupados con AddReferenceValue van
                // dentro de la suya (se dibujan con su símbolo y no tienen entrada propia).
                foreach (var g in UniqueValuesHandlers.Grupos(unicos))
                {
                    string v = g.Item1;
                    string etiqueta = unicos.get_Label(v);
                    salida.Add(new Clase
                    {
                        Valor = v,
                        Agrupados = g.Item2.Skip(1).ToList(),
                        Etiqueta = string.IsNullOrEmpty(etiqueta) ? v : etiqueta,
                        Leer = () => unicos.get_Symbol(v),
                        Poner = s => unicos.set_Symbol(v, s),
                        PonerEtiqueta = t => unicos.set_Label(v, t)
                    });
                }
            }
            else
            {
                for (int i = 0; i < rangos.BreakCount; i++)
                {
                    int k = i;
                    string etiqueta = rangos.get_Label(k);
                    string hasta = rangos.get_Break(k).ToString(CultureInfo.InvariantCulture);
                    salida.Add(new Clase
                    {
                        // El "valor" de un rango es su número de orden (1..n): el límite
                        // superior con decimales no es algo que se escriba a mano.
                        Valor = (k + 1).ToString(CultureInfo.InvariantCulture),
                        Etiqueta = string.IsNullOrEmpty(etiqueta) ? "hasta " + hasta : etiqueta,
                        Leer = () => rangos.get_Symbol(k),
                        Poner = s => rangos.set_Symbol(k, s),
                        PonerEtiqueta = t => rangos.set_Label(k, t)
                    });
                }
            }
            return salida;
        }

        /// <summary>El encabezado del primer grupo de la leyenda del renderer: el texto que
        /// sale encima de las clases en la TOC y en la leyenda.</summary>
        private static string Encabezado(ILegendInfo li)
        {
            try
            {
                return li != null && li.LegendGroupCount > 0 ? li.get_LegendGroup(0).Heading : null;
            }
            catch { return null; }
        }

        private static void PonerEncabezado(ILegendInfo li, string texto)
        {
            if (li == null || li.LegendGroupCount == 0)
                throw new ArgumentException("La simbología de la capa no tiene encabezado que cambiar.");
            li.get_LegendGroup(0).Heading = texto;
        }

        private static string TipoRenderer(IFeatureRenderer fr)
        {
            if (fr is ISimpleRenderer) return "simple";
            if (fr is IUniqueValueRenderer) return "valores_unicos";
            if (fr is IClassBreaksRenderer) return "rangos";
            return fr == null ? "ninguno" : fr.GetType().Name;
        }

        internal static JObject Estado(IGeoFeatureLayer gfl, JObject antes)
        {
            IFeatureRenderer fr = gfl.Renderer;
            var clases = new JArray();
            ISimpleRenderer simple = fr as ISimpleRenderer;
            if (simple != null)
            {
                JObject s = Describir(simple.Symbol);
                s["etiqueta"] = simple.Label;
                clases.Add(s);
            }
            else if (fr is IUniqueValueRenderer || fr is IClassBreaksRenderer)
            {
                foreach (Clase k in Clases(fr as IUniqueValueRenderer, fr as IClassBreaksRenderer))
                {
                    JObject s = Describir(k.Leer());
                    s["categoria"] = k.Valor;
                    s["etiqueta"] = k.Etiqueta;
                    if (k.Agrupados.Count > 0)
                        s["valores_agrupados"] = new JArray(k.Agrupados.ToArray());
                    clases.Add(s);
                }
            }
            ILayerEffects fx = gfl as ILayerEffects;
            var r = new JObject
            {
                ["capa"] = ((ILayer)gfl).Name,
                ["renderer"] = TipoRenderer(fr),
                ["encabezado"] = Encabezado(fr as ILegendInfo),
                ["transparencia"] = fx != null && fx.SupportsTransparency ? (int)fx.Transparency : (JToken)null,
                ["clases"] = clases
            };
            if (antes != null)
                r["antes"] = antes;
            return r;
        }

        /// <summary>Un relleno que no pinta: color nulo, o un relleno simple con el estilo
        /// «Hollow» (Style = esriSFSNull), cuyo Color es un valor muerto.</summary>
        private static bool RellenoHueco(IFillSymbol relleno)
        {
            if (relleno.Color == null || relleno.Color.NullColor)
                return true;
            ISimpleFillSymbol simple = relleno as ISimpleFillSymbol;
            return simple != null && simple.Style == esriSimpleFillStyle.esriSFSNull;
        }

        private static JObject Describir(ISymbol sym)
        {
            var d = new JObject { ["tipo_simbolo"] = sym == null ? null : TipoSimbolo(sym) };
            // Un símbolo de varias capas se leía como «relleno» a secas, y patron_linea o trama
            // fallaban luego sin aviso previo (prueba desde el cliente, 2026-10-10).
            int capas = CapasDe(sym);
            if (capas > 0)
                d["capas_simbolo"] = capas;
            IFillSymbol relleno = sym as IFillSymbol;
            ILineSymbol linea = sym as ILineSymbol;
            IMarkerSymbol marca = sym as IMarkerSymbol;
            if (relleno != null)
            {
                // Un relleno simple con Style = esriSFSNull no pinta nada, pero su Color conserva
                // el negro por defecto: leyendo solo el color, un polígono hueco salía como
                // «relleno negro» (issue #8, 2026-10-05). El Style manda sobre el color.
                bool hueco = RellenoHueco(relleno);
                d["color_relleno"] = hueco ? null : Parametros.Hex(relleno.Color);
                d["sin_relleno"] = hueco;
                ILineSymbol borde = relleno.Outline;
                d["color_borde"] = borde != null ? Parametros.Hex(borde.Color) : null;
                d["grosor_borde"] = borde != null ? borde.Width : 0.0;
                if (borde != null)
                    d["patron_borde"] = PatronDe(borde);
                int capasBorde = CapasDe(borde as ISymbol);
                if (capasBorde > 0)
                    d["capas_borde"] = capasBorde;
                ILineFillSymbol trama = sym as ILineFillSymbol;
                if (trama != null)
                    d["trama"] = new JObject
                    {
                        ["angulo"] = trama.Angle,
                        ["separacion"] = trama.Separation,
                        ["color"] = trama.LineSymbol != null ? Parametros.Hex(trama.LineSymbol.Color) : null,
                        ["grosor"] = trama.LineSymbol != null ? trama.LineSymbol.Width : 0.0
                    };
            }
            else if (linea != null)
            {
                d["color_borde"] = Parametros.Hex(linea.Color);
                d["grosor_borde"] = linea.Width;
                d["patron_linea"] = PatronDe(linea);
            }
            else if (marca != null)
            {
                d["color_relleno"] = Parametros.Hex(marca.Color);
                d["tamano"] = marca.Size;
                ISimpleMarkerSymbol simple = sym as ISimpleMarkerSymbol;
                if (simple != null)
                    d["estilo_marcador"] = EstilosMarcador.Where(kv => kv.Value == simple.Style)
                        .Select(kv => kv.Key).FirstOrDefault() ?? simple.Style.ToString();
                if (simple != null && simple.Outline)
                {
                    d["color_borde"] = Parametros.Hex(simple.OutlineColor);
                    d["grosor_borde"] = simple.OutlineSize;
                }
            }
            return d;
        }

        /// <summary>Cuántas capas tiene un símbolo de VARIAS capas (relleno, línea o marcador);
        /// 0 si no lo es. Un multicapa con una sola capa también cuenta: patron_linea y trama
        /// lo rechazan igual.</summary>
        private static int CapasDe(ISymbol sym)
        {
            IMultiLayerFillSymbol mf = sym as IMultiLayerFillSymbol;
            if (mf != null) return mf.LayerCount;
            IMultiLayerLineSymbol ml = sym as IMultiLayerLineSymbol;
            if (ml != null) return ml.LayerCount;
            IMultiLayerMarkerSymbol mm = sym as IMultiLayerMarkerSymbol;
            if (mm != null) return mm.LayerCount;
            return 0;
        }

        private static string TipoSimbolo(ISymbol sym)
        {
            if (sym is ISimpleFillSymbol) return "relleno_simple";
            if (sym is IFillSymbol) return "relleno";
            if (sym is ISimpleLineSymbol) return "linea_simple";
            if (sym is ILineSymbol) return "linea";
            if (sym is ISimpleMarkerSymbol) return "marcador_simple";
            if (sym is IMarkerSymbol) return "marcador";
            return "otro";
        }
    }
}
