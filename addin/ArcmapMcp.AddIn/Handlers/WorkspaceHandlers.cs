using System;
using System.IO;
using ESRI.ArcGIS.DataSourcesRaster;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// get_workspace / set_workspace / list_feature_classes / list_tables /
    /// list_rasters / describe_data — contrato JSON que esperan los schemas del
    /// servidor MCP. arcpy.env y arcpy.Describe NO existen en ArcObjects: el
    /// workspace es estado propio del add-in y los listados/describe van por
    /// IWorkspaceFactory + IWorkspace.DatasetNames + interfaces de Geodatabase.
    /// </summary>
    internal static class WorkspaceHandlers
    {
        // Estado propio: vive lo que viva el add-in, igual que arcpy.env vive lo
        // que vive su sesión. scratchWorkspace queda por paridad de contrato.
        private static string _workspace;
        private static readonly string _scratchWorkspace = null;

        public static JObject GetWorkspace(JObject parameters)
        {
            return Protocol.Result(new JObject
            {
                ["workspace"] = _workspace,
                ["scratchWorkspace"] = _scratchWorkspace
            });
        }

        public static JObject SetWorkspace(JObject parameters)
        {
            string ws = (string)parameters["workspace"];
            if (string.IsNullOrEmpty(ws))
                throw new ArgumentException("Indica 'workspace' (ruta a gdb o carpeta).");
            if (!Directory.Exists(ws))
                throw new ArgumentException("Workspace no encontrado (carpeta o .gdb): " + ws);
            _workspace = ws;
            return Protocol.Result(new JObject { ["workspace"] = _workspace });
        }

        /// <summary>Workspace efectivo: el del parámetro (sin persistirlo, igual
        /// que arcpy restaura arcpy.env) o el fijado con set_workspace.</summary>
        private static string WorkspaceEfectivo(JObject parameters)
        {
            string ws = (string)parameters["workspace"];
            return !string.IsNullOrEmpty(ws) ? ws : _workspace;
        }

        public static JObject ListFeatureClasses(JObject parameters)
        {
            string ws = WorkspaceEfectivo(parameters);
            IWorkspace workspace = DataAccess.AbrirWorkspace(ws, false);

            var fcs = new JArray();
            IEnumDatasetName en = workspace.get_DatasetNames(esriDatasetType.esriDTFeatureClass);
            en.Reset();
            IDatasetName dn;
            while ((dn = en.Next()) != null)
                fcs.Add(dn.Name);

            // Las feature classes dentro de feature datasets se listan como "dataset/fc".
            IEnumDatasetName dsEnum = workspace.get_DatasetNames(esriDatasetType.esriDTFeatureDataset);
            dsEnum.Reset();
            while ((dn = dsEnum.Next()) != null)
            {
                IEnumDatasetName sub = dn.SubsetNames;
                if (sub == null)
                    continue;
                sub.Reset();
                IDatasetName hijo;
                while ((hijo = sub.Next()) != null)
                {
                    if (hijo is IFeatureClassName)
                        fcs.Add(dn.Name + "/" + hijo.Name);
                }
            }

            return Protocol.Result(new JObject
            {
                ["workspace"] = ws,
                ["num"] = fcs.Count,
                ["feature_classes"] = fcs
            });
        }

        public static JObject ListTables(JObject parameters)
        {
            string ws = WorkspaceEfectivo(parameters);
            IWorkspace workspace = DataAccess.AbrirWorkspace(ws, false);

            var tablas = new JArray();
            IEnumDatasetName en = workspace.get_DatasetNames(esriDatasetType.esriDTTable);
            en.Reset();
            IDatasetName dn;
            while ((dn = en.Next()) != null)
                tablas.Add(dn.Name);

            return Protocol.Result(new JObject
            {
                ["workspace"] = ws,
                ["num"] = tablas.Count,
                ["tablas"] = tablas
            });
        }

        public static JObject ListRasters(JObject parameters)
        {
            string ws = WorkspaceEfectivo(parameters);
            IWorkspace workspace = DataAccess.AbrirWorkspace(ws, true);

            var rasters = new JArray();
            IEnumDatasetName en = workspace.get_DatasetNames(esriDatasetType.esriDTRasterDataset);
            en.Reset();
            IDatasetName dn;
            while ((dn = en.Next()) != null)
                rasters.Add(dn.Name);

            return Protocol.Result(new JObject
            {
                ["workspace"] = ws,
                ["num"] = rasters.Count,
                ["rasters"] = rasters
            });
        }

        public static JObject DescribeData(JObject parameters)
        {
            string ruta = (string)parameters["ruta"];
            if (string.IsNullOrEmpty(ruta))
                throw new ArgumentException("Indica 'ruta' (dataset en disco).");

            var salida = new JObject { ["ruta"] = ruta };
            string ext = Path.GetExtension(ruta).ToLowerInvariant();
            string gdb, nombre;

            if (Directory.Exists(ruta))
            {
                bool esGdb = ruta.TrimEnd('\\', '/').ToLowerInvariant().EndsWith(".gdb");
                salida["dataType"] = esGdb ? "Workspace" : "Folder";
                salida["baseName"] = Path.GetFileName(ruta.TrimEnd('\\', '/'));
                salida["shapeType"] = null;
                salida["datasetType"] = null;
                return Protocol.Result(salida);
            }

            if (ext == ".shp")
            {
                IWorkspace ws = DataAccess.AbrirWorkspace(Path.GetDirectoryName(ruta), false);
                IFeatureClass fc = ((IFeatureWorkspace)ws).OpenFeatureClass(Path.GetFileNameWithoutExtension(ruta));
                return Protocol.Result(DescribirFeatureClass(salida, fc, "ShapeFile"));
            }

            if (ext == ".dbf")
            {
                IWorkspace ws = DataAccess.AbrirWorkspace(Path.GetDirectoryName(ruta), false);
                ITable tabla = ((IFeatureWorkspace)ws).OpenTable(Path.GetFileNameWithoutExtension(ruta));
                salida["dataType"] = "DbaseTable";
                salida["baseName"] = Path.GetFileNameWithoutExtension(ruta);
                salida["shapeType"] = null;
                salida["datasetType"] = DataAccess.NombreTipoDataset(((IDataset)tabla).Type);
                salida["campos"] = CamposAJson(tabla.Fields);
                return Protocol.Result(salida);
            }

            if (DataAccess.SepararRutaGdb(ruta, out gdb, out nombre))
            {
                if (!Directory.Exists(gdb))
                    throw new ArgumentException("No existe la geodatabase: " + gdb);
                IWorkspace ws = DataAccess.AbrirWorkspace(gdb, false);
                try
                {
                    IFeatureClass fc = ((IFeatureWorkspace)ws).OpenFeatureClass(nombre);
                    return Protocol.Result(DescribirFeatureClass(salida, fc, "FeatureClass"));
                }
                catch (ArgumentException) { throw; }
                catch
                {
                    // No es feature class: tabla o ráster de gdb.
                    try
                    {
                        ITable tabla = ((IFeatureWorkspace)ws).OpenTable(nombre);
                        salida["dataType"] = "Table";
                        salida["baseName"] = nombre;
                        salida["shapeType"] = null;
                        salida["datasetType"] = DataAccess.NombreTipoDataset(((IDataset)tabla).Type);
                        salida["campos"] = CamposAJson(tabla.Fields);
                        return Protocol.Result(salida);
                    }
                    catch
                    {
                        IRasterWorkspaceEx rwx = ws as IRasterWorkspaceEx;
                        if (rwx == null)
                            throw;
                        IRasterDataset rd = rwx.OpenRasterDataset(nombre);
                        return Protocol.Result(DescribirRaster(salida, rd, nombre));
                    }
                }
            }

            if (DataAccess.EsExtensionRaster(ext))
            {
                if (!File.Exists(ruta))
                    throw new ArgumentException("No existe el ráster: " + ruta);
                IRasterWorkspace rw = (IRasterWorkspace)DataAccess.AbrirWorkspace(Path.GetDirectoryName(ruta), true);
                IRasterDataset rd = rw.OpenRasterDataset(Path.GetFileName(ruta));
                return Protocol.Result(DescribirRaster(salida, rd, Path.GetFileNameWithoutExtension(ruta)));
            }

            throw new ArgumentException(
                "Ruta no soportada (se admite carpeta, .gdb, .shp, .dbf, dataset de .gdb o ráster de archivo): " + ruta);
        }

        private static JObject DescribirFeatureClass(JObject salida, IFeatureClass fc, string dataType)
        {
            salida["dataType"] = dataType;
            salida["baseName"] = ((IDataset)fc).Name;
            salida["shapeType"] = DataAccess.NombreTipoGeometria(fc.ShapeType);
            salida["datasetType"] = DataAccess.NombreTipoDataset(((IDataset)fc).Type);
            AnadirCrsYExtent(salida, fc as IGeoDataset);
            salida["campos"] = CamposAJson(fc.Fields);
            return salida;
        }

        private static JObject DescribirRaster(JObject salida, IRasterDataset rd, string baseName)
        {
            salida["dataType"] = "RasterDataset";
            salida["baseName"] = baseName;
            salida["shapeType"] = null;
            salida["datasetType"] = DataAccess.NombreTipoDataset(((IDataset)rd).Type);
            AnadirCrsYExtent(salida, rd as IGeoDataset);
            return salida;
        }

        private static void AnadirCrsYExtent(JObject salida, IGeoDataset gds)
        {
            salida["crs"] = null;
            salida["crs_code"] = null;
            if (gds == null)
                return;
            try
            {
                if (gds.SpatialReference != null)
                {
                    salida["crs"] = gds.SpatialReference.Name;
                    salida["crs_code"] = gds.SpatialReference.FactoryCode;
                }
            }
            catch { }
            try
            {
                ESRI.ArcGIS.Geometry.IEnvelope e = gds.Extent;
                if (e != null && !e.IsEmpty)
                    salida["extent"] = new JObject
                    {
                        ["xmin"] = e.XMin,
                        ["ymin"] = e.YMin,
                        ["xmax"] = e.XMax,
                        ["ymax"] = e.YMax
                    };
            }
            catch { }
        }

        private static JArray CamposAJson(IFields fields)
        {
            var campos = new JArray();
            for (int i = 0; i < fields.FieldCount; i++)
            {
                IField f = fields.get_Field(i);
                campos.Add(new JObject
                {
                    ["nombre"] = f.Name,
                    ["tipo"] = DataAccess.NombreTipoCampo(f.Type)
                });
            }
            return campos;
        }
    }
}
