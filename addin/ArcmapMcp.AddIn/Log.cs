using System;
using System.IO;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Log a fichero: sin Visual Studio no hay debugger — el log ES el debugger.
    /// </summary>
    internal static class Log
    {
        private static readonly object _lock = new object();
        private static readonly string _path = InitPath();

        public static string PathInfo
        {
            get { return _path; }
        }

        private static string InitPath()
        {
            try
            {
                Directory.CreateDirectory(@"C:\MCP_Logs");
                return @"C:\MCP_Logs\arcmap-mcp.log";
            }
            catch
            {
                return Path.Combine(Path.GetTempPath(), "arcmap-mcp.log");
            }
        }

        public static void Info(string msg)
        {
            WriteLine("INFO ", msg);
        }

        public static void Error(string msg, Exception ex = null)
        {
            Estadisticas.RegistrarError(); // el contador que ve el botón "Estado"
            WriteLine("ERROR", ex == null ? msg : msg + " :: " + ex);
        }

        // Tope antes de rotar. El log recoge cada comando y el stdout entero de cada
        // runner arcpy, así que crecía sin techo ni rotación: un fichero de cientos de MB
        // en C:\ que nadie mira y que ya no se puede abrir para diagnosticar nada, que es
        // justo para lo que existe.
        private const long MaxBytes = 5L * 1024 * 1024;

        private static void WriteLine(string level, string msg)
        {
            try
            {
                lock (_lock)
                {
                    RotarSiHaceFalta();
                    File.AppendAllText(_path,
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff")
                        + " [" + level + "] " + msg + Environment.NewLine);
                }
            }
            catch
            {
                // El log jamás tira ArcMap.
            }
        }

        /// <summary>
        /// Una sola generación: al pasar de MaxBytes, el log actual pasa a ser `.1` y se
        /// empieza uno limpio. Dos ficheros son suficientes para diagnosticar y acotan el
        /// gasto en disco a 10 MB, que es la mitad del problema que se está arreglando.
        ///
        /// Se llama dentro del lock, con lo que dos threads de este proceso no pueden
        /// rotar a la vez. Entre PROCESOS no hay lock que valga —dos ArcMap escriben el
        /// mismo fichero—, pero eso solo significa que un Move puede fallar porque el otro
        /// lo tiene abierto: se traga y se rota en la siguiente línea. AppendAllText ya
        /// abre y cierra en cada llamada, así que el stat de aquí no es lo caro.
        /// </summary>
        private static void RotarSiHaceFalta()
        {
            try
            {
                var fi = new FileInfo(_path);
                if (!fi.Exists || fi.Length < MaxBytes) return;
                string previo = _path + ".1";
                if (File.Exists(previo)) File.Delete(previo);
                File.Move(_path, previo);
            }
            catch
            {
                // Rotar es mantenimiento: si falla, se sigue escribiendo en el de siempre.
            }
        }
    }
}
