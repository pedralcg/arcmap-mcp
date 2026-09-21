using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// select_by_attribute / clear_selection / get_unique_values / count_features /
    /// list_fields / get_layer_info / get_layer_features — contrato JSON que
    /// esperan los schemas del servidor MCP. Las consultas honran definition query y selección
    /// como arcpy.da.SearchCursor sobre una capa de la TOC: si hay selección se
    /// recorre el ISelectionSet; si no, la display table (joins + def. query).
    ///
    /// TODO cursor que se abra aquí se cierra en un finally con ReleaseComObject:
    /// un ICursor vivo deja lock sobre la fuente, y los caminos que se salían a
    /// mitad de tabla —el límite de `get_layer_features`, el "campo no encontrado"
    /// que se lanza con el cursor ya abierto— lo dejaban abierto para el resto de
    /// la sesión de ArcMap.
    /// </summary>
    internal static class QueryHandlers
    {
        /// <summary>Tope de filas de `get_layer_features`. La respuesta viaja por
        /// JSON hasta el cliente MCP: unos miles de filas ya no son una consulta,
        /// son un volcado.</summary>
        private const int MaxLimiteFilas = 5000;

        /// <summary>Tope por defecto de `get_unique_values`. El recorrido va en el
        /// hilo STA, o sea que congela la GUI de ArcMap mientras dura: sin tope, un
        /// campo de texto libre sobre una capa de un millón de filas no termina.</summary>
        private const int MaxValoresPorDefecto = 1000;

        private static ILayer CapaRequerida(JObject parameters, IMap map)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");
            return MapHandlers.FindLayer(map, capa);
        }

        /// <summary>Tabla consultable de la capa: la DISPLAY TABLE, que es la que
        /// trae los campos de los joins. Se comprueba que exista en vez de dejar
        /// que COM lance un E_FAIL mudo (ráster sin tabla, fuente rota).</summary>
        internal static ITable TablaDeCapa(ILayer lyr)
        {
            IDisplayTable dt = lyr as IDisplayTable;
            ITable tabla = null;
            if (dt != null)
            {
                try { tabla = dt.DisplayTable; }
                catch { tabla = null; }
            }
            if (tabla == null)
                throw new ArgumentException("La capa no tiene tabla de atributos consultable"
                    + " (¿ráster, o fuente rota?): " + MapHandlers.NombreSeguro(lyr));
            return tabla;
        }

        /// <summary>Cursor de lectura sobre la capa: selección si existe, display
        /// table si no. El where se combina con la definition query explícitamente
        /// para no depender de si SearchDisplayTable la aplica por su cuenta.
        /// Quien lo abra DEBE soltarlo (DataAccess.SoltarCom) en un finally.</summary>
        internal static ICursor CursorSobreCapa(ILayer lyr, string where, string subcampos)
        {
            IFeatureSelection fsel = lyr as IFeatureSelection;
            if (fsel != null && fsel.SelectionSet != null && fsel.SelectionSet.Count > 0)
            {
                // Con un JOIN activo los nombres de campo van cualificados y solo
                // existen en la selección de la DISPLAY table: la de la clase base no
                // los conoce, así que un where o unos SubFields sobre un campo de la
                // tabla unida fallaban. Sin join se sigue usando la selección de
                // siempre, que para ese caso es la misma y está probada.
                ISelectionSet conjunto = fsel.SelectionSet;
                if (TieneJoin(lyr))
                {
                    IDisplayTable dtSel = lyr as IDisplayTable;
                    ISelectionSet display = null;
                    if (dtSel != null)
                    {
                        try { display = dtSel.DisplaySelectionSet; }
                        catch { display = null; }
                    }
                    if (display != null)
                        conjunto = display;
                }

                IQueryFilter filtroSel = null;
                if (!string.IsNullOrEmpty(where) || !string.IsNullOrEmpty(subcampos))
                {
                    filtroSel = new QueryFilterClass();
                    if (!string.IsNullOrEmpty(where)) filtroSel.WhereClause = where;
                    if (!string.IsNullOrEmpty(subcampos)) filtroSel.SubFields = subcampos;
                }
                ICursor cursorSel;
                conjunto.Search(filtroSel, true, out cursorSel);
                return cursorSel;
            }

            return CursorDisplayTable(lyr, where, subcampos);
        }

        /// <summary>Cursor sobre la display table (joins + definition query) SIN
        /// mirar la selección: es lo que necesita quien describe la capa entera,
        /// como la simbología, donde honrar la selección daría una leyenda que solo
        /// vale para lo que hubiera seleccionado en ese momento.
        /// Cursor reciclado: aquí solo se leen atributos.</summary>
        internal static ICursor CursorDisplayTable(ILayer lyr, string where, string subcampos)
        {
            // Valida que la capa tiene tabla consultable ANTES de pedir el cursor:
            // un ráster sin tabla o una fuente rota devolvían un E_FAIL crudo.
            TablaDeCapa(lyr);
            IDisplayTable dt = (IDisplayTable)lyr;

            IFeatureLayerDefinition def = lyr as IFeatureLayerDefinition;
            string combinado = DataAccess.CombinarWhere(
                def != null ? def.DefinitionExpression : null, where);
            IQueryFilter filtro = null;
            if (!string.IsNullOrEmpty(combinado) || !string.IsNullOrEmpty(subcampos))
            {
                filtro = new QueryFilterClass();
                if (!string.IsNullOrEmpty(combinado)) filtro.WhereClause = combinado;
                if (!string.IsNullOrEmpty(subcampos)) filtro.SubFields = subcampos;
            }
            return dt.SearchDisplayTable(filtro, true);
        }

        /// <summary>¿La capa tiene una tabla unida por join? Es lo que separa el caso
        /// en que la display table y la clase base son la misma cosa del caso en que
        /// no lo son.</summary>
        private static bool TieneJoin(ILayer lyr)
        {
            IDisplayRelationshipClass drc = lyr as IDisplayRelationshipClass;
            if (drc == null)
                return false;
            try { return drc.RelationshipClass != null; }
            catch { return false; }
        }

        /// <summary>Campos de la capa con joins incluidos (display table), como
        /// arcpy.ListFields sobre la capa.</summary>
        internal static IFields CamposDeCapa(ILayer lyr)
        {
            return TablaDeCapa(lyr).Fields;
        }

        /// <summary>Conteo con la semántica del GetCount de arcpy: selección si la hay;
        /// si no, total honrando la definition query (RowCount es O(1) en gdb).</summary>
        private static int ContarFeatures(ILayer lyr, string where)
        {
            IFeatureSelection fsel = lyr as IFeatureSelection;
            bool haySeleccion = fsel != null && fsel.SelectionSet != null && fsel.SelectionSet.Count > 0;

            if (haySeleccion)
            {
                if (string.IsNullOrEmpty(where))
                    return fsel.SelectionSet.Count;
                int n = 0;
                ICursor cur = CursorSobreCapa(lyr, where, null);
                try
                {
                    while (cur.NextRow() != null)
                        n++;
                }
                finally
                {
                    DataAccess.SoltarCom(cur);
                }
                return n;
            }

            ITable tabla = TablaDeCapa(lyr);
            IFeatureLayerDefinition def = lyr as IFeatureLayerDefinition;
            string combinado = DataAccess.CombinarWhere(
                def != null ? def.DefinitionExpression : null, where);
            IQueryFilter filtro = null;
            if (!string.IsNullOrEmpty(combinado))
                filtro = new QueryFilterClass { WhereClause = combinado };
            return tabla.RowCount(filtro);
        }

        private static void RefrescarSeleccion(IMxDocument doc, IMap map)
        {
            IActiveView av = (IActiveView)map;
            av.PartialRefresh(esriViewDrawPhase.esriViewGeoSelection, null, null);
            doc.ActiveView.Refresh();
        }

        public static JObject SelectByAttribute(JObject parameters)
        {
            string where = (string)parameters["where"] ?? "";
            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = CapaRequerida(parameters, map);
            IFeatureSelection fsel = lyr as IFeatureSelection;
            if (fsel == null)
                throw new ArgumentException("La capa no admite selección por atributos: " + lyr.Name);

            IQueryFilter filtro = new QueryFilterClass { WhereClause = where };
            fsel.SelectFeatures(filtro, esriSelectionResultEnum.esriSelectionResultNew, false);
            int n = fsel.SelectionSet != null ? fsel.SelectionSet.Count : 0;
            RefrescarSeleccion(doc, map);
            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["where"] = where,
                ["seleccionados"] = n
            });
        }

        public static JObject ClearSelection(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);

            var limpiadas = new JArray();
            if (!string.IsNullOrEmpty(capa))
            {
                ILayer lyr = MapHandlers.FindLayer(map, capa);
                IFeatureSelection fsel = lyr as IFeatureSelection;
                if (fsel == null)
                    throw new ArgumentException("La capa no admite selección: " + capa);
                fsel.Clear();
                limpiadas.Add(lyr.Name);
            }
            else
            {
                foreach (ILayer lyr in MapHandlers.Capas(map))
                {
                    IFeatureSelection fsel = lyr as IFeatureSelection;
                    if (fsel == null)
                        continue;
                    try
                    {
                        fsel.Clear();
                        limpiadas.Add(lyr.Name);
                    }
                    catch { /* capas rotas o sin selección utilizable: seguir */ }
                }
            }
            RefrescarSeleccion(doc, map);
            return Protocol.Result(new JObject
            {
                ["capas"] = limpiadas,
                ["num"] = limpiadas.Count
            });
        }

        public static JObject GetUniqueValues(JObject parameters)
        {
            string campo = (string)parameters["campo"];
            if (string.IsNullOrEmpty(campo))
                throw new ArgumentException("Indica 'campo'.");
            string where = (string)parameters["where"];
            int maxValores = Parametros.LeerEntero(parameters["max_valores"], "max_valores",
                MaxValoresPorDefecto, 1, 100000);

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = CapaRequerida(parameters, map);

            var vistos = new HashSet<object>();
            bool truncado = false;
            ICursor cur = CursorSobreCapa(lyr, where, campo);
            try
            {
                int idx = cur.Fields.FindField(campo);
                if (idx < 0)
                    throw new ArgumentException("Campo no encontrado: " + campo
                        + ". Disponibles: " + NombresDeCampos(CamposDeCapa(lyr)));

                IRow row;
                while ((row = cur.NextRow()) != null)
                {
                    object v = row.get_Value(idx);
                    if (v is DBNull)
                        v = null;
                    if (vistos.Contains(v))
                        continue;
                    // Se corta al pasarse, no al llegar: el tope se anuncia en la
                    // respuesta con 'truncado', nunca en silencio.
                    if (vistos.Count >= maxValores)
                    {
                        truncado = true;
                        break;
                    }
                    vistos.Add(v);
                }
            }
            finally
            {
                DataAccess.SoltarCom(cur);
            }

            var lista = new List<object>(vistos);
            try
            {
                lista.Sort(Comparer<object>.Default);
            }
            catch { /* tipos mezclados no comparables: devolver sin ordenar */ }

            var valores = new JArray();
            foreach (object v in lista)
                valores.Add(DataAccess.ValorAJson(v));

            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["campo"] = campo,
                ["num"] = valores.Count,
                ["max_valores"] = maxValores,
                ["truncado"] = truncado,
                ["valores"] = valores
            });
        }

        public static JObject CountFeatures(JObject parameters)
        {
            string where = (string)parameters["where"];
            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = CapaRequerida(parameters, map);
            int n = ContarFeatures(lyr, where);
            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["where"] = where,
                ["num"] = n
            });
        }

        public static JObject ListFields(JObject parameters)
        {
            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = CapaRequerida(parameters, map);
            IFields fields = CamposDeCapa(lyr);

            var campos = new JArray();
            for (int i = 0; i < fields.FieldCount; i++)
            {
                IField f = fields.get_Field(i);
                campos.Add(new JObject
                {
                    ["nombre"] = f.Name,
                    ["tipo"] = DataAccess.NombreTipoCampo(f.Type),
                    ["alias"] = f.AliasName,
                    ["longitud"] = f.Length
                });
            }
            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["num"] = campos.Count,
                ["campos"] = campos
            });
        }

        public static JObject GetLayerInfo(JObject parameters)
        {
            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = CapaRequerida(parameters, map);

            string tipoGeom = null;
            string crs = null;
            JObject ext = null;
            int? num = null;
            var campos = new JArray();

            IGeoDataset gds = null;
            IFeatureLayer fl = lyr as IFeatureLayer;
            if (fl != null)
            {
                try
                {
                    if (fl.FeatureClass != null)
                    {
                        tipoGeom = DataAccess.NombreTipoGeometria(fl.FeatureClass.ShapeType);
                        gds = fl.FeatureClass as IGeoDataset;
                    }
                }
                catch { /* fuente rota: seguir con lo que haya */ }
                try
                {
                    num = ContarFeatures(lyr, null);
                }
                catch { }
                try
                {
                    IFields fields = CamposDeCapa(lyr);
                    for (int i = 0; i < fields.FieldCount; i++)
                    {
                        IField f = fields.get_Field(i);
                        campos.Add(new JObject
                        {
                            ["nombre"] = f.Name,
                            ["tipo"] = DataAccess.NombreTipoCampo(f.Type)
                        });
                    }
                }
                catch { }
            }
            else
            {
                // Capas no vectoriales (ráster, TIN...): CRS y extent del propio dataset.
                gds = lyr as IGeoDataset;
            }

            if (gds != null)
            {
                try
                {
                    if (gds.SpatialReference != null)
                        crs = gds.SpatialReference.Name;
                }
                catch { }
                try
                {
                    ESRI.ArcGIS.Geometry.IEnvelope e = gds.Extent;
                    if (e != null && !e.IsEmpty)
                        ext = new JObject
                        {
                            ["xmin"] = e.XMin,
                            ["ymin"] = e.YMin,
                            ["xmax"] = e.XMax,
                            ["ymax"] = e.YMax
                        };
                }
                catch { }
            }

            string workspace;
            string fuente = DataAccess.RutaFuente(lyr, out workspace);

            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["tipo_geometria"] = tipoGeom,
                ["crs"] = crs,
                ["extent"] = ext,
                ["num_features"] = num,
                ["campos"] = campos,
                ["fuente"] = fuente
            });
        }

        public static JObject GetLayerFeatures(JObject parameters)
        {
            string where = (string)parameters["where"];
            JArray camposParam = parameters["campos"] as JArray;
            int limite = Parametros.LeerEntero(parameters["limite"], "limite", 50, 1, MaxLimiteFilas);

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = CapaRequerida(parameters, map);

            var nombres = new List<string>();
            string subcampos = null;
            if (camposParam != null && camposParam.Count > 0)
            {
                foreach (JToken t in camposParam)
                    nombres.Add((string)t);
                subcampos = string.Join(",", nombres);
            }
            else
            {
                // Todos salvo geometría/blob/ráster.
                IFields fields = CamposDeCapa(lyr);
                for (int i = 0; i < fields.FieldCount; i++)
                {
                    IField f = fields.get_Field(i);
                    if (f.Type == esriFieldType.esriFieldTypeGeometry
                        || f.Type == esriFieldType.esriFieldTypeBlob
                        || f.Type == esriFieldType.esriFieldTypeRaster)
                        continue;
                    nombres.Add(f.Name);
                }
            }

            var filas = new JArray();
            ICursor cur = CursorSobreCapa(lyr, where, subcampos);
            try
            {
                var indices = new int[nombres.Count];
                for (int i = 0; i < nombres.Count; i++)
                {
                    indices[i] = cur.Fields.FindField(nombres[i]);
                    if (indices[i] < 0)
                        throw new ArgumentException("Campo no encontrado: " + nombres[i]
                            + ". Disponibles: " + NombresDeCampos(CamposDeCapa(lyr)));
                }

                IRow row;
                while (filas.Count < limite && (row = cur.NextRow()) != null)
                {
                    var fila = new JObject();
                    for (int i = 0; i < nombres.Count; i++)
                        fila[nombres[i]] = DataAccess.ValorAJson(row.get_Value(indices[i]));
                    filas.Add(fila);
                }
            }
            finally
            {
                // El camino normal de esta tool es SALIRSE al llegar al límite, con
                // el cursor a mitad de tabla: sin este finally quedaba abierto.
                DataAccess.SoltarCom(cur);
            }

            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["campos"] = new JArray(nombres),
                ["num"] = filas.Count,
                ["limite"] = limite,
                ["filas"] = filas
            });
        }

        private static string NombresDeCampos(IFields fields)
        {
            var nombres = new List<string>();
            for (int i = 0; i < fields.FieldCount; i++)
                nombres.Add(fields.get_Field(i).Name);
            return string.Join(", ", nombres);
        }
    }
}
