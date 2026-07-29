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
            Log.Info("Extensión cargada; dispatcher STA capturado (thread "
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
                                + "¿Abrir el repositorio para descargarla?",
                                "arcmap-mcp · Actualización disponible",
                                MessageBoxButtons.YesNo, MessageBoxIcon.Information);
                            if (r == DialogResult.Yes)
                                Process.Start(new ProcessStartInfo
                                {
                                    FileName = Actualizaciones.RepoUrl,
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
            catch (System.Exception ex)
            {
                Log.Error("No se pudo arrancar el servidor", ex);
                return ex.Message + " (¿puerto " + McpServer.Port + " ocupado por otra instancia?)";
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
