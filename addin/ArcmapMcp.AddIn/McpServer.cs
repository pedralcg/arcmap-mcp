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
        // BIND: solo loopback, y NO configurable a propósito (ADR-006, aceptada
        // 2026-08-28). `execute_arcpy` es ejecución de código arbitrario sin
        // autenticación, sin usuarios y sin TLS: quien alcance este puerto ejecuta lo
        // que quiera con los permisos de quien tenga ArcMap abierto. El loopback no es
        // un descuido pendiente de arreglar, es la única barrera que hay. El acceso a
        // un ArcMap remoto se hace por TÚNEL (SSH o Tailscale con reenvío de puerto),
        // que termina en el 127.0.0.1 del destino y además cifra y autentica: ya
        // alcanza este listener sin abrir nada.
        private static readonly IPAddress Bind = IPAddress.Loopback;

        // PUERTO: este sí es configurable, por `ARCMAP_BRIDGE_PORT` — el MISMO nombre
        // que lee el servidor Python (src/arcmap_mcp_server.py), de modo que una sola
        // variable mueve los dos extremos. No es una comodidad: cuando una instancia de
        // ArcMap se queda zombi sujetando el 27179 sin ventana que cerrar, sin puerto
        // alternativo el sistema entero se queda sin vía de escape hasta matar el
        // proceso por PID (incidente del 2026-08-27). Cambiar el puerto NO saca nada de
        // local: se sigue escuchando solo en loopback, en otro número.
        public const int PuertoPorDefecto = 27179;
        public static readonly int Port;

        // Qué contar sobre el puerto cuando arranque el puente. Se decide aquí y se
        // escribe en Start(): un inicializador estático no es sitio para tocar el log.
        private static readonly string _avisoPuerto;

        static McpServer()
        {
            string crudo = null;
            try { crudo = Environment.GetEnvironmentVariable("ARCMAP_BRIDGE_PORT"); }
            catch { /* si el entorno no se deja leer, el puerto por defecto sirve */ }

            if (crudo == null || crudo.Trim().Length == 0)
            {
                Port = PuertoPorDefecto;
                return;
            }

            int puerto;
            // Se rechazan los privilegiados (<1024): ArcMap corre como usuario normal y
            // el bind fallaría con un error que no explicaría por qué.
            if (!int.TryParse(crudo.Trim(), out puerto) || puerto < 1024 || puerto > 65535)
            {
                Port = PuertoPorDefecto;
                _avisoPuerto = "ARCMAP_BRIDGE_PORT='" + crudo + "' no es un puerto válido"
                    + " (se admite 1024-65535). Se usa el de por defecto, " + PuertoPorDefecto + ".";
                return;
            }

            Port = puerto;
            if (puerto != PuertoPorDefecto)
            {
                _avisoPuerto = "Puerto tomado de ARCMAP_BRIDGE_PORT: " + puerto
                    + " (por defecto sería " + PuertoPorDefecto + "). El servidor MCP NO lo"
                    + " adivina: exporta la MISMA variable donde corra él, o no se encontrarán.";
            }
        }

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

        // Quién tiene el gate y desde cuándo. Sirve para que `ping` pueda decir
        // "ocupado desde hace N s con el comando X" en vez de un "busy" pelado.
        // Es la diferencia entre diagnosticar en segundos y en media hora: el
        // 2026-08-27 un handler se quedó sin volver y desde fuera era imposible
        // distinguirlo de trabajo legítimo en curso.
        private volatile string _comandoEnCurso;
        private long _inicioComandoTicks;

        // Conexiones ACEPTADAS y todavía abiertas. Hacen falta porque `_listener.Stop()`
        // cierra el socket de escucha pero NO las conexiones ya aceptadas, y una conexión
        // viva mantiene ocupado el 127.0.0.1:27179. El 2026-08-27 eso dejó el puerto
        // cogido por un ArcMap que ya había descargado su extensión ("Servidor TCP
        // detenido" en el log a las 11:49:49) mientras un handler seguía bloqueado
        // sujetando su conexión: las instancias siguientes fallaban al bindear con
        // "Solo se permite un uso de cada dirección de socket". Cerrarlas en Stop()
        // desbloquea de paso al cliente, que deja de esperar una respuesta que no llega.
        private readonly System.Collections.Generic.List<TcpClient> _conexiones =
            new System.Collections.Generic.List<TcpClient>();

        /// <summary>Segundos que lleva ocupado, o -1 si está libre.</summary>
        private double SegundosOcupado()
        {
            long ticks = Interlocked.Read(ref _inicioComandoTicks);
            if (ticks == 0)
                return -1;
            return (DateTime.UtcNow - new DateTime(ticks, DateTimeKind.Utc)).TotalSeconds;
        }

        public bool IsRunning
        {
            get { return _running; }
        }

        [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetHandleInformation(IntPtr hObject, uint dwMask, uint dwFlags);
        private const uint HANDLE_FLAG_INHERIT = 0x00000001;

        /// <summary>
        /// Marca un socket como NO heredable. En .NET Framework los sockets nacen
        /// heredables, y cualquier proceso que ArcMap lance después con herencia de
        /// handles se lleva una copia: medido el 2026-09-23, `RuntimeLocalServer.exe`
        /// (Esri, hijo de ArcMap) arrancó 10 s después del puente y se quedó con el
        /// socket de escucha. Tras «Detener», el 27179 siguió en LISTENING a nombre de
        /// ArcMap, las conexiones se colgaban sin respuesta y el siguiente «Iniciar»
        /// falló con "Solo se permite un uso de cada dirección". Es muy probablemente la
        /// raíz del puerto cogido del 2026-08-27. Nunca rompe: si falla, se anota.
        /// </summary>
        private static void NoHeredable(Socket s, string que)
        {
            try
            {
                if (!SetHandleInformation(s.Handle, HANDLE_FLAG_INHERIT, 0))
                    Log.Info("No se pudo marcar como no heredable el socket (" + que + "): error "
                             + System.Runtime.InteropServices.Marshal.GetLastWin32Error());
            }
            catch (Exception ex)
            {
                Log.Error("No se pudo marcar como no heredable el socket (" + que + ")", ex);
            }
        }

        public void Start()
        {
            if (_avisoPuerto != null)
                Log.Info(_avisoPuerto);
            _listener = new TcpListener(Bind, Port);
            // Antes de Start: el handle ya existe, y así no hay ventana en la que un hijo
            // lanzado justo entonces lo herede.
            NoHeredable(_listener.Server, "escucha");
            _listener.Start();
            _running = true;
            _acceptThread = new Thread(AcceptLoop)
            {
                IsBackground = true,
                Name = "ArcmapMcp.Accept"
            };
            _acceptThread.Start();
            Log.Info("Servidor TCP escuchando en " + Bind + ":" + Port);
        }

        public void Stop()
        {
            _running = false;
            try { _listener.Stop(); } catch { /* ya cerrado */ }

            // Cerrar el listener NO basta: una conexión ya aceptada sigue ocupando el
            // puerto, y si su handler está bloqueado nadie la va a cerrar. Sin esto, un
            // ArcMap que ya descargó la extensión deja el 27179 cogido y ninguna instancia
            // nueva puede levantar el puente (incidente del 2026-08-27).
            TcpClient[] abiertas;
            lock (_conexiones)
            {
                abiertas = _conexiones.ToArray();
                _conexiones.Clear();
            }
            foreach (TcpClient c in abiertas)
                try { c.Close(); } catch { /* el handler ya la cerró */ }
            if (abiertas.Length > 0)
                Log.Info("Cerradas " + abiertas.Length + " conexión(es) en vuelo para liberar el puerto "
                         + Port + ". Si alguna tenía un comando a medias, su trabajo puede seguir"
                         + " corriendo dentro de ArcMap.");

            // Un runner arcpy vivo impide que ArcMap acabe de salir, y ahí es donde nace
            // el zombi: proceso vivo, sin ventana, con el puerto cogido. Si el puente se
            // para, nadie va a leer ya el resultado de ese runner, así que se corta.
            try
            {
                int muertos = Handlers.PythonHandlers.MatarRunnersVivos();
                if (muertos > 0)
                    Log.Info("Terminados " + muertos + " runner(s) arcpy que seguían vivos.");
            }
            catch (Exception ex)
            {
                Log.Error("No se pudieron terminar los runners vivos", ex);
            }

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
                // Las conexiones aceptadas también: un runner python.exe lanzado mientras
                // se atiende un comando heredaría este socket, y cerrar la conexión desde
                // aquí no llegaría al cliente hasta que el runner muriese.
                NoHeredable(client.Client, "conexión");
                ThreadPool.QueueUserWorkItem(delegate { HandleClient(client); });
            }
        }

        private void HandleClient(TcpClient client)
        {
            lock (_conexiones) _conexiones.Add(client);
            try
            {
                AtenderCliente(client);
            }
            finally
            {
                lock (_conexiones) _conexiones.Remove(client);
            }
        }

        private void AtenderCliente(TcpClient client)
        {
            using (client)
            {
                NetworkStream stream = client.GetStream();
                JObject response;
                try
                {
                    string motivo;
                    JObject request = ReadRequest(stream, out motivo);
                    if (request == null)
                    {
                        response = Protocol.Error(motivo);
                    }
                    else
                    {
                        // Los dos datos se leen ANTES de tocar el gate, a propósito: entre
                        // el Wait que lo toma y el Release o el finally que lo suelta no
                        // puede correr nada más. Una excepción en ese hueco dejaría el gate
                        // cogido para siempre y el puente diciendo "busy" hasta reiniciar.
                        string desbordado = StaDispatcher.TrabajoDesbordado();
                        bool esPing = "ping".Equals((string)request["type"]);

                        if (!_gate.Wait(0))
                        {
                            // OCUPADO. `ping` NO se queda aquí: un chequeo de salud que
                            // solo contesta cuando todo va bien no sirve para nada, y es
                            // justo cuando hay algo atascado cuando hace falta saber QUÉ.
                            // Se responde sin tocar el STA (que puede ser lo atascado),
                            // así que esta rama contesta siempre y al instante.
                            double seg = SegundosOcupado();
                            string cual = _comandoEnCurso ?? "desconocido";
                            if (esPing)
                            {
                                var estado = new JObject
                                {
                                    { "estado", "ocupado" },
                                    { "comando_en_curso", cual },
                                    { "ocupado_desde_s", Math.Round(seg, 1) },
                                    { "nota", "El puente está VIVO; ArcMap está atendiendo '" + cual
                                              + "' desde hace " + seg.ToString("0") + " s. Si es un geoproceso"
                                              + " pesado sigue corriendo: espera, no relances. Si lleva mucho"
                                              + " más de lo razonable, mira el log del add-in." },
                                };
                                if (desbordado != null)
                                    estado["trabajo_desbordado"] = desbordado;
                                response = Protocol.Result(estado);
                            }
                            else
                            {
                                response = Protocol.Error("busy: ArcMap lleva " + seg.ToString("0")
                                    + " s atendiendo '" + cual + "'; reintenta en unos segundos"
                                    + " (llama a ping para ver el estado sin esperar)"
                                    + (desbordado == null ? "" : ". Además sigue corriendo dentro de ArcMap "
                                       + "trabajo que ya venció por timeout: " + desbordado));
                            }
                        }
                        // Gate libre, pero ArcMap puede NO estarlo: una operación que venció
                        // por timeout con el handler ya empezado sigue ocupando el hilo STA.
                        // Un `ping` que fuera al STA en ese estado se quedaría esperando sus
                        // 60 s para acabar en un timeout — justo cuando más falta hace saber
                        // qué pasa. Se contesta aquí, sin tocar el STA, con la misma lógica
                        // que la rama de ocupado. NO se rechaza ningún otro comando: la
                        // decisión de no encolar es del gate y no cambia.
                        else if (desbordado != null && esPing)
                        {
                            _gate.Release();
                            response = Protocol.Result(new JObject
                            {
                                { "estado", "ocupado_tras_timeout" },
                                { "trabajo_desbordado", desbordado },
                                { "nota", "El puente está VIVO y acepta peticiones, pero ArcMap sigue"
                                          + " ocupado con trabajo que YA venció su timeout (" + desbordado
                                          + "): no se pudo abortar porque había empezado. Lo que mandes"
                                          + " ahora se encolará detrás. Espera a que el log diga que ha"
                                          + " terminado; si no termina nunca, reinicia ArcMap." },
                            });
                        }
                        else
                        {
                            _comandoEnCurso = (string)request["type"] ?? "?";
                            Interlocked.Exchange(ref _inicioComandoTicks, DateTime.UtcNow.Ticks);
                            try
                            {
                                response = Dispatch(request);
                            }
                            finally
                            {
                                Interlocked.Exchange(ref _inicioComandoTicks, 0);
                                _comandoEnCurso = null;
                                _gate.Release();
                            }
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
        /// el parse cuando el buffer PUEDE estar completo.
        ///
        /// Devuelve null y deja en `motivo` por qué: un request de 3 MB y uno cortado a
        /// medias no son el mismo problema y no pueden dar el mismo "Request ilegible",
        /// que manda a mirar el JSON cuando lo que pasa es que sobra tamaño.
        /// </summary>
        private static JObject ReadRequest(NetworkStream stream, out string motivo)
        {
            motivo = null;   // solo se rellena en los caminos que devuelven null
            stream.ReadTimeout = ReadTimeoutMs;
            var buf = new MemoryStream();
            var chunk = new byte[8192];
            while (true)
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

                if (buf.Length + n > MaxRequestBytes)
                {
                    motivo = "Request DEMASIADO GRANDE: pasa de " + (MaxRequestBytes / 1024)
                        + " KB y se corta sin leerlo. Los comandos del puente son pequeños; si estás "
                        + "mandando datos a granel dentro de 'code', escríbelos a un fichero y que el "
                        + "código los lea de ahí.";
                    return null;
                }
                buf.Write(chunk, 0, n);

                // Solo se intenta parsear cuando el último byte no-blanco es '}'. Antes se
                // copiaba, decodificaba y reparseaba el buffer ENTERO tras cada chunk: coste
                // cuadrático en el tamaño del request. Medido en aislado sobre .NET
                // Framework el 2026-09-20 con un request de 76 bytes: 76 parses sin puerta
                // y 3 con ella leyendo byte a byte, 11 y 1 leyendo de 7 en 7, y el mismo
                // resultado en todos los casos.
                // La puerta es un FILTRO, no una decisión: una '}' de dentro de una cadena
                // pasa, el parse falla y se sigue leyendo (1 de los 3 intentos de la medida).
                if (!AcabaEnLlave(buf))
                    continue;
                try
                {
                    return JObject.Parse(Encoding.UTF8.GetString(buf.ToArray()));
                }
                catch (JsonReaderException)
                {
                    // Una '}' que era parte de una cadena, no el cierre: seguir leyendo.
                }
            }
            motivo = buf.Length == 0
                ? "No llegó ni un byte antes del timeout de lectura (" + (ReadTimeoutMs / 1000)
                  + " s): el cliente abrió la conexión y no mandó el request."
                : "Request ilegible (no llegó un objeto JSON válido; " + buf.Length + " bytes recibidos)";
            return null;
        }

        /// <summary>¿El último byte no-blanco del buffer es '}'? Sin copiar el buffer:
        /// se mira el array de respaldo del MemoryStream tal cual.</summary>
        private static bool AcabaEnLlave(MemoryStream buf)
        {
            byte[] datos = buf.GetBuffer();
            for (int i = (int)buf.Length - 1; i >= 0; i--)
            {
                byte b = datos[i];
                if (b == (byte)' ' || b == (byte)'\t' || b == (byte)'\r' || b == (byte)'\n')
                    continue;
                return b == (byte)'}';
            }
            return false;
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
                { "set_legend_item",       Handlers.LayoutHandlers.SetLegendItem },
                { "set_labels",            Handlers.LabelHandlers.SetLabels },
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
                { "add_group",                  Handlers.LayerHandlers.AddGroup },
                { "remove_layer",               Handlers.LayerHandlers.RemoveLayer },
                { "apply_symbology_from_layer", Handlers.LayerHandlers.ApplySymbologyFromLayer },
                { "set_graduated_symbology",    Handlers.LayerHandlers.SetGraduatedSymbology },
                { "set_raster_symbology",       Handlers.RasterSymbologyHandlers.SetRasterSymbology },
                { "set_unique_values_symbology", Handlers.UniqueValuesHandlers.SetUniqueValuesSymbology },
                { "set_single_symbology",       Handlers.SymbolHandlers.SetSingleSymbology },
                { "edit_symbol",                Handlers.SymbolHandlers.EditSymbol },
                { "get_bookmarks",              Handlers.BookmarkHandlers.GetBookmarks },
                { "add_bookmark",               Handlers.BookmarkHandlers.AddBookmark },
                { "remove_bookmark",            Handlers.BookmarkHandlers.RemoveBookmark },
                { "goto_bookmark",              Handlers.BookmarkHandlers.GotoBookmark },
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

        /// <summary>Cuántos comandos entiende el puente. Lo consume la ficha "Acerca de"
        /// para no tener que mantener el número a mano (estuvo en 48 con 57 tools reales).</summary>
        public static int NumComandos
        {
            get { return _handlers.Count + _handlersFondo.Count; }
        }

        // Exports a disco y GP nativos pueden tardar mucho más de 60s (layouts densos,
        // dpi alto, geoprocesos): timeout STA amplio, filosofía del ARCMAP_GP_TIMEOUT.
        // calculate_geometry está aquí porque es un GP nativo como run_geoprocessing:
        // AddGeometryAttributes sobre una capa de decenas de miles de entidades se pasa
        // de 60 s con facilidad, y el timeout cortaba con el cálculo ya escribiendo
        // campos en la fuente real.
        private static readonly System.Collections.Generic.HashSet<string> _comandosLargos =
            new System.Collections.Generic.HashSet<string> { "export_pdf", "export_jpg", "export_view_png", "run_geoprocessing", "calculate_geometry" };
        private static readonly TimeSpan LongHandlerTimeout = TimeSpan.FromSeconds(1800);

        // save_mxd y save_mxd_as acaban en SaveDocument/SaveAsDocument, la MISMA operación
        // que PythonHandlers cronometra con 600 s (SnapshotTimeout) porque un mxd con
        // decenas de capas y rásters pesados se va a varios minutos. Con los 60 s de por
        // defecto, guardar un documento grande devolvía un timeout mientras ArcMap seguía
        // escribiéndolo: el peor error posible, el que dice que falló algo que sí pasó.
        private static readonly System.Collections.Generic.HashSet<string> _comandosGuardado =
            new System.Collections.Generic.HashSet<string> { "save_mxd", "save_mxd_as" };
        private static readonly TimeSpan GuardadoTimeout = TimeSpan.FromSeconds(600);

        // Techo de SEGURIDAD para los handlers de fondo. No compite con sus timeouts
        // internos: el peor caso legítimo es snapshot (600 s) + subprocess (1800 s) =
        // 2400 s, así que esto va deliberadamente por encima. Si salta, no es que la
        // operación fuera larga: es que el handler no volvió, y sin este techo eso
        // deja el puente muerto sin recuperación (incidente del 2026-08-27).
        private static readonly TimeSpan FondoTimeout = TimeSpan.FromSeconds(2700);

        private static JObject Dispatch(JObject request)
        {
            string type = (string)request["type"];
            JObject parameters = request["params"] as JObject ?? new JObject();
            Log.Info("Comando recibido: " + type);
            Estadisticas.RegistrarComando(type);

            Func<JObject, JObject> handler;
            if (type != null && _handlersFondo.TryGetValue(type, out handler))
            {
                // Out-of-process: el handler gestiona su propio timeout de subprocess
                // y sus pasos STA internos. PERO eso no basta: el 2026-08-27 un
                // handler de fondo NO VOLVIÓ, el `finally` que suelta el gate nunca
                // llegó, y el puente quedó inservible hasta matar ArcMap por PID.
                // De ahí este techo exterior: si el handler se pasa de largo, se
                // devuelve un error y el gate se libera. El trabajo huérfano puede
                // seguir vivo por dentro (no se puede abortar un thread ajeno sin
                // riesgo), pero el puente vuelve a atender, que es lo que importa.
                Func<JObject, JObject> handlerLocal = handler;
                JObject parametrosLocal = parameters;
                JObject resultado = null;
                Exception fallo = null;
                var terminado = new ManualResetEventSlim(false);

                // Quién libera el evento: el ÚLTIMO de los dos que acabe de usarlo. No se
                // puede liberar sin más al volver de Wait, porque con timeout el handler
                // sigue vivo y llamará a Set() sobre un objeto ya liberado — y liberarlo
                // MIENTRAS otro thread está dentro de Set() es la carrera clásica de
                // ManualResetEventSlim, que el try/catch de ahí no cubre. Cada lado
                // decrementa cuando ya ha terminado de tocarlo, y el que llega a cero
                // libera. Hasta ahora sencillamente no se liberaba nunca.
                int usuarios = 2;
                Action soltar = delegate
                {
                    if (Interlocked.Decrement(ref usuarios) == 0)
                    {
                        try { terminado.Dispose(); } catch { }
                    }
                };

                ThreadPool.QueueUserWorkItem(delegate
                {
                    try { resultado = handlerLocal(parametrosLocal); }
                    catch (Exception ex) { fallo = ex; }
                    finally
                    {
                        try { terminado.Set(); } catch { /* ya liberado */ }
                        soltar();
                    }
                });

                bool completo;
                try { completo = terminado.Wait(FondoTimeout); }
                finally { soltar(); }

                if (!completo)
                {
                    Log.Error("Handler de fondo '" + type + "' superó el techo de "
                              + FondoTimeout.TotalSeconds + " s y NO volvió. El gate se libera "
                              + "para no dejar el puente inservible; puede quedar trabajo huérfano.");
                    return Protocol.Error("El comando '" + type + "' superó el techo de seguridad de "
                        + FondoTimeout.TotalSeconds + " s sin devolver nada. El puente sigue disponible, "
                        + "pero puede haber trabajo huérfano dentro de ArcMap: revisa el log del add-in "
                        + "y, si ArcMap se queda raro, reinícialo. Causa habitual: abrir un .mxd cuyas "
                        + "fuentes de datos no responden (usa describe_mxd antes de abrir a ciegas).");
                }
                if (fallo != null)
                {
                    Log.Error("Handler de fondo lanzó excepción", fallo);
                    return Protocol.Error(fallo.Message, fallo);
                }
                return resultado;
            }
            if (type == null || !_handlers.TryGetValue(type, out handler))
            {
                var implementados = new System.Collections.Generic.List<string>(_handlers.Keys);
                implementados.AddRange(_handlersFondo.Keys);
                return Protocol.Error(
                    "Comando desconocido: '" + type + "'. Implementados: "
                    + string.Join(", ", implementados));
            }
            TimeSpan timeout = _comandosLargos.Contains(type)
                ? LongHandlerTimeout
                : _comandosGuardado.Contains(type) ? GuardadoTimeout : HandlerTimeout;
            return StaDispatcher.Invoke(delegate { return handler(parameters); }, timeout, type);
        }
    }
}
