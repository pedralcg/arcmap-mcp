using System;
using System.Globalization;
using System.IO;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.Geodatabase;
using ESRI.ArcGIS.Geometry;
using ESRI.ArcGIS.esriSystem;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// add_layer / remove_layer / apply_symbology_from_layer — contrato JSON que
    /// esperan los schemas del servidor MCP. Equivalen al Layer(ruta)/AddLayer/
    /// RemoveLayer/UpdateLayer de arcpy.mapping; aquí: factory por tipo de fuente
    /// + IMapLayers, y renderer clonado desde el .lyr vía IGeoFeatureLayer
    /// (UpdateLayer no existe en ArcObjects .NET).
    /// </summary>
    internal static class LayerHandlers
    {
        public static JObject AddLayer(JObject parameters)
        {
            string fuente = (string)parameters["fuente"];
            if (string.IsNullOrEmpty(fuente))
                throw new ArgumentException("Indica 'fuente' (ruta a .shp, feature class de .gdb o ráster).");
            string posicion = ((string)parameters["posicion"] ?? "TOP").ToUpperInvariant();
            if (posicion != "TOP" && posicion != "BOTTOM" && posicion != "AUTO_ARRANGE")
                throw new ArgumentException("'posicion' debe ser TOP, BOTTOM o AUTO_ARRANGE.");
            string grupo = (string)parameters["grupo"];

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer nueva = DataAccess.CrearCapaDesdeRuta(fuente);
            IMapLayers mapLayers = (IMapLayers)map;

            if (!string.IsNullOrEmpty(grupo))
            {
                IGroupLayer grp = MapHandlers.FindLayer(map, grupo) as IGroupLayer;
                if (grp == null)
                    throw new ArgumentException("La capa indicada en 'grupo' no es una capa de grupo: " + grupo);
                if (posicion == "AUTO_ARRANGE")
                    mapLayers.InsertLayerInGroup(grp, nueva, true, -1);
                else
                    mapLayers.InsertLayerInGroup(grp, nueva, false,
                        posicion == "BOTTOM" ? ((ICompositeLayer)grp).Count : 0);
            }
            else
            {
                if (posicion == "AUTO_ARRANGE")
                    mapLayers.InsertLayer(nueva, true, -1);
                else
                    mapLayers.InsertLayer(nueva, false, posicion == "BOTTOM" ? map.LayerCount : 0);
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = nueva.Name,
                ["fuente"] = fuente,
                ["posicion"] = posicion,
                ["grupo"] = grupo
            });
        }

        public static JObject RemoveLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);

            // Si la capa vive dentro de un grupo hay que borrarla del grupo;
            // IMap.DeleteLayer solo garantiza las de primer nivel.
            IGroupLayer padre = BuscarGrupoPadre(map, lyr);
            if (padre != null)
                padre.Delete(lyr);
            else
                map.DeleteLayer(lyr);

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = capa,
                ["eliminada"] = true
            });
        }

        public static JObject ApplySymbologyFromLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string lyrFile = (string)parameters["lyr_file"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(lyrFile))
                throw new ArgumentException("Indica 'capa' y 'lyr_file' (ruta al .lyr de origen).");
            if (!File.Exists(lyrFile))
                throw new ArgumentException("No existe el archivo .lyr: " + lyrFile);

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            IGeoFeatureLayer destino = MapHandlers.FindLayer(map, capa) as IGeoFeatureLayer;
            if (destino == null)
                throw new ArgumentException("Solo capas de entidades admiten simbología desde .lyr: " + capa);

            ILayerFile lf = new LayerFileClass();
            lf.Open(lyrFile);
            try
            {
                IGeoFeatureLayer origen = PrimerGeoFeatureLayer(lf.Layer);
                if (origen == null)
                    throw new ArgumentException("El .lyr no contiene una capa de entidades: " + lyrFile);

                // Geometrías incompatibles → el renderer no aplicaría (mismo guard
                // implícito del UpdateLayer de arcpy.mapping). Si una fuente está
                // rota no se puede comprobar: se intenta igualmente.
                try
                {
                    if (destino.FeatureClass != null && origen.FeatureClass != null
                        && destino.FeatureClass.ShapeType != origen.FeatureClass.ShapeType)
                        throw new ArgumentException("Geometrías incompatibles entre la capa ("
                            + DataAccess.NombreTipoGeometria(destino.FeatureClass.ShapeType)
                            + ") y el .lyr (" + DataAccess.NombreTipoGeometria(origen.FeatureClass.ShapeType) + ").");
                }
                catch (ArgumentException) { throw; }
                catch { /* FeatureClass inaccesible: no bloquear por el guard */ }

                // Clonar el renderer para no dejar referencias vivas al layer file.
                IObjectCopy copia = new ObjectCopyClass();
                destino.Renderer = (IFeatureRenderer)copia.Copy(origen.Renderer);
            }
            finally
            {
                try { lf.Close(); } catch { }
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = capa,
                ["lyr_origen"] = lyrFile
            });
        }

        /// <summary>
        /// Simbología graduada (class breaks) sobre la capa VIVA, por un campo
        /// numérico. Cubre el hueco que no tapan las tools nativas: execute_arcpy no
        /// puede cambiar el renderer de la sesión (opera sobre una copia), y
        /// apply_symbology_from_layer necesita un .lyr plantilla. Aquí se construye
        /// el IClassBreaksRenderer directamente y se aplica in-process.
        ///
        /// El histograma se calcula sobre TODOS los valores del campo en la fuente
        /// (BasicTableHistogram no honra definition query ni selección); si necesitas
        /// clasificar un subconjunto, filtra antes con una definition query permanente.
        /// </summary>
        public static JObject SetGraduatedSymbology(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string campo = (string)parameters["campo"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(campo))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC) y 'campo' (campo numérico a graduar).");

            int numClases = LeerInt(parameters["num_clases"], 5);
            if (numClases < 2 || numClases > 32)
                throw new ArgumentException("'num_clases' debe estar entre 2 y 32.");
            string metodo = ((string)parameters["metodo"] ?? "natural_breaks").ToLowerInvariant();

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            IGeoFeatureLayer gfl = MapHandlers.FindLayer(map, capa) as IGeoFeatureLayer;
            if (gfl == null)
                throw new ArgumentException("Solo las capas de entidades admiten simbología graduada: " + capa);

            IFeatureClass fc = gfl.FeatureClass;
            if (fc == null)
                throw new ArgumentException("La capa no tiene fuente de datos accesible (¿rota?): " + capa);

            int idxCampo = fc.FindField(campo);
            if (idxCampo < 0)
                throw new ArgumentException("Campo no encontrado en la capa '" + capa + "': " + campo);
            IField f = fc.Fields.get_Field(idxCampo);
            if (!EsCampoNumerico(f.Type))
                throw new ArgumentException("El campo '" + campo + "' no es numérico (es "
                    + DataAccess.NombreTipoCampo(f.Type) + "); la simbología graduada necesita un campo numérico.");

            // Histograma de los valores del campo → clasificación en cortes.
            // Se tipa como la coclase (no ITableHistogram) para llamar a GetHistogram
            // sin arrastrar el tipo IHistogram, que vive en otro ensamblado no referenciado.
            BasicTableHistogramClass tableHist = new BasicTableHistogramClass();
            tableHist.Field = campo;
            tableHist.Table = (ITable)fc;
            object dataValues, dataFreq;
            tableHist.GetHistogram(out dataValues, out dataFreq);

            IClassifyGEN clasificador = Clasificador(metodo);
            int clasesDeseadas = numClases;
            try
            {
                clasificador.Classify(dataValues, dataFreq, ref clasesDeseadas);
            }
            catch (Exception ex)
            {
                throw new ArgumentException("No se pudo clasificar el campo '" + campo + "' con método '"
                    + metodo + "': " + ex.Message + ". ¿Tiene el campo suficientes valores distintos?");
            }
            double[] cortes = (double[])clasificador.ClassBreaks;
            if (cortes == null || cortes.Length < 2)
                throw new ArgumentException("El campo '" + campo + "' no tiene variación suficiente para "
                    + numClases + " clases (valores todos iguales o insuficientes).");
            int n = cortes.Length - 1; // el clasificador puede reducir el número real de clases

            // Rampa de color: por defecto amarillo claro → rojo oscuro (secuencial).
            IColor desde = LeerColor(parameters["color_desde"], 255, 255, 178);
            IColor hasta = LeerColor(parameters["color_hasta"], 189, 0, 38);
            IAlgorithmicColorRamp rampa = new AlgorithmicColorRampClass
            {
                Algorithm = esriColorRampAlgorithm.esriHSVAlgorithm,
                FromColor = desde,
                ToColor = hasta,
                Size = n
            };
            bool rampaOk;
            rampa.CreateRamp(out rampaOk);
            IEnumColors colores = rampa.Colors;
            colores.Reset();

            esriGeometryType shp = fc.ShapeType;
            double tam = LeerDouble(parameters["tamano"],
                shp == esriGeometryType.esriGeometryPolyline ? 2.0
                : shp == esriGeometryType.esriGeometryPolygon ? 0.4 : 6.0);

            IClassBreaksRenderer render = new ClassBreaksRendererClass
            {
                Field = campo,
                BreakCount = n,
                SortClassesAscending = true,
                MinimumBreak = cortes[0]
            };
            for (int i = 0; i < n; i++)
            {
                render.set_Break(i, cortes[i + 1]);
                IColor col = colores.Next() ?? hasta;
                render.set_Symbol(i, SimboloGraduado(shp, col, tam));
                render.set_Label(i, EtiquetaClase(cortes[i], cortes[i + 1]));
            }

            gfl.Renderer = (IFeatureRenderer)render;
            MapHandlers.NotificarCambioContenido(map, doc);

            var breaksJson = new JArray();
            for (int i = 0; i < cortes.Length; i++)
                breaksJson.Add(cortes[i]);

            return Protocol.Result(new JObject
            {
                ["capa"] = gfl.Name,
                ["campo"] = campo,
                ["metodo"] = metodo,
                ["num_clases"] = n,
                ["cortes"] = breaksJson
            });
        }

        private static bool EsCampoNumerico(esriFieldType tipo)
        {
            return tipo == esriFieldType.esriFieldTypeSmallInteger
                || tipo == esriFieldType.esriFieldTypeInteger
                || tipo == esriFieldType.esriFieldTypeSingle
                || tipo == esriFieldType.esriFieldTypeDouble;
        }

        /// <summary>Método de clasificación arcpy → coclase IClassifyGEN.</summary>
        private static IClassifyGEN Clasificador(string metodo)
        {
            switch (metodo)
            {
                case "natural_breaks":
                case "jenks":
                    return new NaturalBreaksClass();
                case "quantile":
                case "cuantil":
                    return new QuantileClass();
                case "equal_interval":
                case "intervalo_igual":
                    return new EqualIntervalClass();
                case "geometrical_interval":
                case "intervalo_geometrico":
                    return new GeometricalIntervalClass();
                case "standard_deviation":
                case "desviacion_estandar":
                    return new StandardDeviationClass();
                default:
                    throw new ArgumentException("Método de clasificación no soportado: '" + metodo
                        + "'. Usa natural_breaks, quantile, equal_interval, geometrical_interval o standard_deviation.");
            }
        }

        /// <summary>Símbolo relleno de un color según la geometría de la capa.</summary>
        private static ISymbol SimboloGraduado(esriGeometryType shp, IColor color, double tam)
        {
            switch (shp)
            {
                case esriGeometryType.esriGeometryPolygon:
                case esriGeometryType.esriGeometryMultiPatch:
                    ISimpleFillSymbol fill = new SimpleFillSymbolClass { Color = color };
                    ILineSymbol borde = new SimpleLineSymbolClass
                    {
                        Color = GrisBorde(),
                        Width = tam
                    };
                    fill.Outline = borde;
                    return (ISymbol)fill;
                case esriGeometryType.esriGeometryPolyline:
                    return (ISymbol)new SimpleLineSymbolClass { Color = color, Width = tam };
                default: // punto / multipunto
                    return (ISymbol)new SimpleMarkerSymbolClass { Color = color, Size = tam };
            }
        }

        private static IColor GrisBorde()
        {
            return new RgbColorClass { Red = 130, Green = 130, Blue = 130 };
        }

        private static IColor LeerColor(JToken t, int rDef, int gDef, int bDef)
        {
            int r = rDef, g = gDef, b = bDef;
            JArray arr = t as JArray;
            if (arr != null && arr.Count >= 3)
            {
                r = ClampByte((int)arr[0]);
                g = ClampByte((int)arr[1]);
                b = ClampByte((int)arr[2]);
            }
            return new RgbColorClass { Red = r, Green = g, Blue = b };
        }

        private static int ClampByte(int v)
        {
            return v < 0 ? 0 : (v > 255 ? 255 : v);
        }

        private static int LeerInt(JToken t, int porDefecto)
        {
            return t != null && t.Type != JTokenType.Null ? (int)t : porDefecto;
        }

        private static double LeerDouble(JToken t, double porDefecto)
        {
            return t != null && t.Type != JTokenType.Null ? (double)t : porDefecto;
        }

        private static string EtiquetaClase(double lo, double hi)
        {
            return FormatoNum(lo) + " - " + FormatoNum(hi);
        }

        private static string FormatoNum(double v)
        {
            // Enteros sin decimales; el resto con hasta 2 decimales, sin ceros de cola.
            if (Math.Abs(v - Math.Round(v)) < 1e-9)
                return ((long)Math.Round(v)).ToString(CultureInfo.InvariantCulture);
            return v.ToString("0.##", CultureInfo.InvariantCulture);
        }

        /// <summary>Primer IGeoFeatureLayer dentro de una capa (la propia, o la
        /// primera de un grupo, recursivo) — un .lyr puede envolver un grupo.</summary>
        private static IGeoFeatureLayer PrimerGeoFeatureLayer(ILayer lyr)
        {
            IGeoFeatureLayer gf = lyr as IGeoFeatureLayer;
            if (gf != null)
                return gf;
            ICompositeLayer comp = lyr as ICompositeLayer;
            if (comp == null)
                return null;
            for (int i = 0; i < comp.Count; i++)
            {
                IGeoFeatureLayer hijo = PrimerGeoFeatureLayer(comp.get_Layer(i));
                if (hijo != null)
                    return hijo;
            }
            return null;
        }

        private static IGroupLayer BuscarGrupoPadre(IMap map, ILayer objetivo)
        {
            for (int i = 0; i < map.LayerCount; i++)
            {
                if (ReferenceEquals(map.get_Layer(i), objetivo))
                    return null; // de primer nivel: la borra IMap.DeleteLayer
                IGroupLayer padre = BuscarPadreEn(map.get_Layer(i), objetivo);
                if (padre != null)
                    return padre;
            }
            return null;
        }

        private static IGroupLayer BuscarPadreEn(ILayer candidato, ILayer objetivo)
        {
            ICompositeLayer comp = candidato as ICompositeLayer;
            if (comp == null)
                return null;
            for (int i = 0; i < comp.Count; i++)
            {
                ILayer hijo = comp.get_Layer(i);
                if (ReferenceEquals(hijo, objetivo))
                    return candidato as IGroupLayer;
                IGroupLayer anidado = BuscarPadreEn(hijo, objetivo);
                if (anidado != null)
                    return anidado;
            }
            return null;
        }
    }
}
