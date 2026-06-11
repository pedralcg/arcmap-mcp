using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Servidor TCP del add-in: atiende al servidor MCP externo (FastMCP) por el
    /// puerto 27179. El listener corre en thread de fondo; cada petición se
    /// marshalea al hilo STA vía StaDispatcher.
    /// </summary>
    internal class McpServer
    {
        // Solo loopback por diseño (execute_arcpy = ejecución de código): el
        // acceso remoto se hace por túnel, nunca exponiendo el puerto.
        public const int Port = 27179;
        private const int ReadTimeoutMs = 5000;
        private const int MaxRequestBytes = 1024 * 1024; // los requests son pequeños; 1MB = algo va mal
        private static readonly TimeSpan HandlerTimeout = TimeSpan.FromSeconds(60);

        private TcpListener _listener;
        private Thread _acceptThread;
        private volatile bool _running;

        // UNA petición en vuelo. Si llega otra mientras ArcMap trabaja, respuesta
        // "busy" inmediata — nunca encolar: encolar a ciegas degrada en cascada
        // cuando el dibujado (p. ej. servicios WMS lentos) retiene el hilo STA.
        private readonly SemaphoreSlim _gate = new SemaphoreSlim(1, 1);

        public bool IsRunning
        {
            get { return _running; }
        }

        public void Start()
        {
            _listener = new TcpListener(IPAddress.Loopback, Port);
            _listener.Start();
            _running = true;
            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "ArcmapMcp.Accept"
            };
            _acceptThread.Start();
            Log.Info("Servidor TCP escuchando en 127.0.0.1:" + Port);
        }

        public void Stop()
        {
            _running = false;
            try { _listener.Stop(); } catch { /* ya cerrado */ }
            Log.Info("Servidor TCP detenido");
        }

        private void AcceptLoop()
        {
            while (_running)
            {
                TcpClient client;
                try
                {
                    client = _listener.AcceptTcpClient();
                }
                catch
                {
                    break; // Stop() cierra el listener y rompe el Accept: salida limpia
                }
                ThreadPool.QueueUserWorkItem(delegate { HandleClient(client); });
            }
        }

        private void HandleClient(TcpClient client)
        {
            using (client)
            {
                NetworkStream stream = client.GetStream();
                JObject response;
                try
                {
                    JObject request = ReadRequest(stream);
                    if (request == null)
                    {
                        response = Protocol.Error("Request ilegible (no llegó un objeto JSON válido)");
                    }
                    else if (!_gate.Wait(0))
                    {
                        response = Protocol.Error("busy: ArcMap está atendiendo otra petición; reintenta en unos segundos");
                    }
                    else
                    {
                        try
                        {
                            response = Dispatch(request);
                        }
                        finally
                        {
                            _gate.Release();
                        }
                    }
                }
                catch (Exception ex)
                {
                    Log.Error("Error atendiendo cliente", ex);
                    response = Protocol.Error("Error interno del add-in: " + ex.Message);
                }

                try
                {
                    byte[] payload = Encoding.UTF8.GetBytes(response.ToString(Formatting.None));
                    stream.Write(payload, 0, payload.Length);
                }
                catch (Exception ex)
                {
                    Log.Error("Error escribiendo la respuesta", ex);
                }
                // El using cierra la conexión => EOF para el servidor MCP.
            }
        }

        /// <summary>
        /// El relay envía UN objeto JSON y espera SIN cerrar su lado de envío
        /// (no hay shutdown): no se puede leer hasta EOF. Se acumula y se intenta
        /// el parse tras cada chunk hasta que el JSON está completo.
        /// </summary>
        private static JObject ReadRequest(NetworkStream stream)
        {
            stream.ReadTimeout = ReadTimeoutMs;
            var buf = new MemoryStream();
            var chunk = new byte[8192];
            while (buf.Length < MaxRequestBytes)
            {
                int n;
                try
                {
                    n = stream.Read(chunk, 0, chunk.Length);
                }
                catch (IOException)
                {
                    break; // timeout de lectura sin JSON completo
                }
                if (n <= 0)
                    break;
                buf.Write(chunk, 0, n);
                try
                {
                    return JObject.Parse(Encoding.UTF8.GetString(buf.ToArray()));
                }
                catch (JsonReaderException)
                {
                    // JSON aún incompleto (o byte multibyte cortado): seguir leyendo.
                }
            }
            return null;
        }

        // Comandos nativos ArcObjects. Corren en el hilo STA vía StaDispatcher;
        // el nombre y contrato JSON de cada comando es el que esperan los schemas
        // del servidor MCP (src/arcmap_mcp_server.py).
        private static readonly System.Collections.Generic.Dictionary<string, Func<JObject, JObject>> _handlers =
            new System.Collections.Generic.Dictionary<string, Func<JObject, JObject>>
            {
                { "ping",                  Handlers.PingHandler.Run },
                { "get_arcmap_info",       Handlers.InfoHandlers.GetArcmapInfo },
                { "list_layers",           Handlers.InfoHandlers.ListLayers },
                { "zoom_to_layer",         Handlers.InfoHandlers.ZoomToLayer },
                { "set_text_element",      Handlers.LayoutHandlers.SetTextElement },
                { "get_canvas_screenshot", Handlers.ScreenshotHandler.Run },
                { "list_layout_elements",  Handlers.LayoutHandlers.ListLayoutElements },
                { "export_pdf",            Handlers.ExportHandlers.ExportPdf },
                { "export_jpg",            Handlers.ExportHandlers.ExportJpg },
                { "export_view_png",       Handlers.ExportHandlers.ExportViewPng },
                { "refresh",               Handlers.MapHandlers.Refresh },
                { "set_scale",             Handlers.MapHandlers.SetScale },
                { "set_extent",            Handlers.MapHandlers.SetExtent },
                { "set_layer_visibility",  Handlers.MapHandlers.SetLayerVisibility },
                { "set_definition_query",  Handlers.MapHandlers.SetDefinitionQuery },
                { "save_mxd",              Handlers.DocumentHandlers.SaveMxd },
                { "save_mxd_as",           Handlers.DocumentHandlers.SaveMxdAs },
                // Geoprocesamiento nativo sobre la sesión viva.
                { "run_geoprocessing",     Handlers.GeoprocessingHandlers.RunGeoprocessing },
                { "calculate_geometry",    Handlers.GeoprocessingHandlers.CalculateGeometry },
                // Capas y datos.
                { "select_by_attribute",        Handlers.QueryHandlers.SelectByAttribute },
                { "clear_selection",            Handlers.QueryHandlers.ClearSelection },
                { "get_unique_values",          Handlers.QueryHandlers.GetUniqueValues },
                { "count_features",             Handlers.QueryHandlers.CountFeatures },
                { "list_fields",                Handlers.QueryHandlers.ListFields },
                { "get_layer_info",             Handlers.QueryHandlers.GetLayerInfo },
                { "get_layer_features",         Handlers.QueryHandlers.GetLayerFeatures },
                { "add_layer",                  Handlers.LayerHandlers.AddLayer },
                { "remove_layer",               Handlers.LayerHandlers.RemoveLayer },
                { "apply_symbology_from_layer", Handlers.LayerHandlers.ApplySymbologyFromLayer },
                { "describe_data",              Handlers.WorkspaceHandlers.DescribeData },
                { "list_data_frames",           Handlers.DataFrameHandlers.ListDataFrames },
                { "set_active_df",              Handlers.DataFrameHandlers.SetActiveDf },
                { "get_workspace",              Handlers.WorkspaceHandlers.GetWorkspace },
                { "set_workspace",              Handlers.WorkspaceHandlers.SetWorkspace },
                { "list_feature_classes",       Handlers.WorkspaceHandlers.ListFeatureClasses },
                { "list_tables",                Handlers.WorkspaceHandlers.ListTables },
                { "list_rasters",               Handlers.WorkspaceHandlers.ListRasters },
                { "list_broken_data_sources",   Handlers.SourceHandlers.ListBrokenDataSources },
                { "repair_data_source",         Handlers.SourceHandlers.RepairDataSource },
            };

        // Comandos que ArcObjects no cubre (código arcpy arbitrario, Data Driven
        // Pages, análisis ambiental), resueltos OUT-OF-PROCESS con el arcpy
        // standalone sobre un snapshot del documento. Corren en ESTE thread de
        // fondo — el subprocess no congela la GUI de ArcMap — y por dentro
        // marshalean al STA solo los pasos ArcObjects (snapshot, añadir al mapa,
        // extent). El gate busy sigue garantizando UNA petición en vuelo.
        private static readonly System.Collections.Generic.Dictionary<string, Func<JObject, JObject>> _handlersFondo =
            new System.Collections.Generic.Dictionary<string, Func<JObject, JObject>>
            {
                { "execute_code",         Handlers.PythonHandlers.ExecuteArcpy },
                { "list_ddp",             Handlers.PythonHandlers.ListDdp },
                { "export_ddp",           Handlers.PythonHandlers.ExportDdp },
                { "goto_ddp_page",        Handlers.PythonHandlers.GotoDdpPage },
                { "raster_index",         Handlers.PythonHandlers.RasterIndex },
                { "hydrology",            Handlers.PythonHandlers.Hydrology },
                { "contours",             Handlers.PythonHandlers.Contours },
                { "topographic_profile",  Handlers.PythonHandlers.TopographicProfile },
                { "least_cost_path",      Handlers.PythonHandlers.LeastCostPath },
            };

        // Exports a disco y GP nativos pueden tardar mucho más de 60s (layouts densos,
        // dpi alto, geoprocesos): timeout STA amplio, filosofía del ARCMAP_GP_TIMEOUT.
        private static readonly System.Collections.Generic.HashSet<string> _comandosLargos =
            new System.Collections.Generic.HashSet<string> { "export_pdf", "export_jpg", "export_view_png", "run_geoprocessing" };
        private static readonly TimeSpan LongHandlerTimeout = TimeSpan.FromSeconds(1800);

        private static JObject Dispatch(JObject request)
        {
            string type = (string)request["type"];
            JObject parameters = request["params"] as JObject ?? new JObject();
            Log.Info("Comando recibido: " + type);

            Func<JObject, JObject> handler;
            if (type != null && _handlersFondo.TryGetValue(type, out handler))
            {
                // Out-of-process: se ejecuta aquí mismo (thread de fondo); el handler
                // gestiona su propio timeout de subprocess y sus pasos STA internos.
                try
                {
                    return handler(parameters);
                }
                catch (Exception ex)
                {
                    Log.Error("Handler de fondo lanzó excepción", ex);
                    return Protocol.Error(ex.Message, ex);
                }
            }
            if (type == null || !_handlers.TryGetValue(type, out handler))
            {
                var implementados = new System.Collections.Generic.List<string>(_handlers.Keys);
                implementados.AddRange(_handlersFondo.Keys);
                return Protocol.Error(
                    "Comando desconocido: '" + type + "'. Implementados: "
                    + string.Join(", ", implementados));
            }
            TimeSpan timeout = _comandosLargos.Contains(type) ? LongHandlerTimeout : HandlerTimeout;
            return StaDispatcher.Invoke(delegate { return handler(parameters); }, timeout);
        }
    }
}
