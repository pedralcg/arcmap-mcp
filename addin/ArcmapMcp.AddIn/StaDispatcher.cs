using System;
using System.Collections.Generic;
using System.Text;
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
        ///
        /// `etiqueta` es el nombre del comando, solo para el log y para que `ping` pueda
        /// decir QUÉ es lo que sigue ocupando ArcMap. Opcional por compatibilidad: los
        /// handlers que no la pasan siguen compilando igual.
        /// </summary>
        public static JObject Invoke(Func<JObject> handler, TimeSpan timeout, string etiqueta = null)
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
                // Abort() solo puede con lo que TODAVÍA no ha empezado. Los dos casos son
                // distintos de verdad y hasta ahora se contaban igual: una operación
                // descartada no deja rastro, mientras que una que ya entró en ArcMap sigue
                // corriendo allí y el siguiente comando se encolará detrás de ella.
                if (op.Abort())
                {
                    Log.Error("Timeout de " + timeout.TotalSeconds + "s esperando al hilo STA: '"
                              + (etiqueta ?? "comando sin nombre") + "' aún estaba ENCOLADA y se ha descartado.");
                    return Protocol.Error(
                        "Timeout (" + timeout.TotalSeconds + "s) esperando al hilo de ArcMap "
                        + "(¿geoproceso largo, dibujado WMS o diálogo modal abierto?). La operación "
                        + "seguía encolada y se ha descartado: no ha llegado a tocar nada.");
                }

                Anotar(etiqueta, task, timeout);
                return Protocol.Error(
                    "Timeout (" + timeout.TotalSeconds + "s) esperando al hilo de ArcMap "
                    + "(¿geoproceso largo, dibujado WMS o diálogo modal abierto?). OJO: la operación "
                    + "YA HABÍA EMPEZADO dentro de ArcMap y SIGUE CORRIENDO allí; el puente vuelve a "
                    + "aceptar peticiones, pero lo que mandes se encolará detrás de ella. Llama a ping "
                    + "para ver si ha terminado ya.");
            }
            return task.Result;
        }

        // ------------------------------------------------------------------ //
        // Trabajo desbordado: lo que venció por timeout con el handler YA EMPEZADO.
        // ------------------------------------------------------------------ //

        private sealed class Desbordada
        {
            public string Etiqueta;
            public DateTime Desde;
        }

        private static readonly List<Desbordada> _desbordadas = new List<Desbordada>();

        /// <summary>
        /// Registra una operación que venció pero sigue viva dentro de ArcMap, y le
        /// engancha una continuación para enterarse de cuándo termina de verdad.
        ///
        /// Sin esto el estado era invisible: el gate del servidor se libera al devolver el
        /// error, y `ping` contestaba "libre" mientras ArcMap seguía atado al comando
        /// anterior. Quien preguntaba se llevaba una respuesta correcta y falsa a la vez.
        /// </summary>
        private static void Anotar(string etiqueta, Task<JObject> task, TimeSpan timeout)
        {
            var d = new Desbordada { Etiqueta = etiqueta ?? "comando sin nombre", Desde = DateTime.UtcNow };
            lock (_desbordadas) _desbordadas.Add(d);

            Log.Error("El comando '" + d.Etiqueta + "' SIGUE CORRIENDO en ArcMap tras vencer su timeout de "
                      + timeout.TotalSeconds + " s: no se ha podido abortar porque ya había empezado. El "
                      + "puente vuelve a aceptar peticiones, pero lo que llegue se encolará detrás de él "
                      + "en el hilo de ArcMap.");

            task.ContinueWith(delegate
            {
                lock (_desbordadas) _desbordadas.Remove(d);
                Log.Info("El comando desbordado '" + d.Etiqueta + "' ha TERMINADO por fin dentro de ArcMap, "
                         + (DateTime.UtcNow - d.Desde).TotalSeconds.ToString("0")
                         + " s después de su timeout. ArcMap vuelve a estar libre de verdad.");
            });
        }

        /// <summary>Qué sigue ocupando ArcMap tras haber vencido, o null si nada. Es lo
        /// que `ping` y la respuesta `busy` cuentan al otro lado.</summary>
        public static string TrabajoDesbordado()
        {
            Desbordada[] copia;
            lock (_desbordadas)
            {
                if (_desbordadas.Count == 0) return null;
                copia = _desbordadas.ToArray();
            }
            var sb = new StringBuilder();
            foreach (Desbordada d in copia)
            {
                if (sb.Length > 0) sb.Append("; ");
                sb.Append("'").Append(d.Etiqueta).Append("' desde hace ")
                  .Append((DateTime.UtcNow - d.Desde).TotalSeconds.ToString("0")).Append(" s");
            }
            return sb.ToString();
        }

        /// <summary>
        /// Encola una acción en el hilo UI para que corra DESPUÉS de que termine el
        /// evento en curso, sin bloquear. Necesario para mostrar un diálogo modal
        /// fuera del OnSelChange de un ComboBox de add-in: hacerlo dentro hace que
        /// ArcMap revierta la selección del combo al cerrarse el diálogo y vuelva a
        /// disparar el evento. Prioridad Background: se ejecuta cuando la cola de
        /// entrada (incluido cualquier re-disparo espurio del combo) ya se ha drenado.
        /// </summary>
        public static void Post(Action action)
        {
            if (action == null) return;
            if (_ui == null) { action(); return; }  // sin dispatcher: mejor síncrono que nada
            _ui.BeginInvoke(DispatcherPriority.Background, action);
        }
    }
}
