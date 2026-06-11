using System;
using System.Threading.Tasks;
using System.Windows.Threading;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Marshal de peticiones al hilo STA de ArcMap vía WPF Dispatcher.
    /// ArcObjects SOLO puede tocarse desde ese hilo.
    /// </summary>
    internal static class StaDispatcher
    {
        private static Dispatcher _ui;

        /// <summary>Llamar UNA vez desde el hilo UI (OnStartup de la extensión).</summary>
        public static void CaptureCurrent()
        {
            _ui = Dispatcher.CurrentDispatcher;
        }

        public static bool IsCaptured
        {
            get { return _ui != null; }
        }

        /// <summary>
        /// Ejecuta el handler en el hilo STA con timeout. Si expira, devuelve un
        /// error JSON SIN matar el listener: si la operación ya empezó en ArcMap,
        /// seguirá corriendo allí; si aún estaba encolada, se aborta.
        /// </summary>
        public static JObject Invoke(Func<JObject> handler, TimeSpan timeout)
        {
            if (_ui == null)
                return Protocol.Error("Dispatcher STA no capturado (¿la extensión no llegó a cargar?)");

            DispatcherOperation<JObject> op = _ui.InvokeAsync(delegate
            {
                // Ninguna excepción escapa al message pump de ArcMap — eso
                // sería un crash de la aplicación entera.
                try
                {
                    return handler();
                }
                catch (Exception ex)
                {
                    Log.Error("Handler lanzó excepción en el hilo STA", ex);
                    // Sobre de error del protocolo: error = mensaje + traceback.
                    return Protocol.Error(ex.Message, ex);
                }
            });

            Task<JObject> task = op.Task;
            if (!task.Wait(timeout))
            {
                op.Abort();
                Log.Error("Timeout de " + timeout.TotalSeconds + "s esperando al hilo STA");
                return Protocol.Error(
                    "Timeout (" + timeout.TotalSeconds + "s) esperando al hilo de ArcMap "
                    + "(¿geoproceso largo, dibujado WMS o diálogo modal abierto?)");
            }
            return task.Result;
        }
    }
}
