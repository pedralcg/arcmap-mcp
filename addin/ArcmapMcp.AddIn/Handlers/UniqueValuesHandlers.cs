using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.Geodatabase;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Simbología CATEGÓRICA por valores únicos, complemento de la graduada.
    ///
    /// Antes de esto la única vía era preparar un .lyr plantilla a mano y
    /// aplicarlo con apply_symbology_from_layer, o sea salir de la sesión para
    /// cada leyenda nueva.
    /// </summary>
    internal static class UniqueValuesHandlers
    {
        /// <summary>Tope de categorías. Una leyenda de miles de entradas no es una
        /// leyenda: es un cuelgue. Mejor fallar diciendo el número real.</summary>
        private const int MaxCategorias = 100;

        public static JObject SetUniqueValuesSymbology(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string campo = (string)parameters["campo"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(campo))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC) y 'campo' (campo por el que categorizar).");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer encontrada = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer gfl = encontrada as IGeoFeatureLayer;
            if (gfl == null)
            {
                string queEs = encontrada == null
                    ? "no existe ninguna capa con ese nombre en la TOC"
                    : (encontrada is IRasterLayer
                        ? "es un RÁSTER; para eso usa set_raster_symbology"
                        : "no es una capa de entidades");
                throw new ArgumentException("'" + capa + "': " + queEs + ".");
            }

            IFeatureClass fc = gfl.FeatureClass;
            if (fc == null)
                throw new ArgumentException("La capa no tiene fuente de datos accesible (¿rota?): " + capa);

            // El campo se busca en la DISPLAY TABLE, no en la clase base: los campos
            // de un join existen en la capa (y list_fields los ofrece) pero no en la
            // feature class, así que categorizar por uno daba "Campo no encontrado".
            IFields campos = QueryHandlers.CamposDeCapa(encontrada);
            int idx = campos.FindField(campo);
            if (idx < 0)
                throw new ArgumentException("Campo no encontrado en la capa '" + capa + "': " + campo
                    + ". Disponibles: " + NombresDeCampos(campos));
            bool esNumerico = EsCampoNumerico(campos.get_Field(idx).Type);

            bool excedeTope;
            List<string> valores = ValoresUnicos(encontrada, campo, esNumerico, out excedeTope);
            if (valores.Count == 0)
                throw new ArgumentException("El campo '" + campo + "' no tiene ningún valor: nada que categorizar.");
            if (excedeTope)
                throw new ArgumentException("El campo '" + campo + "' tiene más de " + MaxCategorias
                    + " valores distintos, por encima del tope. Una leyenda así no es legible: agrupa "
                    + "el campo antes, o usa set_graduated_symbology si el campo es numérico y continuo.");

            esriGeometryType shp = fc.ShapeType;
            double tam = LeerDouble(parameters["tamano"],
                shp == esriGeometryType.esriGeometryPolyline ? 2.0
                : shp == esriGeometryType.esriGeometryPolygon ? 0.4 : 6.0);

            IColor[] paleta = Paleta(parameters, valores.Count);

            IUniqueValueRenderer render = new UniqueValueRendererClass();
            render.FieldCount = 1;
            render.set_Field(0, campo);
            // Sin símbolo por defecto: si aparece un valor nuevo que no está en la
            // leyenda, es mejor que NO se dibuje a que se cuele con un color
            // cualquiera y parezca una categoría más.
            render.UseDefaultSymbol = false;

            for (int i = 0; i < valores.Count; i++)
            {
                ISymbol simbolo = Simbolo(shp, paleta[i], tam);
                render.AddValue(valores[i], campo, simbolo);
                render.set_Label(valores[i], valores[i]);
            }

            gfl.Renderer = (IFeatureRenderer)render;
            MapHandlers.NotificarCambioContenido(map, doc);

            var lista = new JArray();
            foreach (string v in valores)
                lista.Add(v);

            return Protocol.Result(new JObject
            {
                ["capa"] = gfl.Name,
                ["campo"] = campo,
                ["num_categorias"] = valores.Count,
                ["valores"] = lista,
            });
        }

        /// <summary>
        /// Valores distintos del campo, vía IDataStatistics.
        ///
        /// El cursor sale de la capa (display table), no de `fc.Search(null, false)`:
        /// así honra la definition query —una capa filtrada a un municipio no debe
        /// sacar en la leyenda las categorías de toda la región—, ve los campos de
        /// los joins, pide SOLO el campo con SubFields y recicla las filas, en vez de
        /// arrastrar la geometría de toda la tabla. La SELECCIÓN no se honra a
        /// propósito: una leyenda que solo cubriera lo seleccionado dejaría sin
        /// pintar el resto de la capa.
        ///
        /// Se corta al pasar del tope en vez de recorrer la tabla entera para poder
        /// decir el número exacto: sobre una tabla grande ese recuento exacto cuesta
        /// el mismo cuelgue que se quería evitar.
        /// </summary>
        private static List<string> ValoresUnicos(ILayer lyr, string campo, bool esNumerico,
            out bool excedeTope)
        {
            var salida = new List<string>();
            excedeTope = false;
            ICursor cursor = QueryHandlers.CursorDisplayTable(lyr, null, campo);
            try
            {
                IDataStatistics stats = new DataStatisticsClass
                {
                    Field = campo,
                    Cursor = cursor
                };
                IEnumerator e = stats.UniqueValues;
                e.Reset();
                while (e.MoveNext())
                {
                    object v = e.Current;
                    // NULL se salta a propósito: AddValue con null revienta, y una
                    // categoría "nada" no aporta.
                    if (v == null || v == DBNull.Value)
                        continue;
                    salida.Add(Convert.ToString(v, CultureInfo.InvariantCulture));
                    if (salida.Count > MaxCategorias)
                    {
                        excedeTope = true;
                        break;
                    }
                }
            }
            finally
            {
                // Los cursores de ArcObjects hay que soltarlos o dejan lock sobre la
                // fuente; con shapefiles en red eso se nota enseguida.
                DataAccess.SoltarCom(cursor);
            }
            // Un campo numérico se ordena como número: por texto salía 1, 10, 11, 2 y
            // la leyenda quedaba ilegible justo donde más se nota (estratos, códigos).
            if (esNumerico)
                salida.Sort(CompararNumerico);
            else
                salida.Sort(StringComparer.OrdinalIgnoreCase);
            return salida;
        }

        private static int CompararNumerico(string a, string b)
        {
            double na, nb;
            bool okA = double.TryParse(a, NumberStyles.Any, CultureInfo.InvariantCulture, out na);
            bool okB = double.TryParse(b, NumberStyles.Any, CultureInfo.InvariantCulture, out nb);
            if (okA && okB)
                return na.CompareTo(nb);
            if (okA != okB)
                return okA ? -1 : 1; // lo que no parsea, al final
            return string.Compare(a, b, StringComparison.OrdinalIgnoreCase);
        }

        private static bool EsCampoNumerico(esriFieldType tipo)
        {
            return tipo == esriFieldType.esriFieldTypeSmallInteger
                || tipo == esriFieldType.esriFieldTypeInteger
                || tipo == esriFieldType.esriFieldTypeSingle
                || tipo == esriFieldType.esriFieldTypeDouble;
        }

        private static string NombresDeCampos(IFields campos)
        {
            var nombres = new List<string>();
            for (int i = 0; i < campos.FieldCount; i++)
                nombres.Add(campos.get_Field(i).Name);
            return string.Join(", ", nombres);
        }

        /// <summary>
        /// Paleta para categorías. Por defecto tonos bien separados en el círculo
        /// cromático: en categórico lo que importa es DISTINGUIR, no ordenar, así
        /// que una rampa secuencial (la de la graduada) sería justo lo contrario.
        /// Si se pasan color_desde y color_hasta se respeta esa rampa.
        ///
        /// La rampa explícita se construye con el MISMO helper que el ráster y por
        /// tanto en CIE Lab por defecto, no en HSV: HSV interpola el tono dando la
        /// vuelta a la rueda de color y devuelve un arcoíris (medido el 2026-09-04,
        /// ver RasterSymbologyHandlers.LeerAlgoritmo). `algoritmo` permite pedir hsv
        /// a propósito, igual que en el ráster.
        /// </summary>
        private static IColor[] Paleta(JObject parameters, int n)
        {
            bool rampaExplicita = EsDado(parameters["color_desde"]) || EsDado(parameters["color_hasta"]);

            if (rampaExplicita)
            {
                IColor desde = Parametros.LeerColor(parameters["color_desde"], "color_desde", 255, 255, 178);
                IColor hasta = Parametros.LeerColor(parameters["color_hasta"], "color_hasta", 189, 0, 38);
                return RasterSymbologyHandlers.ConstruirRampa(desde, hasta, n,
                    RasterSymbologyHandlers.LeerAlgoritmo(parameters["algoritmo"]));
            }

            // Tonos repartidos por el círculo, saturación y valor fijos. Reproducible
            // a propósito: la misma capa con el mismo campo sale siempre igual, que
            // es lo que se espera al reexportar una serie de planos.
            var salida = new IColor[n];
            for (int i = 0; i < n; i++)
            {
                double hue = (360.0 * i) / n;
                salida[i] = DesdeHsv(hue, 0.65, 0.90);
            }
            return salida;
        }

        private static bool EsDado(JToken t)
        {
            return t != null && t.Type != JTokenType.Null;
        }

        private static IColor DesdeHsv(double h, double s, double v)
        {
            double c = v * s;
            double x = c * (1 - Math.Abs(((h / 60.0) % 2) - 1));
            double m = v - c;
            double r1, g1, b1;
            if (h < 60) { r1 = c; g1 = x; b1 = 0; }
            else if (h < 120) { r1 = x; g1 = c; b1 = 0; }
            else if (h < 180) { r1 = 0; g1 = c; b1 = x; }
            else if (h < 240) { r1 = 0; g1 = x; b1 = c; }
            else if (h < 300) { r1 = x; g1 = 0; b1 = c; }
            else { r1 = c; g1 = 0; b1 = x; }
            return new RgbColorClass
            {
                Red = (int)Math.Round((r1 + m) * 255),
                Green = (int)Math.Round((g1 + m) * 255),
                Blue = (int)Math.Round((b1 + m) * 255)
            };
        }

        private static ISymbol Simbolo(esriGeometryType shp, IColor color, double tam)
        {
            switch (shp)
            {
                case esriGeometryType.esriGeometryPolygon:
                    ISimpleLineSymbol borde = new SimpleLineSymbolClass
                    {
                        Color = new RgbColorClass { Red = 110, Green = 110, Blue = 110 },
                        Width = tam
                    };
                    ISimpleFillSymbol relleno = new SimpleFillSymbolClass
                    {
                        Color = color,
                        Style = esriSimpleFillStyle.esriSFSSolid,
                        Outline = borde
                    };
                    return (ISymbol)relleno;

                case esriGeometryType.esriGeometryPolyline:
                    ISimpleLineSymbol linea = new SimpleLineSymbolClass
                    {
                        Color = color,
                        Width = tam,
                        Style = esriSimpleLineStyle.esriSLSSolid
                    };
                    return (ISymbol)linea;

                default:
                    ISimpleMarkerSymbol punto = new SimpleMarkerSymbolClass
                    {
                        Color = color,
                        Size = tam,
                        Style = esriSimpleMarkerStyle.esriSMSCircle
                    };
                    return (ISymbol)punto;
            }
        }

        private static double LeerDouble(JToken t, double porDefecto)
        {
            return t == null || t.Type == JTokenType.Null ? porDefecto : (double)t;
        }
    }
}
