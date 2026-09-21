using System;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geodatabase;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// get_arcmap_info / list_layers / zoom_to_layer — contrato JSON que esperan
    /// los schemas del servidor MCP.
    /// </summary>
    internal static class InfoHandlers
    {
        public static JObject GetArcmapInfo(JObject parameters)
        {
            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);

            // Ruta del .mxd: el último item de ITemplates es el documento actual.
            string mxdPath = null;
            try
            {
                ITemplates templates = app.Templates;
                if (templates != null && templates.Count > 0)
                    mxdPath = templates.get_Item(templates.Count - 1);
            }
            catch { /* documento sin guardar o Templates no disponible */ }

            var dataFrames = new JArray();
            IMaps maps = doc.Maps;
            for (int i = 0; i < maps.Count; i++)
                dataFrames.Add(maps.get_Item(i).Name);

            // La escala puede no estar definida según el estado del documento;
            // que no tumbe toda la llamada.
            double? escala = null;
            try
            {
                if (doc.FocusMap != null)
                    escala = doc.FocusMap.MapScale;
            }
            catch { }

            // Vista activa: nombre del data frame en vista de datos, o "PAGE_LAYOUT".
            string vistaActiva = doc.ActiveView is IPageLayout
                ? "PAGE_LAYOUT"
                : (doc.FocusMap != null ? doc.FocusMap.Name : null);

            return Protocol.Result(new JObject
            {
                ["mxd"] = mxdPath,
                ["titulo"] = app.Document.Title,
                ["data_frames"] = dataFrames,
                ["df_activo"] = doc.FocusMap != null ? doc.FocusMap.Name : null,
                ["escala_activa"] = escala,
                ["vista_activa"] = vistaActiva
            });
        }

        public static JObject ListLayers(JObject parameters)
        {
            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            IMap map = doc.FocusMap;
            if (map == null)
                throw new InvalidOperationException("No hay data frame activo.");

            var capas = new JArray();
            // Recorrido recursivo (grupos incluidos), como arcpy ListLayers. Por el
            // helper, que tolera el data frame VACÍO: get_Layers(null, true) lanza
            // E_FAIL sin capas y list_layers fallaba en un mxd recién creado en vez
            // de devolver num 0. 'ruta' da la ruta de grupo, que es lo único que
            // distingue dos capas con el mismo nombre.
            foreach (MapHandlers.CapaEnMapa c in MapHandlers.CapasConRuta(map))
            {
                ILayer lyr = c.Capa;
                var item = new JObject
                {
                    ["nombre"] = c.Nombre,
                    ["ruta"] = c.Ruta,
                    ["visible"] = lyr.Visible,
                    ["es_grupo"] = lyr is IGroupLayer
                };
                try
                {
                    // Por DataAccess.RutaFuente, no componiendo la ruta aquí: esta 'fuente'
                    // se copia y se pega en 'params' de run_geoprocessing, y sin la extensión
                    // real el multivalor daba ERROR 000732 (regresión del 2026-09-21).
                    string workspaceCapa;
                    string fuenteCapa = DataAccess.RutaFuente(lyr, out workspaceCapa);
                    if (fuenteCapa != null)
                        item["fuente"] = fuenteCapa;
                }
                catch { /* capas sin fuente resoluble (rotas, servicios) */ }

                IFeatureLayerDefinition def = lyr as IFeatureLayerDefinition;
                if (def != null)
                    item["definition_query"] = def.DefinitionExpression ?? "";

                capas.Add(item);
            }

            return Protocol.Result(new JObject
            {
                ["data_frame"] = map.Name,
                ["num"] = capas.Count,
                ["capas"] = capas
            });
        }

        public static JObject ZoomToLayer(JObject parameters)
        {
            string nombre = (string)parameters["nombre"];
            if (string.IsNullOrEmpty(nombre))
                throw new ArgumentException("Indica 'nombre' (nombre de la capa en la TOC).");

            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            IMap map = doc.FocusMap;
            if (map == null)
                throw new InvalidOperationException("No hay data frame activo.");

            // Misma búsqueda que el resto del add-in: tolera el data frame vacío y no
            // elige a ciegas entre dos capas con el mismo nombre.
            ILayer objetivo = MapHandlers.FindLayer(map, nombre);

            IEnvelope extent = objetivo.AreaOfInterest;
            if (extent == null || extent.IsEmpty)
                throw new InvalidOperationException("La capa no tiene extent utilizable: " + nombre);

            // AreaOfInterest viene en el CRS de la FUENTE y el setter de Extent no
            // reproyecta: con la capa en geográficas y el data frame en UTM el
            // encuadre se iba al origen.
            extent = MapHandlers.ProyectarAlMapa(extent, map);

            IActiveView mapView = (IActiveView)map;
            mapView.Extent = extent;
            doc.ActiveView.Refresh();

            return Protocol.Result(new JObject
            {
                ["capa"] = nombre,
                ["escala"] = map.MapScale
            });
        }
    }
}
