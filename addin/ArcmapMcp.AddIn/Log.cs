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
            WriteLine("ERROR", ex == null ? msg : msg + " :: " + ex);
        }

        private static void WriteLine(string level, string msg)
        {
            try
            {
                lock (_lock)
                {
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
    }
}
