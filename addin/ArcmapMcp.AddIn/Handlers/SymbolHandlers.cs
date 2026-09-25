using System;
using System.Collections.Generic;
using System.Globalization;
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
            IMxDocument doc;
            IMap map;
            IGeoFeatureLayer gfl = CapaDeEntidades(parameters, out doc, out map);
            esriGeometryType shp = Geometria(gfl);
            Cambios c = LeerCambios(parameters, shp);

            ISymbol sym = SimboloNuevo(shp);
            Aplicar(sym, c);
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
            IMxDocument doc;
            IMap map;
            IGeoFeatureLayer gfl = CapaDeEntidades(parameters, out doc, out map);
            esriGeometryType shp = Geometria(gfl);
            Cambios c = LeerCambios(parameters, shp);
            if (!c.AlgoQueCambiar)
                return Protocol.Result(Estado(gfl, null));

            string categoria = Parametros.Dado(parameters["categoria"]) ? (string)parameters["categoria"] : null;
            JObject antes = Estado(gfl, null);
            IFeatureRenderer fr = gfl.Renderer;

            ISimpleRenderer simple = fr as ISimpleRenderer;
            IUniqueValueRenderer unicos = fr as IUniqueValueRenderer;
            IClassBreaksRenderer rangos = fr as IClassBreaksRenderer;
            var tocadas = new List<string>();

            if (simple != null)
            {
                if (categoria != null)
                    throw new ArgumentException("La capa tiene símbolo único: no hay categorías que elegir."
                        + " Quita 'categoria'.");
                simple.Symbol = Retocado(simple.Symbol, c);
                tocadas.Add("(símbolo único)");
            }
            else if (unicos != null || rangos != null)
            {
                List<Clase> clases = Clases(unicos, rangos);
                List<Clase> objetivo;
                if (categoria != null)
                {
                    objetivo = clases.FindAll(k => string.Equals(k.Valor, categoria, StringComparison.OrdinalIgnoreCase)
                                                   || string.Equals(k.Etiqueta, categoria, StringComparison.OrdinalIgnoreCase));
                    if (objetivo.Count == 0)
                        throw new ArgumentException("No hay ninguna categoría '" + categoria + "'. Categorías (valor ="
                            + " etiqueta): " + string.Join(" | ", clases.ConvertAll(k => k.Valor + " = " + k.Etiqueta)));
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
                foreach (Clase k in objetivo)
                {
                    k.Poner(Retocado(k.Leer(), c));
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
            return Protocol.Result(r);
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

            public bool AlgoQueCambiar
            {
                get
                {
                    return ColorRelleno != null || ColorBorde != null || GrosorBorde != null
                        || Tamano != null || SinRelleno || Transparencia != null;
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
            return c;
        }

        private static IGeoFeatureLayer CapaDeEntidades(JObject parameters, out IMxDocument doc, out IMap map)
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

        private static esriGeometryType Geometria(IGeoFeatureLayer gfl)
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
            Aplicar(copia, c);
            return copia;
        }

        private static void Aplicar(ISymbol sym, Cambios c)
        {
            IFillSymbol relleno = sym as IFillSymbol;
            ILineSymbol linea = sym as ILineSymbol;
            IMarkerSymbol marca = sym as IMarkerSymbol;
            if (relleno != null)
            {
                if (c.SinRelleno)
                    relleno.Color = new RgbColorClass { NullColor = true };
                else if (c.ColorRelleno != null)
                    relleno.Color = c.ColorRelleno;
                if (c.ColorBorde != null || c.GrosorBorde != null)
                {
                    ILineSymbol borde = relleno.Outline ?? new SimpleLineSymbolClass();
                    if (c.ColorBorde != null) borde.Color = c.ColorBorde;
                    if (c.GrosorBorde != null) borde.Width = c.GrosorBorde.Value;
                    relleno.Outline = borde; // se reasigna siempre: no se da por hecho que el getter devuelva el original
                }
            }
            else if (linea != null)
            {
                if (c.ColorBorde != null) linea.Color = c.ColorBorde;
                if (c.GrosorBorde != null) linea.Width = c.GrosorBorde.Value;
            }
            else if (marca != null)
            {
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
            public string Etiqueta;
            public Func<ISymbol> Leer;
            public Action<ISymbol> Poner;
        }

        private static List<Clase> Clases(IUniqueValueRenderer unicos, IClassBreaksRenderer rangos)
        {
            var salida = new List<Clase>();
            if (unicos != null)
            {
                for (int i = 0; i < unicos.ValueCount; i++)
                {
                    string v = unicos.get_Value(i);
                    string etiqueta = unicos.get_Label(v);
                    salida.Add(new Clase
                    {
                        Valor = v,
                        Etiqueta = string.IsNullOrEmpty(etiqueta) ? v : etiqueta,
                        Leer = () => unicos.get_Symbol(v),
                        Poner = s => unicos.set_Symbol(v, s)
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
                        Poner = s => rangos.set_Symbol(k, s)
                    });
                }
            }
            return salida;
        }

        private static string TipoRenderer(IFeatureRenderer fr)
        {
            if (fr is ISimpleRenderer) return "simple";
            if (fr is IUniqueValueRenderer) return "valores_unicos";
            if (fr is IClassBreaksRenderer) return "rangos";
            return fr == null ? "ninguno" : fr.GetType().Name;
        }

        private static JObject Estado(IGeoFeatureLayer gfl, JObject antes)
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
                    clases.Add(s);
                }
            }
            ILayerEffects fx = gfl as ILayerEffects;
            var r = new JObject
            {
                ["capa"] = ((ILayer)gfl).Name,
                ["renderer"] = TipoRenderer(fr),
                ["transparencia"] = fx != null && fx.SupportsTransparency ? (int)fx.Transparency : (JToken)null,
                ["clases"] = clases
            };
            if (antes != null)
                r["antes"] = antes;
            return r;
        }

        private static JObject Describir(ISymbol sym)
        {
            var d = new JObject { ["tipo_simbolo"] = sym == null ? null : TipoSimbolo(sym) };
            IFillSymbol relleno = sym as IFillSymbol;
            ILineSymbol linea = sym as ILineSymbol;
            IMarkerSymbol marca = sym as IMarkerSymbol;
            if (relleno != null)
            {
                d["color_relleno"] = Parametros.Hex(relleno.Color);
                d["sin_relleno"] = relleno.Color == null || relleno.Color.NullColor;
                ILineSymbol borde = relleno.Outline;
                d["color_borde"] = borde != null ? Parametros.Hex(borde.Color) : null;
                d["grosor_borde"] = borde != null ? borde.Width : 0.0;
            }
            else if (linea != null)
            {
                d["color_borde"] = Parametros.Hex(linea.Color);
                d["grosor_borde"] = linea.Width;
            }
            else if (marca != null)
            {
                d["color_relleno"] = Parametros.Hex(marca.Color);
                d["tamano"] = marca.Size;
                ISimpleMarkerSymbol simple = sym as ISimpleMarkerSymbol;
                if (simple != null && simple.Outline)
                {
                    d["color_borde"] = Parametros.Hex(simple.OutlineColor);
                    d["grosor_borde"] = simple.OutlineSize;
                }
            }
            return d;
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
