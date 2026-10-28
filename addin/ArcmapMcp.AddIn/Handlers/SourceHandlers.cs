using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.DataSourcesRaster;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// list_broken_data_sources / repair_data_source — contrato JSON que esperan
    /// los schemas del servidor MCP. Equivalen al ListBrokenDataSources/
    /// findAndReplaceWorkspacePath de arcpy; aquí: ILayer2.Valid +
    /// IStandaloneTable.Valid para detectar, y un clon del IDatasetName
    /// reapuntado + IDataLayer.Connect para reparar.
    /// </summary>
    internal static class SourceHandlers
    {
        public static JObject ListBrokenDataSources(JObject parameters)
        {
            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            IMaps maps = doc.Maps;

            var rotos = new JArray();
            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);

                foreach (MapHandlers.CapaEnMapa c in MapHandlers.CapasConRuta(m))
                {
                    ILayer lyr = c.Capa;
                    if (lyr is IGroupLayer)
                        continue; // los grupos no tienen fuente propia
                    bool valido = true;
                    try
                    {
                        ILayer2 l2 = lyr as ILayer2;
                        valido = l2 == null || l2.Valid;
                    }
                    catch { valido = false; }
                    if (valido)
                        continue;

                    string workspace;
                    string fuente = DataAccess.RutaFuente(lyr, out workspace);
                    rotos.Add(new JObject
                    {
                        ["nombre"] = NombreSeguro(lyr),
                        // El listado recorre TODOS los data frames, así que sin decir
                        // en cuál está cada capa, repair_data_source no sabe dónde
                        // buscarla. 'ruta' la desambigua si el nombre se repite.
                        ["data_frame"] = NombreDeMapa(m),
                        ["ruta"] = c.Ruta,
                        ["fuente_rota"] = fuente,
                        ["workspace"] = workspace
                    });
                }

                // Tablas independientes del data frame, como ListBrokenDataSources.
                IStandaloneTableCollection tablas = m as IStandaloneTableCollection;
                if (tablas == null)
                    continue;
                for (int j = 0; j < tablas.StandaloneTableCount; j++)
                {
                    IStandaloneTable st = tablas.get_StandaloneTable(j);
                    bool valido = true;
                    try { valido = st.Valid; } catch { valido = false; }
                    if (valido)
                        continue;
                    var item = new JObject
                    {
                        ["nombre"] = "(tabla sin nombre)",
                        ["data_frame"] = NombreDeMapa(m),
                        ["ruta"] = null,
                        ["fuente_rota"] = null,
                        ["workspace"] = null
                    };
                    try { item["nombre"] = st.Name; } catch { }
                    try
                    {
                        IDatasetName dsn = (st as IDataLayer) != null
                            ? ((IDataLayer)st).DataSourceName as IDatasetName : null;
                        if (dsn != null)
                        {
                            string ws = dsn.WorkspaceName != null ? dsn.WorkspaceName.PathName : null;
                            item["workspace"] = ws;
                            item["fuente_rota"] = ws != null
                                ? System.IO.Path.Combine(ws, dsn.Name) : dsn.Name;
                        }
                    }
                    catch { }
                    rotos.Add(item);
                }
            }

            return Protocol.Result(new JObject
            {
                ["num"] = rotos.Count,
                ["rotos"] = rotos
            });
        }

        private static string NombreSeguro(ILayer lyr)
        {
            try { return lyr.Name; } catch { return "(sin nombre)"; }
        }

        private static string NombreDeMapa(IMap m)
        {
            try { return m.Name; } catch { return "(data frame sin nombre)"; }
        }

        /// <summary>
        /// Busca la capa en TODOS los data frames, o solo en el que diga
        /// 'data_frame'. list_broken_data_sources recorre el documento entero, así
        /// que limitar la reparación al data frame activo dejaba sin arreglo
        /// precisamente la mitad de lo que el listado enseñaba.
        /// </summary>
        private static ILayer BuscarCapa(IMxDocument doc, string capa, string dataFrame, out IMap mapa)
        {
            mapa = null;
            IMaps maps = doc.Maps;
            var candidatas = new List<string>();
            var todas = new List<string>();
            ILayer encontrada = null;
            bool dfVisto = false;
            var nombresDf = new List<string>();

            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);
                string nombreDf = NombreDeMapa(m);
                nombresDf.Add(nombreDf);
                if (!string.IsNullOrEmpty(dataFrame)
                    && !string.Equals(nombreDf, dataFrame, StringComparison.OrdinalIgnoreCase))
                    continue;
                dfVisto = true;

                List<MapHandlers.CapaEnMapa> capas = MapHandlers.CapasConRuta(m);
                foreach (MapHandlers.CapaEnMapa c in capas)
                    todas.Add(nombreDf + "/" + c.Ruta);
                foreach (MapHandlers.CapaEnMapa c in MapHandlers.Coincidencias(capas, capa))
                {
                    candidatas.Add(nombreDf + "/" + c.Ruta);
                    if (encontrada == null)
                    {
                        encontrada = c.Capa;
                        mapa = m;
                    }
                }
            }

            if (!string.IsNullOrEmpty(dataFrame) && !dfVisto)
                throw new ArgumentException("Data frame no encontrado: " + dataFrame
                    + ". Disponibles: " + string.Join(", ", nombresDf));
            if (candidatas.Count == 0)
                throw new ArgumentException("Capa no encontrada: " + capa
                    + ". Disponibles: " + string.Join(", ", todas));
            if (candidatas.Count > 1)
            {
                // Misma regla que MapHandlers.FindLayer en modo destructivo: la ruta
                // completa manda, pero solo ANCLADA con «/» delante (reapuntar no adivina).
                // Si dos data frames tienen cada uno una capa con esa ruta, sigue siendo error.
                ILayer exacta = null;
                IMap mapaExacta = null;
                int exactas = 0;
                for (int i = 0; i < maps.Count && MapHandlers.Anclada(capa); i++)
                {
                    IMap m = maps.get_Item(i);
                    if (!string.IsNullOrEmpty(dataFrame)
                        && !string.Equals(NombreDeMapa(m), dataFrame, StringComparison.OrdinalIgnoreCase))
                        continue;
                    List<MapHandlers.CapaEnMapa> capas = MapHandlers.CapasConRuta(m);
                    MapHandlers.CapaEnMapa c = MapHandlers.RutaExacta(MapHandlers.Coincidencias(capas, capa), capa);
                    if (c == null)
                        continue;
                    exactas++;
                    exacta = c.Capa;
                    mapaExacta = m;
                }
                if (exactas == 1)
                {
                    mapa = mapaExacta;
                    return exacta;
                }
                mapa = null;
                throw new ArgumentException("Hay " + candidatas.Count + " capas que casan con '" + capa
                    + "'. Indica 'data_frame' y la ruta anclada con «/» delante (p. ej. '/"
                    + candidatas[0].Substring(candidatas[0].IndexOf('/') + 1) + "'). Candidatas (data frame/ruta): "
                    + string.Join(", ", candidatas));
            }
            return encontrada;
        }

        public static JObject RepairDataSource(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string rutaAntigua = (string)parameters["ruta_antigua"];
            string rutaNueva = (string)parameters["ruta_nueva"];
            string dataFrame = (string)parameters["data_frame"];
            bool validar = Parametros.LeerBool(parameters["validar"], "validar", true);
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(rutaAntigua) || string.IsNullOrEmpty(rutaNueva))
                throw new ArgumentException("Indica 'capa', 'ruta_antigua' y 'ruta_nueva'.");

            IApplication appRep = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(appRep);
            IMap map;
            ILayer lyr = BuscarCapa(doc, capa, dataFrame, out map);
            IDataLayer dl = lyr as IDataLayer;
            if (dl == null)
                throw new ArgumentException("La capa no tiene fuente de datos reapuntable: " + capa);

            bool rotoAntes = EstaRota(lyr);

            IDatasetName dsn = dl.DataSourceName as IDatasetName;
            if (dsn == null || dsn.WorkspaceName == null)
                throw new InvalidOperationException("No se puede resolver la fuente actual de la capa: " + capa);
            string wsActual = dsn.WorkspaceName.PathName ?? "";
            // Sustitución del fragmento de ruta, insensible a mayúsculas — misma
            // semántica que el findAndReplaceWorkspacePath de arcpy.
            string wsNuevo = Regex.Replace(wsActual, Regex.Escape(rutaAntigua),
                rutaNueva.Replace("$", "$$"), RegexOptions.IgnoreCase);

            bool aplicado = true;
            string aviso = null;
            if (string.Equals(wsNuevo, wsActual, StringComparison.OrdinalIgnoreCase))
            {
                aplicado = false;
                aviso = "La 'ruta_antigua' no aparece en el workspace actual de la capa ("
                        + wsActual + "); no hay nada que sustituir.";
            }
            else
            {
                // Clonar el dataset name y reapuntarlo, sin tocar la capa hasta validar.
                IObjectCopy copia = new ObjectCopyClass();
                IDatasetName nuevoDsn = (IDatasetName)copia.Copy(dsn);
                nuevoDsn.WorkspaceName.PathName = wsNuevo;

                if (validar)
                {
                    try
                    {
                        ((IName)nuevoDsn).Open();
                    }
                    catch (Exception e)
                    {
                        // Respuesta limpia cuando la ruta nueva no contiene el
                        // dataset: aplicado:false + aviso, no excepción.
                        aplicado = false;
                        aviso = "Ruta nueva no valida (el dataset no existe alli); con validar=True "
                                + "no se aplico el cambio. Detalle: " + e.Message;
                    }
                }
                if (aplicado)
                {
                    // Connect(repairName) IGNORA el repair name mientras el
                    // DataSourceName actual siga siendo abrible (incluso tras
                    // Disconnect): para un reapunte incondicional como el del
                    // findAndReplaceWorkspacePath de arcpy hay que ESCRIBIR
                    // DataSourceName y reconectar.
                    IDataLayer2 dl2 = lyr as IDataLayer2;
                    bool conectado;
                    try
                    {
                        // Disconnect lanza E_FAIL si la capa ya está desconectada
                        // (rota); es indiferente: solo se busca soltar la conexión.
                        try { if (dl2 != null) dl2.Disconnect(); } catch { }
                        dl.DataSourceName = (IName)nuevoDsn;
                        conectado = dl.Connect((IName)nuevoDsn);
                    }
                    catch (Exception e)
                    {
                        if (validar)
                            throw;
                        conectado = false;
                        aviso = "Connect falló: " + e.Message;
                    }
                    // No fiarse del bool de Connect: verificar el reapunte
                    // re-leyendo la fuente (así se cazó este bug en la prueba).
                    try
                    {
                        IDatasetName dsnFinal = dl.DataSourceName as IDatasetName;
                        string wsFinal = dsnFinal != null && dsnFinal.WorkspaceName != null
                            ? dsnFinal.WorkspaceName.PathName : null;
                        if (conectado && !string.Equals(wsFinal, wsNuevo, StringComparison.OrdinalIgnoreCase))
                        {
                            conectado = false;
                            aviso = "El reapunte no se aplicó (workspace final: " + wsFinal + ").";
                        }
                    }
                    catch { }
                    if (!conectado && aviso == null)
                        aviso = "ArcObjects no pudo reapuntar la capa a " + wsNuevo + " (Connect devolvió false).";
                    aplicado = conectado;
                }
            }

            bool rotoDespues = EstaRota(lyr);
            // Como el resto de mutadoras: ContentsChanged avisa a los map surrounds.
            // Sin él, una leyenda se quedaba con el estado anterior a la reparación y
            // el export salía con la capa todavía rota.
            MapHandlers.NotificarCambioContenido(map, doc);

            var salida = new JObject
            {
                ["capa"] = capa,
                ["data_frame"] = NombreDeMapa(map),
                ["roto_antes"] = rotoAntes,
                ["roto_despues"] = rotoDespues,
                ["ruta_antigua"] = rutaAntigua,
                ["ruta_nueva"] = rutaNueva,
                ["validado"] = validar,
                ["aplicado"] = aplicado
            };
            if (aviso != null)
                salida["aviso"] = aviso;

            // Reparada en memoria no es reparada en disco: con rutas relativas y un
            // workspace demasiado lejos del .mxd, el guardado la vuelve a romper.
            if (aplicado)
            {
                try
                {
                    string mxd = ArcSession.MxdPath(appRep);
                    JObject riesgo = doc.RelativePaths ? RutasRelativas.Riesgo(mxd, wsNuevo) : null;
                    if (riesgo != null)
                    {
                        salida["riesgo_ruta_relativa"] = riesgo;
                        salida["aviso_ruta_relativa"] = "Reparada en memoria, pero se va a ROMPER al guardar: "
                            + RutasRelativas.Aviso(1, mxd);
                    }
                }
                catch (Exception ex) { Log.Warn("repair_data_source: no se pudo evaluar la ruta relativa: " + ex.Message); }
            }
            return Protocol.Result(salida);
        }

        /// <summary>
        /// replace_data_source: apunta una capa a OTRO dataset (otro fichero, otro tipo de
        /// workspace), conservando su simbología, etiquetas y definition query. Es el
        /// `replaceDataSource` de arcpy (op `replace_ds` de una serie de montes: 116 veces, del ráster
        /// regional a los del Anexo 1 de cada monte, o de Masas_V0 a Rodales_V1).
        /// repair_data_source solo cambia el principio de la ruta y solo en vivo; esta va por
        /// la costura Documento y vale también en edit_mxd_lote.
        /// </summary>
        public static JObject ReplaceDataSource(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string fuente = (string)parameters["fuente"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(fuente))
                throw new ArgumentException("Indica 'capa' y 'fuente' (ruta completa del dataset nuevo: .shp, .tif,"
                    + " Carpeta.gdb\\clase...).");
            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            // Reapuntar es destructivo: con homónimas exige la ruta anclada, como remove_layer.
            ILayer lyr = MapHandlers.FindLayer(map, capa, true);
            IDataLayer dl = lyr as IDataLayer;
            if (dl == null || lyr is IGroupLayer)
                throw new ArgumentException("'" + capa + "' no tiene fuente de datos que cambiar (¿es un grupo?).");

            // La fuente nueva se abre como capa de paso: valida que existe y da su IName.
            ILayer nueva;
            try { nueva = DataAccess.CrearCapaDesdeRuta(fuente); }
            catch (Exception ex) { throw new ArgumentException("No se puede abrir la fuente nueva '" + fuente + "': " + ex.Message); }
            IDataLayer dlNueva = nueva as IDataLayer;
            if (dlNueva == null || dlNueva.DataSourceName == null)
                throw new ArgumentException("La fuente nueva no da un dataset: " + fuente);

            bool esRaster = lyr is IRasterLayer;
            if (esRaster != (nueva is IRasterLayer))
                throw new ArgumentException("'" + capa + "' es " + (esRaster ? "un ráster" : "de entidades") + " y la fuente"
                    + " nueva es " + (esRaster ? "de entidades" : "un ráster") + ": no se puede cambiar el tipo de capa.");
            IFeatureLayer flVieja = lyr as IFeatureLayer, flNueva = nueva as IFeatureLayer;
            var avisos = new JArray();
            // Todo lo de entidades se comprueba ANTES de reapuntar, y también con la capa ROTA, que
            // es el caso típico de esta tool: sin FeatureClass vieja la geometría sale del símbolo
            // (revisión del 2026-10-25: una capa de polígonos rota reapuntada a puntos daba ok).
            if (flVieja != null && flNueva.FeatureClass != null)
                ComprobarEntidades(flVieja, flNueva, avisos);

            string antes = Fuente(lyr);
            bool rotoAntes = EstaRota(lyr);

            // Ráster clasificado: los cortes, etiquetas y colores se guardan y se restauran,
            // porque al reapuntar el renderer se recalcula y aparece «<VALOR>» de encabezado
            // y la primera clase en blanco (serie de montes).
            IRasterLayer rl = lyr as IRasterLayer;
            IRasterClassifyColorRampRenderer clas = rl != null ? rl.Renderer as IRasterClassifyColorRampRenderer : null;
            double[] cortes = null; string[] etiquetas = null; ISymbol[] simbolos = null; string encabezado = null;
            if (clas != null)
            {
                int n = clas.ClassCount;
                cortes = new double[n + 1]; etiquetas = new string[n]; simbolos = new ISymbol[n];
                for (int i = 0; i <= n; i++) cortes[i] = clas.get_Break(i);
                for (int i = 0; i < n; i++) { etiquetas[i] = clas.get_Label(i); simbolos[i] = clas.get_Symbol(i); }
                ILegendInfo li = clas as ILegendInfo;
                try { if (li != null && li.LegendGroupCount > 0) encabezado = li.get_LegendGroup(0).Heading; } catch { }
            }

            IDataLayer2 dl2 = lyr as IDataLayer2;
            try { if (dl2 != null) dl2.Disconnect(); } catch { }
            if (flVieja != null && flNueva.FeatureClass != null)
                flVieja.FeatureClass = flNueva.FeatureClass;   // conserva renderer, etiquetas y DQ
            else
            {
                dl.DataSourceName = dlNueva.DataSourceName;
                if (!dl.Connect(dlNueva.DataSourceName))
                    throw new InvalidOperationException("ArcObjects no pudo conectar la capa a " + fuente + ".");
            }

            if (rl != null && rl.Renderer != null)
            {
                IRasterRenderer rr = rl.Renderer;
                rr.Raster = rl.Raster;
                rr.Update();
                if (clas != null)
                {
                    IRasterClassifyColorRampRenderer c2 = rr as IRasterClassifyColorRampRenderer;
                    if (c2 != null)
                    {
                        if (c2.ClassCount != etiquetas.Length)
                            c2.ClassCount = etiquetas.Length;
                        for (int i = 0; i <= etiquetas.Length; i++) c2.set_Break(i, cortes[i]);
                        for (int i = 0; i < etiquetas.Length; i++) { c2.set_Label(i, etiquetas[i]); c2.set_Symbol(i, simbolos[i]); }
                        rr.Update();
                        // El encabezado DESPUÉS de Update: el renderer lo regenera al actualizarse.
                        ILegendInfo li2 = c2 as ILegendInfo;
                        try { if (li2 != null && li2.LegendGroupCount > 0 && encabezado != null) li2.get_LegendGroup(0).Heading = encabezado; } catch { }
                        // El mínimo del ráster nuevo puede ser otro: el primer corte sigue siendo el viejo.
                        avisos.Add("Ráster clasificado: se han conservado los " + etiquetas.Length + " cortes, etiquetas y colores."
                            + " Si el rango del dato nuevo es otro, revisa los cortes (set_raster_symbology).");
                    }
                }
                rl.Renderer = rr;
            }

            // Campos que la capa usa y el dataset nuevo no tiene.
            if (flVieja != null && flVieja.FeatureClass != null)
            {
                IFeatureLayerDefinition dq = lyr as IFeatureLayerDefinition;
                string q = dq != null ? dq.DefinitionExpression : null;
                if (!string.IsNullOrEmpty(q))
                    foreach (Match m in Regex.Matches(q, "\"([^\"]+)\""))
                        if (flVieja.FeatureClass.FindField(m.Groups[1].Value) < 0)
                            avisos.Add("La definition query usa el campo '" + m.Groups[1].Value + "', que el dataset nuevo no tiene.");
            }

            bool rotoDespues = EstaRota(lyr);
            MapHandlers.NotificarCambioContenido(map, doc);
            var r = new JObject
            {
                ["capa"] = capa,
                ["fuente_anterior"] = antes,
                ["fuente_nueva"] = Fuente(lyr),
                ["roto_antes"] = rotoAntes,
                ["roto_despues"] = rotoDespues
            };
            if (rotoDespues)
                avisos.Add("La capa sigue rota tras reapuntar: revisa la ruta.");
            if (avisos.Count > 0)
                r["avisos"] = avisos;
            return Protocol.Result(r);
        }

        /// <summary>
        /// Lo que la capa vieja necesita y el dataset nuevo tiene que dar: la geometría
        /// (error), los campos del renderer (error: sin ellos no se dibuja ninguna categoría)
        /// y los de las etiquetas (aviso: la capa se dibuja, las etiquetas no).
        /// </summary>
        private static void ComprobarEntidades(IFeatureLayer flVieja, IFeatureLayer flNueva, JArray avisos)
        {
            IFeatureClass fcNueva = flNueva.FeatureClass;
            ESRI.ArcGIS.Geometry.esriGeometryType nueva = GeometriaBase(fcNueva.ShapeType);
            IGeoFeatureLayer gfl = flVieja as IGeoFeatureLayer;
            ESRI.ArcGIS.Geometry.esriGeometryType? vieja = null;
            string deDonde = null;
            if (flVieja.FeatureClass != null)
                vieja = GeometriaBase(flVieja.FeatureClass.ShapeType);
            else if (gfl != null)
            {
                vieja = GeometriaDelSimbolo(SimboloDe(gfl.Renderer));
                deDonde = " (deducida del símbolo: la capa está rota)";
            }
            if (vieja != null && vieja.Value != nueva)
                throw new ArgumentException("La geometría no coincide (" + vieja.Value + (deDonde ?? "") + " → "
                    + fcNueva.ShapeType + "): la simbología no valdría.");

            if (gfl == null)
                return;
            var faltan = new List<string>();
            // Un `tabla.CAMPO` es de una tabla unida, que la clase nueva no trae: no se juzga.
            foreach (string campo in CamposDelRenderer(gfl.Renderer))
                if (campo.IndexOf('.') < 0 && fcNueva.FindField(campo) < 0 && !faltan.Contains(campo))
                    faltan.Add(campo);
            if (faltan.Count > 0)
                throw new ArgumentException("La simbología usa " + (faltan.Count == 1 ? "el campo '" : "los campos '")
                    + string.Join("', '", faltan) + "', que el dataset nuevo no tiene: no se dibujaría ninguna"
                    + " categoría. Simboliza antes por un campo que exista en los dos, o reapunta y simboliza.");

            IAnnotateLayerPropertiesCollection col = gfl.AnnotationProperties;
            IGeoFeatureLayer gflNueva = flNueva as IGeoFeatureLayer;
            if (col == null || gflNueva == null)
                return;
            for (int k = 0; k < col.Count; k++)
            {
                ILabelEngineLayerProperties lep = LabelHandlers.Clase(col, k) as ILabelEngineLayerProperties;
                if (lep == null || string.IsNullOrEmpty(lep.Expression))
                    continue;
                try { LabelHandlers.ComprobarCampos(gflNueva, lep.Expression); }
                catch (ArgumentException ex)
                {
                    avisos.Add("Etiquetas («" + ((IAnnotateLayerProperties)lep).Class + "»): " + ex.Message);
                }
            }
        }

        private static ISymbol SimboloDe(IFeatureRenderer renderer)
        {
            ISimpleRenderer simple = renderer as ISimpleRenderer;
            if (simple != null)
                return simple.Symbol;
            IUniqueValueRenderer uvr = renderer as IUniqueValueRenderer;
            if (uvr != null)
                return uvr.DefaultSymbol ?? (uvr.ValueCount > 0 ? uvr.get_Symbol(uvr.get_Value(0)) : null);
            IClassBreaksRenderer cbr = renderer as IClassBreaksRenderer;
            if (cbr != null && cbr.BreakCount > 0)
                return cbr.get_Symbol(0);
            return null;
        }

        /// <summary>Relleno = polígono, línea = polilínea, marcador = punto. Null si el
        /// símbolo no lo dice (un renderer de gráficos, de densidad...): entonces no se juzga.</summary>
        private static ESRI.ArcGIS.Geometry.esriGeometryType? GeometriaDelSimbolo(ISymbol s)
        {
            if (s is IFillSymbol) return ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPolygon;
            if (s is ILineSymbol) return ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPolyline;
            if (s is IMarkerSymbol) return ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPoint;
            return null;
        }

        private static List<string> CamposDelRenderer(IFeatureRenderer renderer)
        {
            var campos = new List<string>();
            IUniqueValueRenderer uvr = renderer as IUniqueValueRenderer;
            if (uvr != null)
                for (int i = 0; i < uvr.FieldCount; i++)
                    if (!string.IsNullOrEmpty(uvr.get_Field(i)))
                        campos.Add(uvr.get_Field(i));
            IClassBreaksRenderer cbr = renderer as IClassBreaksRenderer;
            if (cbr != null && !string.IsNullOrEmpty(cbr.Field))
                campos.Add(cbr.Field);
            IDataNormalization norm = renderer as IDataNormalization;
            if (norm != null && norm.NormalizationType == esriDataNormalization.esriNormalizeByField
                && !string.IsNullOrEmpty(norm.NormalizationField))
                campos.Add(norm.NormalizationField);
            return campos;
        }

        private static ESRI.ArcGIS.Geometry.esriGeometryType GeometriaBase(ESRI.ArcGIS.Geometry.esriGeometryType t)
        {
            return t == ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryMultipoint
                ? ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPoint : t;
        }

        private static string Fuente(ILayer lyr)
        {
            try
            {
                IDatasetName dsn = ((IDataLayer)lyr).DataSourceName as IDatasetName;
                if (dsn == null) return null;
                string ws = dsn.WorkspaceName != null ? dsn.WorkspaceName.PathName : "";
                return System.IO.Path.Combine(ws ?? "", dsn.Name ?? "");
            }
            catch { return null; }
        }

        private static bool EstaRota(ILayer lyr)
        {
            try
            {
                ILayer2 l2 = lyr as ILayer2;
                return l2 != null && !l2.Valid;
            }
            catch
            {
                return true;
            }
        }
    }
}
