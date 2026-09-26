using System;
using System.Collections.Generic;
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
                throw new ArgumentException("Indica 'fuente' (ruta a .shp, feature class de .gdb, ráster o .lyr).");
            string posicion = ((string)parameters["posicion"] ?? "TOP").ToUpperInvariant();
            if (posicion != "TOP" && posicion != "BOTTOM" && posicion != "AUTO_ARRANGE")
                throw new ArgumentException("'posicion' debe ser TOP, BOTTOM o AUTO_ARRANGE.");
            string grupo = (string)parameters["grupo"];
            string nombre = (string)parameters["nombre"];
            bool enLeyenda = Parametros.LeerBool(parameters["en_leyenda"], "en_leyenda", true);
            bool? visible = Parametros.Dado(parameters["visible"])
                ? Parametros.LeerBool(parameters["visible"], "visible", true) : (bool?)null;
            bool esServicio = Servicios.EsUrl(fuente.Trim());
            List<string> subcapas = LeerSubcapas(parameters["subcapas"]);
            if (subcapas != null && !esServicio)
                throw new ArgumentException("'subcapas' es para servicios WMS (fuente = URL http/https).");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            JObject servicio = null;
            ILayer nueva = esServicio ? Servicios.CrearWms(fuente.Trim(), subcapas, out servicio) : CrearCapa(fuente);

            // El nombre de la TOC es el que sale en la leyenda del plano, así que
            // llega hasta aquí: sin esto la capa se queda con el nombre del
            // fichero ("Vis_plataforma_oeste_sin_pantalla.tif") y hay que
            // renombrarla a mano o rodear el puente por MakeRasterLayer.
            if (!string.IsNullOrEmpty(nombre))
                nueva.Name = nombre;
            // Antes de insertar: un WMS que entra apagado no llega a pedir teselas.
            if (visible != null)
                nueva.Visible = visible.Value;

            int leyendasAparte = Insertar(map, doc, nueva, grupo, posicion, enLeyenda);
            ICompositeLayer comp = nueva as ICompositeLayer;
            var r = new JObject
            {
                ["capa"] = nueva.Name,
                ["fuente"] = fuente,
                ["posicion"] = posicion,
                ["grupo"] = grupo,
                ["visible"] = nueva.Visible,
                ["es_grupo"] = comp != null && !esServicio,
                ["capas_dentro"] = comp != null && !esServicio ? (JToken)comp.Count : null,
                ["en_leyenda"] = enLeyenda,
                ["leyendas_sin_tocar"] = enLeyenda ? null : (JToken)leyendasAparte
            };
            if (servicio != null)
                r["servicio"] = servicio;
            return Protocol.Result(r);
        }

        private static List<string> LeerSubcapas(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            JArray a = t as JArray;
            if (a == null || a.Count == 0)
                throw new ArgumentException("'subcapas' debe ser una lista de nombres (o rutas 'Grupo/Subcapa') del"
                    + " servicio. Para encenderlas todas, no la pases.");
            var l = new List<string>();
            foreach (JToken x in a)
            {
                if (x.Type != JTokenType.String || string.IsNullOrEmpty(((string)x).Trim()))
                    throw new ArgumentException("'subcapas' solo admite textos. Recibido: " + x.ToString());
                l.Add(((string)x).Trim());
            }
            return l;
        }

        /// <summary>
        /// add_group — crea una capa de grupo vacía. arcpy no sabe (en el bloque 03 de
        /// ID2018 hubo que guardar un grupo vacío existente como .lyr y usarlo de
        /// plantilla); en ArcObjects es un GroupLayer nuevo.
        /// </summary>
        public static JObject AddGroup(JObject parameters)
        {
            string nombre = (string)parameters["nombre"];
            if (string.IsNullOrEmpty(nombre))
                throw new ArgumentException("Indica 'nombre' para el grupo nuevo.");
            string posicion = ((string)parameters["posicion"] ?? "TOP").ToUpperInvariant();
            if (posicion != "TOP" && posicion != "BOTTOM")
                throw new ArgumentException("'posicion' debe ser TOP o BOTTOM.");
            string padre = (string)parameters["grupo"];
            bool visible = Parametros.LeerBool(parameters["visible"], "visible", true);
            bool enLeyenda = Parametros.LeerBool(parameters["en_leyenda"], "en_leyenda", true);

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);

            // Un homónimo no impide crearlo, pero a partir de ahí 'grupo' y el resto de
            // tools pedirán la ruta: se avisa para que no sea una sorpresa.
            int homonimos = MapHandlers.Coincidencias(MapHandlers.CapasConRuta(map), nombre).Count;

            ILayer grp = new GroupLayerClass();
            grp.Name = nombre;
            grp.Visible = visible;
            int leyendasAparte = Insertar(map, doc, grp, padre, posicion, enLeyenda);

            var r = new JObject
            {
                ["grupo"] = nombre,
                ["dentro_de"] = padre,
                ["posicion"] = posicion,
                ["visible"] = visible,
                ["en_leyenda"] = enLeyenda,
                ["leyendas_sin_tocar"] = enLeyenda ? null : (JToken)leyendasAparte
            };
            if (homonimos > 0)
                r["aviso"] = "Ya había " + homonimos + " capa(s) llamada(s) '" + nombre
                    + "': para referirte a este grupo usa su ruta (list_layers la muestra).";
            return Protocol.Result(r);
        }

        /// <summary>
        /// Inserta la capa en el mapa o en un grupo. Con `enLeyenda` = false apaga el
        /// AutoAdd de las leyendas de ESE mapa mientras entra y lo deja como estaba:
        /// con AutoAdd (lo normal) cada capa añadida entra también en la leyenda del
        /// plano, y en uno que ya va justo la desborda. Devuelve cuántas leyendas se
        /// han dejado sin tocar.
        /// </summary>
        private static int Insertar(IMap map, IMxDocument doc, ILayer nueva, string grupo,
                                    string posicion, bool enLeyenda)
        {
            IMapLayers mapLayers = (IMapLayers)map;
            IGroupLayer grp = null;
            if (!string.IsNullOrEmpty(grupo))
            {
                grp = MapHandlers.FindLayer(map, grupo) as IGroupLayer;
                if (grp == null)
                    throw new ArgumentException("La capa indicada en 'grupo' no es una capa de grupo: " + grupo);
            }

            var leyendas = enLeyenda ? new System.Collections.Generic.List<ILegend>()
                                     : LayoutHandlers.LeyendasDelMapa(doc, map);
            var autoAdd = new System.Collections.Generic.List<bool>();
            foreach (ILegend lg in leyendas)
            {
                autoAdd.Add(lg.AutoAdd);
                lg.AutoAdd = false;
            }
            try
            {
                if (grp != null)
                {
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
                // Dentro del try: la leyenda reacciona al aviso de cambio de contenido,
                // y tiene que llegarle con el AutoAdd aún apagado.
                MapHandlers.NotificarCambioContenido(map, doc);
            }
            finally
            {
                for (int i = 0; i < leyendas.Count; i++)
                    leyendas[i].AutoAdd = autoAdd[i];
            }
            return leyendas.Count;
        }

        /// <summary>
        /// Crea la capa desde la ruta. Un `.lyr` se abre como layer file: trae
        /// dentro el nombre, la simbología, la transparencia y —si es de grupo—
        /// el árbol entero, que es lo único que permite reproducir de una vez la
        /// estructura de grupos de un proyecto de QGIS. El resto de fuentes van
        /// por la factory de siempre.
        ///
        /// `lf.Close()` cierra el fichero pero NO invalida la capa ya obtenida:
        /// es el mismo patrón que usa el Add Data de ArcMap, y cerrarlo evita
        /// dejar el `.lyr` bloqueado en disco mientras dure la sesión.
        /// </summary>
        private static ILayer CrearCapa(string fuente)
        {
            if (!".lyr".Equals(System.IO.Path.GetExtension(fuente), StringComparison.OrdinalIgnoreCase))
                return DataAccess.CrearCapaDesdeRuta(fuente);

            if (!File.Exists(fuente))
                throw new ArgumentException("No existe el archivo .lyr: " + fuente);
            ILayerFile lf = new LayerFileClass();
            ILayer capa;
            try
            {
                lf.Open(fuente);
                capa = lf.Layer;
            }
            finally
            {
                // En finally, no tras el Open: si Open lanzaba (fichero de otra
                // versión, corrupto), el .lyr se quedaba bloqueado en disco el resto
                // de la sesión de ArcMap.
                try { lf.Close(); } catch { }
            }
            if (capa == null)
                throw new ArgumentException("El .lyr no contiene ninguna capa: " + fuente);
            return capa;
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
            ILayer objetivo = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer destino = objetivo as IGeoFeatureLayer;
            IRasterLayer destinoRaster = objetivo as IRasterLayer;
            if (destino == null && destinoRaster == null)
                throw new ArgumentException("Solo capas de entidades o ráster admiten simbología desde .lyr: " + capa);

            JObject detalle;
            ILayerFile lf = new LayerFileClass();
            try
            {
                // El Open va DENTRO del try: si lanza, el finally cierra igualmente
                // y el .lyr no se queda bloqueado.
                lf.Open(lyrFile);
                detalle = destino != null
                    ? AplicarAEntidades(destino, lf.Layer, lyrFile)
                    : AplicarARaster(destinoRaster, lf.Layer, lyrFile);
            }
            finally
            {
                try { lf.Close(); } catch { }
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            detalle["capa"] = capa;
            detalle["lyr_origen"] = lyrFile;
            return Protocol.Result(detalle);
        }

        private static JObject AplicarAEntidades(IGeoFeatureLayer destino, ILayer raizLyr, string lyrFile)
        {
            IGeoFeatureLayer origen = PrimerGeoFeatureLayer(raizLyr);
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
            return new JObject { ["tipo"] = "entidades" };
        }

        /// <summary>
        /// Simbología ráster desde un .lyr. Es la única vía para un renderer que
        /// las tools no saben construir —valores únicos, colormap, RGB compuesto—:
        /// `set_raster_symbology` solo clasifica o estira, y sobre un ráster
        /// categórico (una máscara 0/1/2, una reclasificación, un `paletted` traído
        /// de QGIS) la clasificación falla porque no hay histograma que cortar.
        ///
        /// El renderer se clona y se REENCAJA en el ráster de destino: un
        /// IRasterRenderer lleva dentro el ráster sobre el que se construyó, y sin
        /// `Raster` + `Update()` el clon pintaría contra el dato del .lyr.
        ///
        /// La transparencia viaja también, y a propósito: en un ráster de
        /// visibilidad o de afección la opacidad no es decoración, es lo que deja
        /// ver la ortofoto debajo, y se pierde en cuanto la capa se recrea.
        /// </summary>
        private static JObject AplicarARaster(IRasterLayer destino, ILayer raizLyr, string lyrFile)
        {
            IRasterLayer origen = PrimerRasterLayer(raizLyr);
            if (origen == null)
                throw new ArgumentException("El .lyr no contiene una capa ráster: " + lyrFile);
            if (origen.Renderer == null)
                throw new ArgumentException("La capa ráster del .lyr no tiene renderer: " + lyrFile);
            if (destino.Raster == null)
                throw new ArgumentException("La capa de destino no tiene ráster accesible (¿fuente rota?): " + destino.Name);

            IObjectCopy copia = new ObjectCopyClass();
            IRasterRenderer renderer = (IRasterRenderer)copia.Copy(origen.Renderer);
            renderer.Raster = destino.Raster;
            renderer.Update();
            destino.Renderer = renderer;

            var detalle = new JObject
            {
                ["tipo"] = "raster",
                ["renderer"] = NombreRenderer(renderer)
            };

            ILayerEffects efOrigen = origen as ILayerEffects;
            ILayerEffects efDestino = destino as ILayerEffects;
            if (efOrigen != null && efDestino != null && efDestino.SupportsTransparency)
            {
                efDestino.Transparency = efOrigen.Transparency;
                detalle["transparencia"] = efOrigen.Transparency;
            }
            return detalle;
        }

        private static string NombreRenderer(IRasterRenderer renderer)
        {
            if (renderer is IRasterUniqueValueRenderer) return "valores únicos";
            if (renderer is IRasterClassifyColorRampRenderer) return "clasificado";
            if (renderer is IRasterStretchColorRampRenderer) return "estirado";
            if (renderer is IRasterRGBRenderer) return "RGB";
            return renderer.GetType().Name;
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

            int numClases = Parametros.LeerEntero(parameters["num_clases"], "num_clases", 5, 2, 32);
            string metodo = ((string)parameters["metodo"] ?? "natural_breaks").ToLowerInvariant();

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            IGeoFeatureLayer gfl = MapHandlers.FindLayer(map, capa) as IGeoFeatureLayer;
            if (gfl == null)
                throw new ArgumentException("Solo las capas de entidades admiten simbología graduada: " + capa);

            IFeatureClass fc = gfl.FeatureClass;
            if (fc == null)
                throw new ArgumentException("La capa no tiene fuente de datos accesible (¿rota?): " + capa);

            // El campo se busca en la DISPLAY TABLE, no en la feature class: los
            // campos de un join existen en la capa (y list_fields los ofrece) pero no
            // en la clase base, así que graduar por uno daba "Campo no encontrado".
            // Sin joins, la display table ES la feature class y no cambia nada.
            IFields camposCapa = QueryHandlers.CamposDeCapa(gfl);
            int idxCampo = camposCapa.FindField(campo);
            if (idxCampo < 0)
                throw new ArgumentException("Campo no encontrado en la capa '" + capa + "': " + campo
                    + ". Disponibles: " + NombresDeCampos(camposCapa));
            IField f = camposCapa.get_Field(idxCampo);
            if (!EsCampoNumerico(f.Type))
                throw new ArgumentException("El campo '" + campo + "' no es numérico (es "
                    + DataAccess.NombreTipoCampo(f.Type) + "); la simbología graduada necesita un campo numérico.");

            // Histograma de los valores del campo → clasificación en cortes.
            // Se tipa como la coclase (no ITableHistogram) para llamar a GetHistogram
            // sin arrastrar el tipo IHistogram, que vive en otro ensamblado no referenciado.
            BasicTableHistogramClass tableHist = new BasicTableHistogramClass();
            tableHist.Field = campo;
            tableHist.Table = QueryHandlers.TablaDeCapa(gfl);
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

            // Rampa de color: por defecto amarillo claro → rojo oscuro (secuencial),
            // interpolada en CIE Lab y NO en HSV. Medido el 2026-09-04 sobre el
            // ráster (ver RasterSymbologyHandlers.LeerAlgoritmo): HSV interpola el
            // TONO dando la vuelta a la rueda de color y devuelve un arcoíris del
            // que no se lee ningún orden — justo lo contrario de lo que pide una
            // simbología graduada. Se construye con el MISMO helper que el ráster,
            // que además lanza si CreateRamp falla en vez de seguir con una leyenda
            // entera del mismo color.
            IColor desde = Parametros.LeerColor(parameters["color_desde"], "color_desde", 255, 255, 178);
            IColor hasta = Parametros.LeerColor(parameters["color_hasta"], "color_hasta", 189, 0, 38);
            IColor[] paleta = RasterSymbologyHandlers.ConstruirRampa(desde, hasta, n,
                RasterSymbologyHandlers.LeerAlgoritmo(parameters["algoritmo"]));

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
                render.set_Symbol(i, SimboloGraduado(shp, paleta[i], tam));
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

        private static string NombresDeCampos(IFields campos)
        {
            var nombres = new System.Collections.Generic.List<string>();
            for (int i = 0; i < campos.FieldCount; i++)
                nombres.Add(campos.get_Field(i).Name);
            return string.Join(", ", nombres);
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

        private static IRasterLayer PrimerRasterLayer(ILayer lyr)
        {
            IRasterLayer rl = lyr as IRasterLayer;
            if (rl != null)
                return rl;
            ICompositeLayer comp = lyr as ICompositeLayer;
            if (comp == null)
                return null;
            for (int i = 0; i < comp.Count; i++)
            {
                IRasterLayer hijo = PrimerRasterLayer(comp.get_Layer(i));
                if (hijo != null)
                    return hijo;
            }
            return null;
        }

        internal static IGroupLayer BuscarGrupoPadre(IMap map, ILayer objetivo)
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
