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
        private McpServer _server;

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

            bool auto = Ajustes.Autoarranque;
            Log.Info("Preferencia de autoarranque leida: " + (auto ? "SI" : "NO"));
            if (auto)
            {
                // Nunca romper el arranque de ArcMap por el puente: si el puerto está
                // ocupado (segunda instancia), se registra y el usuario sigue trabajando.
                string error = StartServer();
                Log.Info(error == null
                    ? "Autoarranque activo: puente levantado al abrir ArcMap"
                    : "Autoarranque activo pero el puente no arrancó: " + error);
            }

            LanzarComprobacionActualizacion();
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
            StopServer();
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
        }
    }
}
