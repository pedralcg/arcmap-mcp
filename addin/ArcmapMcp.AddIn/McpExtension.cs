using System.Diagnostics;
using System.Threading;
using System.Windows.Forms;
using ESRI.ArcGIS.Desktop.AddIns;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Extensión del add-in (autoLoad). Captura el Dispatcher del hilo UI/STA en
    /// OnStartup (corre en ese hilo) y gestiona el ciclo de vida del servidor TCP.
    /// </summary>
    public class McpExtension : Extension
    {
        private static McpExtension _instance;

        // ArcMap instancia la extensión VARIAS veces por arranque (3-4 en el log del
        // 27-jul). Con el servidor en un campo de instancia, las cargas 2..N veían su
        // propio _server a null, intentaban bindear el 27179 y morían con
        // SocketException: el log se llenaba de "Autoarranque activo pero el puente no
        // arrancó", que era FALSO — la primera carga lo había levantado bien.
        // El servidor es un recurso del PROCESO, así que su estado va en estático.
        private static McpServer _server;

        // Quién levantó el puente: solo esa carga puede pararlo en su OnShutdown, para
        // que la descarga de una instancia espuria no tumbe el puente de la buena.
        private static McpExtension _duenoServidor;

        // La comprobación de versión también es del proceso: 4 cargas = 4 llamadas a la
        // API de GitHub por arranque, que además tiene rate-limit.
        private static bool _actualizacionComprobada;

        public static McpExtension Instance
        {
            get { return _instance; }
        }

        public bool IsRunning
        {
            get { return _server != null && _server.IsRunning; }
        }

        protected override void OnStartup()
        {
            _instance = this;
            // Capturar el dispatcher AQUÍ (hilo UI), jamás desde el thread del
            // listener: es la única vía segura hacia ArcObjects.
            StaDispatcher.CaptureCurrent();
            // El PID separa en el log los arranques de ArcMaps distintos: sin él, dos
            // ArcMap abiertos a la vez eran indistinguibles de uno que carga la
            // extensión varias veces.
            Process yo = Process.GetCurrentProcess();
            Log.Info("Extensión cargada en " + yo.ProcessName + ".exe PID " + yo.Id
                     + "; dispatcher STA capturado (thread "
                     + Thread.CurrentThread.ManagedThreadId + ", "
                     + Thread.CurrentThread.GetApartmentState() + ")");

            // Guard de instancia única: si otra carga de la extensión ya levantó el
            // puente en este proceso, no hay nada que hacer y desde luego nada que
            // reportar como error.
            if (IsRunning)
            {
                Log.Info("El puente ya estaba levantado por una carga anterior de la extensión "
                         + "en este proceso: no se reintenta (ArcMap carga la extensión varias veces).");
                return;
            }

            bool auto = Ajustes.Autoarranque;
            Log.Info("Preferencia de autoarranque leida: " + (auto ? "SI" : "NO"));
            if (auto)
            {
                // Nunca romper el arranque de ArcMap por el puente: si el puerto está
                // ocupado (otro ArcMap abierto), se registra y el usuario sigue trabajando.
                string error = StartServer();
                Log.Info(error == null
                    ? "Autoarranque activo: puente levantado al abrir ArcMap"
                    : "Autoarranque activo pero el puente no arrancó: " + error);
            }

            if (!_actualizacionComprobada)
            {
                _actualizacionComprobada = true;
                LanzarComprobacionActualizacion();
            }
        }

        /// <summary>
        /// Comprueba en un hilo de fondo si hay una versión nueva en GitHub. Jamás
        /// bloquea el arranque de ArcMap: sin internet o con rate-limit, no pasa nada.
        /// Si hay versión nueva (y no se ha avisado ya de ELLA), muestra un aviso una
        /// sola vez, marshalado al hilo UI (un MessageBox no puede lanzarse desde el
        /// hilo de fondo).
        /// </summary>
        private static void LanzarComprobacionActualizacion()
        {
            var hilo = new Thread(delegate ()
            {
                Actualizaciones.ComprobarEnSegundoPlano(delegate (string versionNueva)
                {
                    StaDispatcher.Post(delegate
                    {
                        try
                        {
                            DialogResult r = MessageBox.Show(
                                "Hay una versión nueva de arcmap-mcp disponible: v" + versionNueva + ".\n"
                                + "Tienes instalada la v" + Diagnostico.VersionAddin() + ".\n\n"
                                + "Para actualizar, con ArcMap cerrado:\n"
                                + " - Si lo descargaste como ZIP: baja el ZIP nuevo,\n"
                                + "   extráelo donde quieras y doble clic en INSTALAR.bat.\n"
                                + " - Si lo clonaste con git: doble clic en ACTUALIZAR.bat\n"
                                + "   dentro de C:\\mcp\\arcmap-mcp.\n\n"
                                + "No se puede instalar sin cerrar ArcMap: mientras está\n"
                                + "abierto mantiene cargado el add-in.\n\n"
                                + "¿Abrir las instrucciones de actualización?",
                                "arcmap-mcp · Actualización disponible",
                                MessageBoxButtons.YesNo, MessageBoxIcon.Information);
                            if (r == DialogResult.Yes)
                                Process.Start(new ProcessStartInfo
                                {
                                    FileName = Actualizaciones.ActualizarUrl,
                                    UseShellExecute = true
                                });
                        }
                        catch (System.Exception ex)
                        {
                            Log.Error("No se pudo mostrar el aviso de actualización", ex);
                        }
                    });
                });
            });
            hilo.IsBackground = true;
            hilo.Name = "arcmap-mcp-update-check";
            hilo.Start();
        }

        protected override void OnShutdown()
        {
            // Solo la carga que levantó el puente lo para. Si ArcMap descarga una de las
            // instancias espurias, el puente de la buena tiene que sobrevivir.
            if (_duenoServidor == null || ReferenceEquals(_duenoServidor, this))
            {
                StopServer();
                _duenoServidor = null;
            }
            else
            {
                Log.Info("Extensión descargada (carga secundaria): el puente sigue vivo, "
                         + "lo para la carga que lo arrancó.");
            }
            if (ReferenceEquals(_instance, this))
                _instance = null;
            Log.Info("Extensión descargada");
        }

        /// <summary>Arranca el servidor. Devuelve null si OK, o el mensaje de error.</summary>
        public string StartServer()
        {
            if (IsRunning)
                return null; // ya activo: para el usuario es un éxito idempotente
            try
            {
                _server = new McpServer();
                _server.Start();
                _duenoServidor = this;
                return null;
            }
            catch (System.Net.Sockets.SocketException ex)
                when (ex.SocketErrorCode == System.Net.Sockets.SocketError.AddressAlreadyInUse)
            {
                _server = null;
                // Caso previsto: WARN con el dueño del puerto, sin traza ni contador de
                // errores. Con el PID real, el consejo deja de ser "busca el <PID>".
                PuertoOcupado.Dueno dueno = PuertoOcupado.Buscar(McpServer.Port);
                string quien = dueno != null ? dueno.Describir() : "dueño desconocido";
                Log.Warn("Puerto " + McpServer.Port + " ocupado por " + quien
                         + "; el puente no arranca en este ArcMap.");
                if (dueno != null && dueno.PareceZombi)
                    return ex.Message + "\n\nEl puerto " + McpServer.Port + " lo tiene " + quien
                        + ": parece un ArcMap zombi (vivo pero sin ventana). Ciérralo con"
                        + " Stop-Process -Id " + dueno.Pid + " -Force y vuelve a arrancar el puente.";
                if (dueno != null)
                    return ex.Message + "\n\nEl puerto " + McpServer.Port + " lo tiene " + quien
                        + ". Si ese es el ArcMap que quieres controlar, no hay que hacer nada."
                        + " Si quieres usar ESTE, cierra aquel o exporta ARCMAP_BRIDGE_PORT=<otro>"
                        + " y vuelve a abrir ArcMap (y la MISMA variable donde corra el servidor MCP).";
                return ex.Message + "\n\nCasi siempre es que el puerto " + McpServer.Port
                    + " ya está cogido por otro ArcMap. Dos salidas:\n"
                    + "  · Usar otro puerto: exporta ARCMAP_BRIDGE_PORT=<otro> y vuelve a abrir"
                    + " ArcMap (y la MISMA variable donde corra el servidor MCP).\n"
                    + "  · Si el ArcMap que lo sujeta está zombi (vivo pero sin ventana),"
                    + " matarlo: Stop-Process -Id <PID> -Force.";
            }
            catch (System.Exception ex)
            {
                Log.Error("No se pudo arrancar el servidor", ex);
                // El caso habitual no es "otra instancia legítima": es un ArcMap zombi,
                // vivo y sin ventana principal, sujetando el puerto sin nada que cerrar.
                // Por eso se nombran las dos salidas, no solo la de matar el proceso.
                return ex.Message + "\n\nCasi siempre es que el puerto " + McpServer.Port
                    + " ya está cogido por otro ArcMap. Dos salidas:\n"
                    + "  · Usar otro puerto: exporta ARCMAP_BRIDGE_PORT=<otro> y vuelve a abrir"
                    + " ArcMap (y la MISMA variable donde corra el servidor MCP).\n"
                    + "  · Si el ArcMap que lo sujeta está zombi (vivo pero sin ventana),"
                    + " matarlo: Stop-Process -Id <PID> -Force.";
            }
        }

        public void StopServer()
        {
            if (_server != null && _server.IsRunning)
                _server.Stop();
            // Liberar la propiedad: si el usuario lo para desde el botón, cualquier carga
            // puede volver a arrancarlo después.
            _duenoServidor = null;
        }
    }
}
