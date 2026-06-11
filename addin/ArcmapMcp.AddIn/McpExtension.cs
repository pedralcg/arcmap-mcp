using System.Threading;
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
