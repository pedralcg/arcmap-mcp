using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
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
            string campo = Parametros.Dado(parameters["campo"]) ? (string)parameters["campo"] : null;
            JArray tCampos = parameters["campos"] as JArray;
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");
            if (tCampos != null && tCampos.Count > 0)
            {
                if (campo != null)
                    throw new ArgumentException("Indica 'campo' o 'campos', no los dos.");
                if (tCampos.Count > 3)
                    throw new ArgumentException("'campos' admite hasta 3 campos (el máximo de ArcMap).");
                if (tCampos.Count > 1)
                    return ValoresUnicosVariosCampos(parameters, capa, tCampos.Select(t => (string)t).ToList());
                campo = (string)tCampos[0];
            }
            if (string.IsNullOrEmpty(campo))
                throw new ArgumentException("Indica 'campo' (o 'campos', hasta 3) por el que categorizar.");
            if (Parametros.Dado(parameters["clases"]) || Parametros.Dado(parameters["resto"]))
                return ValoresUnicosVariosCampos(parameters, capa, new List<string> { campo });

            Documento doc;
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
        /// Valores únicos por VARIOS campos (hasta 3, como «ANIO_ACT | Actuacion») y/o con
        /// clases EXPLÍCITAS: varios valores en una clase (AddReferenceValue), su etiqueta y
        /// su color, y «todos los demás valores» con `resto`.
        ///
        /// La respuesta se lee del RENDERER: arcpy devuelve `classValues` vacío en un
        /// renderer de varios campos, así que no sirve para comprobar nada.
        /// </summary>
        private static JObject ValoresUnicosVariosCampos(JObject parameters, string capa, List<string> campos)
        {
            string delim = Parametros.Dado(parameters["delimitador"]) ? (string)parameters["delimitador"] : ", ";
            if (campos.Count > 1 && string.IsNullOrEmpty(delim))
                throw new ArgumentException("'delimitador' vacío: con varios campos los valores no se podrían separar.");
            JArray tClases = parameters["clases"] as JArray;
            if (Parametros.Dado(parameters["clases"]) && (tClases == null || tClases.Count == 0))
                throw new ArgumentException("'clases' es una lista: [{\"valores\": [...], \"etiqueta\": \"...\", \"color\": \"#RRGGBB\"}].");
            string resto = Parametros.Dado(parameters["resto"]) ? (string)parameters["resto"] : null;

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer encontrada = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer gfl = encontrada as IGeoFeatureLayer;
            if (gfl == null || gfl.FeatureClass == null)
                throw new ArgumentException("'" + capa + "' no es una capa de entidades con fuente accesible.");
            IFields campos0 = QueryHandlers.CamposDeCapa(encontrada);
            foreach (string c in campos)
                if (campos0.FindField(c) < 0)
                    throw new ArgumentException("Campo no encontrado en la capa '" + capa + "': " + c
                        + ". Disponibles: " + NombresDeCampos(campos0));

            // Los valores que hay en los datos: para el modo automático son las clases; con
            // clases explícitas, sirven para decir qué se queda sin clase.
            bool excedeTope;
            List<string> enDatos = ValoresCombinados(encontrada, campos, delim, out excedeTope);

            esriGeometryType shp = gfl.FeatureClass.ShapeType;
            double tam = LeerDouble(parameters["tamano"],
                shp == esriGeometryType.esriGeometryPolyline ? 2.0
                : shp == esriGeometryType.esriGeometryPolygon ? 0.4 : 6.0);

            // (valores de la clase, etiqueta, color o null)
            var clases = new List<Tuple<List<string>, string, IColor>>();
            if (tClases != null)
            {
                var vistos = new HashSet<string>();
                foreach (JToken t in tClases)
                {
                    JObject o = t as JObject;
                    if (o == null || !Parametros.Dado(o["valores"]))
                        throw new ArgumentException("Cada clase es {\"valores\": [...], \"etiqueta\": ..., \"color\": ...}.");
                    JArray vs = o["valores"] as JArray;
                    List<string> valores = vs != null
                        ? vs.Select(v => Convert.ToString(((JValue)v).Value, CultureInfo.InvariantCulture)).ToList()
                        : new List<string> { Convert.ToString(((JValue)o["valores"]).Value, CultureInfo.InvariantCulture) };
                    if (valores.Count == 0)
                        throw new ArgumentException("Una clase sin 'valores'.");
                    foreach (string v in valores)
                        if (!vistos.Add(v))
                            throw new ArgumentException("El valor '" + v + "' está en dos clases: cada valor va a una sola.");
                    string etiqueta = Parametros.Dado(o["etiqueta"]) ? (string)o["etiqueta"] : valores[0];
                    IColor color = Parametros.Dado(o["color"]) ? Parametros.LeerColor(o["color"], "color", 0, 0, 0) : null;
                    clases.Add(Tuple.Create(valores, etiqueta, color));
                }
                if (clases.Count > MaxCategorias)
                    throw new ArgumentException("Más de " + MaxCategorias + " clases: una leyenda así no es legible.");
            }
            else
            {
                if (enDatos.Count == 0)
                    throw new ArgumentException("Los campos no tienen ningún valor: nada que categorizar.");
                if (excedeTope)
                    throw new ArgumentException("Hay más de " + MaxCategorias + " combinaciones distintas, por encima"
                        + " del tope. Agrupa con 'clases' o reduce los campos.");
                foreach (string v in enDatos)
                    clases.Add(Tuple.Create(new List<string> { v }, v, (IColor)null));
            }

            IColor[] paleta = Paleta(parameters, clases.Count);
            IUniqueValueRenderer render = new UniqueValueRendererClass();
            render.FieldCount = campos.Count;
            for (int i = 0; i < campos.Count; i++)
                render.set_Field(i, campos[i]);
            if (campos.Count > 1)
                render.FieldDelimiter = delim;
            string encabezado = string.Join(delim, campos);
            for (int i = 0; i < clases.Count; i++)
            {
                List<string> vs = clases[i].Item1;
                render.AddValue(vs[0], encabezado, Simbolo(shp, clases[i].Item3 ?? paleta[i], tam));
                render.set_Label(vs[0], clases[i].Item2);
                for (int k = 1; k < vs.Count; k++)
                    render.AddReferenceValue(vs[k], vs[0]);
            }
            // Primero el símbolo y la etiqueta, después el interruptor.
            if (resto != null)
            {
                render.DefaultSymbol = Simbolo(shp, new RgbColorClass { Red = 200, Green = 200, Blue = 200 }, tam);
                render.DefaultLabel = resto;
            }
            render.UseDefaultSymbol = resto != null;

            gfl.Renderer = (IFeatureRenderer)render;
            MapHandlers.NotificarCambioContenido(map, doc);

            // Leído del renderer que ha quedado en la capa.
            IUniqueValueRenderer leido = (IUniqueValueRenderer)gfl.Renderer;
            var cubiertos = new HashSet<string>();
            var salida = new JArray();
            foreach (var g in Grupos(leido))
            {
                foreach (string v in g.Item2) cubiertos.Add(v);
                salida.Add(new JObject
                {
                    ["valor"] = g.Item1,
                    ["etiqueta"] = leido.get_Label(g.Item1),
                    ["valores"] = new JArray(g.Item2.ToArray())
                });
            }
            var r = new JObject
            {
                ["capa"] = gfl.Name,
                ["campos"] = new JArray(campos.ToArray()),
                ["delimitador"] = campos.Count > 1 ? leido.FieldDelimiter : null,
                ["num_categorias"] = salida.Count,
                ["clases"] = salida,
                ["resto"] = leido.UseDefaultSymbol ? leido.DefaultLabel : null
            };
            if (excedeTope)
                r["valores_sin_clase"] = "no comprobado: los datos tienen más de " + MaxCategorias + " combinaciones";
            else
            {
                var sinClase = enDatos.Where(v => !cubiertos.Contains(v)).ToList();
                r["valores_sin_clase"] = new JArray(sinClase.ToArray());
                if (sinClase.Count > 0)
                    r["aviso"] = sinClase.Count + " valor(es) de los datos no tienen clase y "
                        + (leido.UseDefaultSymbol ? "van a «" + leido.DefaultLabel + "»." : "NO se dibujan (pasa 'resto' para darles una).");
                r["clases_sin_entidades"] = new JArray(cubiertos.Where(v => !enDatos.Contains(v)).ToArray());
            }
            return Protocol.Result(r);
        }

        /// <summary>
        /// Las clases de un renderer de valores únicos: cada valor principal con los que
        /// tiene agrupados (AddReferenceValue). IUniqueValueRenderer enumera TODOS los
        /// valores, también los agrupados; ReferenceValue(v) dice a qué valor pertenece
        /// uno agrupado. Sin esto, edit_symbol listaría cada valor agrupado como una clase.
        /// </summary>
        internal static List<Tuple<string, List<string>>> Grupos(IUniqueValueRenderer uvr)
        {
            var orden = new List<string>();
            var grupos = new Dictionary<string, List<string>>();
            var principalDe = new Dictionary<string, string>();
            for (int j = 0; j < uvr.ValueCount; j++)
            {
                string v = uvr.get_Value(j);
                string r = null;
                try { r = uvr.get_ReferenceValue(v); } catch { }
                principalDe[v] = string.IsNullOrEmpty(r) ? v : r;
            }
            foreach (var kv in principalDe)
            {
                string principal = kv.Value;
                // Un principal que a su vez fuera referencia no se encadena: se trata como propio.
                if (!principalDe.ContainsKey(principal))
                    principal = kv.Key;
                if (!grupos.ContainsKey(principal))
                {
                    grupos[principal] = new List<string> { principal };
                    orden.Add(principal);
                }
                if (kv.Key != principal)
                    grupos[principal].Add(kv.Key);
            }
            // El orden es el del renderer (el de la leyenda), por el principal.
            var salida = new List<Tuple<string, List<string>>>();
            for (int j = 0; j < uvr.ValueCount; j++)
            {
                string v = uvr.get_Value(j);
                if (grupos.ContainsKey(v) && !salida.Any(t => t.Item1 == v))
                    salida.Add(Tuple.Create(v, grupos[v]));
            }
            return salida;
        }

        /// <summary>Combinaciones distintas de varios campos (o valores de uno), unidas con
        /// el delimitador del renderer. Mismo cursor que ValoresUnicos: display table,
        /// definition query, solo esos campos, y corte al pasar del tope.</summary>
        private static List<string> ValoresCombinados(ILayer lyr, List<string> campos, string delim, out bool excedeTope)
        {
            excedeTope = false;
            var vistos = new HashSet<string>();
            var salida = new List<string>();
            ICursor cursor = QueryHandlers.CursorDisplayTable(lyr, null, string.Join(",", campos));
            try
            {
                int[] idx = campos.Select(c => cursor.Fields.FindField(c)).ToArray();
                IRow fila;
                while ((fila = cursor.NextRow()) != null)
                {
                    var partes = new List<string>();
                    bool hayNulo = false;
                    foreach (int i in idx)
                    {
                        object v = fila.get_Value(i);
                        if (v == null || v == DBNull.Value) { hayNulo = true; break; }
                        partes.Add(Convert.ToString(v, CultureInfo.InvariantCulture));
                    }
                    if (hayNulo)
                        continue;
                    string clave = string.Join(delim, partes);
                    if (vistos.Add(clave))
                    {
                        salida.Add(clave);
                        if (salida.Count > MaxCategorias)
                        {
                            excedeTope = true;
                            break;
                        }
                    }
                }
            }
            finally
            {
                DataAccess.SoltarCom(cursor);
            }
            salida.Sort(StringComparer.OrdinalIgnoreCase);
            return salida;
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
