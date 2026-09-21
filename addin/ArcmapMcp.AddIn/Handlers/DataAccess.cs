using System;
using System.IO;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.DataSourcesFile;
using ESRI.ArcGIS.DataSourcesGDB;
using ESRI.ArcGIS.DataSourcesRaster;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Utilidades compartidas de acceso a datos para los handlers de capas+datos:
    /// apertura de workspaces (carpeta shapefile / file gdb / ráster), creación de
    /// capas desde ruta y mapeo de los enums ArcObjects al vocabulario arcpy que
    /// usa el contrato JSON del servidor MCP ("String", "Polygon", "FeatureClass"...).
    /// </summary>
    internal static class DataAccess
    {
        private static readonly string[] ExtensionesRaster =
            { ".tif", ".tiff", ".img", ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".sid", ".ecw", ".jp2", ".asc" };

        public static bool EsExtensionRaster(string ext)
        {
            return Array.IndexOf(ExtensionesRaster, (ext ?? "").ToLowerInvariant()) >= 0;
        }

        /// <summary>
        /// Crea una workspace factory ACTIVÁNDOLA POR ProgID, nunca con
        /// `new XxxWorkspaceFactoryClass()`.
        ///
        /// El `new` de la coclase lanza `InvalidCastException` («no se puede convertir un
        /// objeto de tipo System.__ComObject al tipo ...WorkspaceFactoryClass») cuando el
        /// RCW no puede castear el objeto COM que devuelve el registro. Medido el
        /// 2026-09-21 en sesión viva: con **FileGDB fallaba siempre**, ya en sesión limpia,
        /// y con **shapefile a partir del segundo pase** de `regresion_sesion_viva.py`
        /// dentro de la misma sesión de ArcMap. Una vez roto se lleva por delante a TODOS
        /// los que pasan por aquí —`add_layer`, `describe_data`, `list_feature_classes`,
        /// `set_workspace`— hasta cerrar ArcMap, y sin más aviso que el cast.
        ///
        /// La activación por ProgID pide el objeto por su interfaz y no pasa por ese cast.
        /// Se usa el ProgID y no el CLSID a pelo para no fijar en el código GUIDs que solo
        /// viven en el registro de ArcGIS Desktop.
        /// </summary>
        private static IWorkspaceFactory CrearFactory(string progId)
        {
            Type tipo = Type.GetTypeFromProgID(progId, false);
            if (tipo == null)
                throw new InvalidOperationException(
                    "No está registrada la factory COM '" + progId + "'. Apunta a una "
                    + "instalación de ArcGIS Desktop incompleta o de otra versión.");
            IWorkspaceFactory factory = Activator.CreateInstance(tipo) as IWorkspaceFactory;
            if (factory == null)
                throw new InvalidOperationException(
                    "La factory COM '" + progId + "' no expone IWorkspaceFactory.");
            return factory;
        }

        /// <summary>Abre un workspace: ".gdb" → FileGDB; carpeta → shapefiles o
        /// rásteres de archivo según paraRasters. Error accionable si no existe.</summary>
        public static IWorkspace AbrirWorkspace(string ruta, bool paraRasters)
        {
            if (string.IsNullOrEmpty(ruta))
                throw new ArgumentException("Define 'workspace' o fíjalo antes con set_workspace.");
            if (!Directory.Exists(ruta))
                throw new ArgumentException("Workspace no encontrado (carpeta o .gdb): " + ruta);

            IWorkspaceFactory factory;
            if (ruta.TrimEnd('\\', '/').ToLowerInvariant().EndsWith(".gdb"))
                factory = CrearFactory("esriDataSourcesGDB.FileGDBWorkspaceFactory");
            else if (paraRasters)
                factory = CrearFactory("esriDataSourcesRaster.RasterWorkspaceFactory");
            else
                factory = CrearFactory("esriDataSourcesFile.ShapefileWorkspaceFactory");
            return factory.OpenFromFile(ruta, 0);
        }

        /// <summary>
        /// Suelta un objeto COM de ArcObjects (típicamente un cursor). Sin esto el
        /// cursor deja lock sobre la fuente hasta que el GC pase, y con shapefiles
        /// en red eso se nota enseguida. Tolera null y objetos que no son COM para
        /// poder llamarse siempre desde un finally.
        /// </summary>
        public static void SoltarCom(object com)
        {
            if (com == null)
                return;
            try
            {
                if (System.Runtime.InteropServices.Marshal.IsComObject(com))
                    System.Runtime.InteropServices.Marshal.ReleaseComObject(com);
            }
            catch { /* ya soltado o RCW separado: nada que hacer */ }
        }

        /// <summary>Divide una ruta a dataset de gdb en (ruta de la .gdb, nombre del
        /// dataset = último segmento). Los nombres de fc son únicos en toda la gdb,
        /// también dentro de feature datasets.
        ///
        /// El ".gdb" que cuenta es el que TERMINA un segmento de ruta: con la primera
        /// aparición a secas, "D:\copias.gdb_old\x.tif" se tomaba por una gdb llamada
        /// "D:\copias.gdb" y el ráster no se abría nunca.</summary>
        public static bool SepararRutaGdb(string ruta, out string gdb, out string nombre)
        {
            gdb = null;
            nombre = null;
            if (string.IsNullOrEmpty(ruta))
                return false;
            int idx = IndiceDeGdb(ruta);
            if (idx < 0)
                return false;
            gdb = ruta.Substring(0, idx + 4);
            string resto = ruta.Substring(idx + 4).Trim('\\', '/');
            if (resto.Length == 0)
                return false;
            int corte = resto.LastIndexOfAny(new[] { '\\', '/' });
            nombre = corte >= 0 ? resto.Substring(corte + 1) : resto;
            return true;
        }

        /// <summary>Primera aparición de ".gdb" que cierra un segmento de ruta
        /// (seguida de separador o de fin de cadena), o -1.</summary>
        private static int IndiceDeGdb(string ruta)
        {
            string baja = ruta.ToLowerInvariant();
            int desde = 0;
            while (desde <= baja.Length - 4)
            {
                int idx = baja.IndexOf(".gdb", desde, StringComparison.Ordinal);
                if (idx < 0)
                    return -1;
                int fin = idx + 4;
                if (fin == baja.Length || baja[fin] == '\\' || baja[fin] == '/')
                    return idx;
                desde = idx + 1;
            }
            return -1;
        }

        /// <summary>Crea una ILayer desde una ruta en disco: .shp, feature class o
        /// ráster de file gdb, o ráster de archivo — equivalente al arcpy.mapping.Layer(ruta).</summary>
        public static ILayer CrearCapaDesdeRuta(string fuente)
        {
            string ext = Path.GetExtension(fuente ?? "").ToLowerInvariant();
            string gdb, nombre;

            if (ext == ".shp")
            {
                if (!File.Exists(fuente))
                    throw new ArgumentException("No existe el shapefile: " + fuente);
                IWorkspace ws = AbrirWorkspace(Path.GetDirectoryName(fuente), false);
                IFeatureClass fc = ((IFeatureWorkspace)ws).OpenFeatureClass(Path.GetFileNameWithoutExtension(fuente));
                return CapaDesdeFeatureClass(fc);
            }

            if (SepararRutaGdb(fuente, out gdb, out nombre))
            {
                if (!Directory.Exists(gdb))
                    throw new ArgumentException("No existe la geodatabase: " + gdb);
                IWorkspace ws = AbrirWorkspace(gdb, false);
                try
                {
                    IFeatureClass fc = ((IFeatureWorkspace)ws).OpenFeatureClass(nombre);
                    return CapaDesdeFeatureClass(fc);
                }
                catch (Exception exFc)
                {
                    // No es feature class: probar como ráster de gdb antes de rendirse.
                    // El error ORIGINAL viaja en el mensaje: antes, cualquier fallo al
                    // abrir la feature class (permisos, lock, gdb corrupta) se
                    // convertía en un error de ráster y el cliente diagnosticaba otra
                    // cosa.
                    IRasterWorkspaceEx rwx = ws as IRasterWorkspaceEx;
                    if (rwx == null)
                        throw;
                    try
                    {
                        IRasterDataset rd = rwx.OpenRasterDataset(nombre);
                        IRasterLayer rl = new RasterLayerClass();
                        rl.CreateFromDataset(rd);
                        return (ILayer)rl;
                    }
                    catch (Exception exRaster)
                    {
                        throw new ArgumentException("No se pudo abrir '" + nombre + "' en " + gdb
                            + ": como feature class falló con \"" + exFc.Message
                            + "\" y como ráster con \"" + exRaster.Message + "\".", exFc);
                    }
                }
            }

            if (EsExtensionRaster(ext))
            {
                if (!File.Exists(fuente))
                    throw new ArgumentException("No existe el ráster: " + fuente);
                IRasterLayer rl = new RasterLayerClass();
                rl.CreateFromFilePath(fuente);
                return (ILayer)rl;
            }

            throw new ArgumentException(
                "Fuente no soportada (se admite .shp, feature class/ráster de .gdb o ráster de archivo): " + fuente);
        }

        private static ILayer CapaDesdeFeatureClass(IFeatureClass fc)
        {
            IFeatureLayer fl = new FeatureLayerClass();
            fl.FeatureClass = fc;
            fl.Name = ((IDataset)fc).Name;
            return (ILayer)fl;
        }

        /// <summary>Extensiones de los datasets de un workspace de CARPETA, que es el
        /// único caso en que el name object da el nombre sin ella. El orden cuenta: un
        /// shapefile trae un .dbf al lado con el mismo nombre, así que el .shp manda.</summary>
        private static readonly string[] ExtensionesDeCarpeta = { ".shp", ".dbf" };

        /// <summary>
        /// Devuelve la ruta con la extensión que el dataset tiene DE VERDAD en disco.
        ///
        /// En un workspace de carpeta, el `IDatasetName.Name` de un shapefile es
        /// "parcelas", no "parcelas.shp", así que `workspace + Name` da una ruta que **no
        /// se puede abrir**. Era el ERROR 000732 del multivalor de `run_geoprocessing`
        /// (visto en la regresión del 2026-09-21): `Merge C:\dir\a;C:\dir\b` con el
        /// geoprocesador diciendo que ese dataset no existe.
        ///
        /// Solo se añade la extensión si el fichero está realmente ahí. Lo que ya existe
        /// se devuelve intacto, y eso cubre a propósito cuatro casos que NO hay que tocar:
        /// una feature class de geodatabase (`C:\x.gdb\fc`, que no es un fichero suelto),
        /// un ráster de carpeta (cuyo Name ya trae el ".tif"), un GRID de ESRI (que es un
        /// directorio) y una capa ROTA (no hay nada que sondear, e inventarle una
        /// extensión daría una ruta igual de inexistente pero más difícil de diagnosticar).
        /// </summary>
        private static string ConExtensionReal(string ruta)
        {
            try
            {
                if (string.IsNullOrEmpty(ruta) || File.Exists(ruta) || Directory.Exists(ruta))
                    return ruta;
                foreach (string ext in ExtensionesDeCarpeta)
                    if (File.Exists(ruta + ext))
                        return ruta + ext;
            }
            catch { /* ruta con caracteres inválidos: se devuelve tal cual */ }
            return ruta;
        }

        /// <summary>Ruta completa de la fuente de una capa (workspace + dataset),
        /// equivalente al catalogPath/dataSource de arcpy. Null si no es resoluble.
        /// Lleva la extensión real cuando el dataset vive en una carpeta (ver
        /// ConExtensionReal): sin ella la ruta no es abrible por un geoproceso.</summary>
        public static string RutaFuente(ILayer lyr, out string workspace)
        {
            workspace = null;
            try
            {
                IDataLayer dataLayer = lyr as IDataLayer;
                IDatasetName dsn = dataLayer != null ? dataLayer.DataSourceName as IDatasetName : null;
                if (dsn == null)
                    return null;
                IWorkspaceName wsn = dsn.WorkspaceName;
                workspace = wsn != null ? wsn.PathName : null;
                return ConExtensionReal(
                    workspace != null ? Path.Combine(workspace, dsn.Name) : dsn.Name);
            }
            catch
            {
                return null; // capas sin fuente resoluble (servicios, rotas sin name object)
            }
        }

        /// <summary>Nombre arcpy del tipo de campo (el Field.type de arcpy).</summary>
        public static string NombreTipoCampo(esriFieldType tipo)
        {
            switch (tipo)
            {
                case esriFieldType.esriFieldTypeSmallInteger: return "SmallInteger";
                case esriFieldType.esriFieldTypeInteger: return "Integer";
                case esriFieldType.esriFieldTypeSingle: return "Single";
                case esriFieldType.esriFieldTypeDouble: return "Double";
                case esriFieldType.esriFieldTypeString: return "String";
                case esriFieldType.esriFieldTypeDate: return "Date";
                case esriFieldType.esriFieldTypeOID: return "OID";
                case esriFieldType.esriFieldTypeGeometry: return "Geometry";
                case esriFieldType.esriFieldTypeBlob: return "Blob";
                case esriFieldType.esriFieldTypeRaster: return "Raster";
                case esriFieldType.esriFieldTypeGUID: return "GUID";
                case esriFieldType.esriFieldTypeGlobalID: return "GlobalID";
                case esriFieldType.esriFieldTypeXML: return "XML";
                default: return tipo.ToString();
            }
        }

        /// <summary>Nombre arcpy del tipo de geometría (el Describe.shapeType de arcpy).</summary>
        public static string NombreTipoGeometria(ESRI.ArcGIS.Geometry.esriGeometryType tipo)
        {
            switch (tipo)
            {
                case ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPoint: return "Point";
                case ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryMultipoint: return "Multipoint";
                case ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPolyline: return "Polyline";
                case ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPolygon: return "Polygon";
                case ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryMultiPatch: return "MultiPatch";
                default: return tipo.ToString();
            }
        }

        /// <summary>Nombre arcpy del tipo de dataset (el Describe.datasetType de arcpy).</summary>
        public static string NombreTipoDataset(esriDatasetType tipo)
        {
            switch (tipo)
            {
                case esriDatasetType.esriDTFeatureClass: return "FeatureClass";
                case esriDatasetType.esriDTFeatureDataset: return "FeatureDataset";
                case esriDatasetType.esriDTTable: return "Table";
                case esriDatasetType.esriDTRasterDataset: return "RasterDataset";
                case esriDatasetType.esriDTRasterCatalog: return "RasterCatalog";
                default: return tipo.ToString();
            }
        }

        /// <summary>Valor de atributo COM → JSON. Las fechas van como texto para no
        /// depender de la serialización por defecto de Json.NET.</summary>
        public static JToken ValorAJson(object v)
        {
            if (v == null || v is DBNull)
                return JValue.CreateNull();
            if (v is DateTime)
                return ((DateTime)v).ToString("yyyy-MM-dd HH:mm:ss");
            try
            {
                return new JValue(v);
            }
            catch
            {
                return v.ToString();
            }
        }

        /// <summary>Combina definition query y where en un solo predicado SQL.</summary>
        public static string CombinarWhere(string defQuery, string where)
        {
            bool hayDq = !string.IsNullOrEmpty(defQuery);
            bool hayWhere = !string.IsNullOrEmpty(where);
            if (hayDq && hayWhere)
                return "(" + defQuery + ") AND (" + where + ")";
            return hayDq ? defQuery : (hayWhere ? where : null);
        }
    }
}
