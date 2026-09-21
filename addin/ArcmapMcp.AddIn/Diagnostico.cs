using System;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Texto de diagnóstico NO SENSIBLE, reutilizado por el botón Estado y por el
    /// reporte de problemas. Deliberadamente NO incluye el título del documento ni
    /// nombres de capas ni rutas de datos: eso son posibles datos de cliente
    /// (restricción L3) y el reporte sale de la máquina. Solo entorno, versiones y
    /// contadores.
    ///
    /// La única ruta que aparece es la del intérprete Python de ArcGIS, que hace falta
    /// para diagnosticar la mitad de los fallos de execute_arcpy — y que puede llevar el
    /// nombre del usuario si viene de ARCMAP_PYTHON27. Por eso se enmascara el perfil
    /// (%USERPROFILE%) antes de mostrarla, y el texto del formulario ya no promete "sin
    /// rutas" a secas: dice qué sale y enseña el bloque antes de enviar nada.
    /// </summary>
    internal static class Diagnostico
    {
        /// <summary>Versión del add-in leída del ensamblado (patrón usado en toda la UI).</summary>
        public static string VersionAddin()
        {
            return typeof(Diagnostico).Assembly.GetName().Version.ToString(3);
        }

        /// <summary>Bloque de diagnóstico sin datos de cliente. Cada dato va en su
        /// propio try: un entorno raro nunca debe impedir generar el reporte.</summary>
        public static string TextoNoSensible(bool puenteActivo)
        {
            return
                "arcmap-mcp:   " + VersionAddin() + " (.NET)\r\n"
                + "ArcMap:       " + VersionArcMap() + "\r\n"
                + "SO:           " + DescribirSO() + "\r\n"
                + ".NET CLR:     " + Environment.Version + "\r\n"
                + "Puente:       " + (puenteActivo ? "ACTIVO" : "PARADO")
                    + " (127.0.0.1:" + McpServer.Port + ")\r\n"
                + "Actualización:" + " " + DescribirActualizacion() + "\r\n"
                + "Sesión desde: " + Estadisticas.Inicio.ToString("yyyy-MM-dd HH:mm:ss") + "\r\n"
                + "Peticiones:   " + Estadisticas.Peticiones + "\r\n"
                + "Errores:      " + Estadisticas.Errores + "\r\n"
                + "Último cmd:   " + Estadisticas.UltimoComando + "\r\n"
                + "Python arcpy: " + SinPerfilDeUsuario(DescribirPython27());
        }

        /// <summary>
        /// Sustituye la carpeta de perfil del usuario por %USERPROFILE% en un texto que va
        /// a salir de la máquina. No es anonimización fuerte —una ruta fuera del perfil
        /// puede seguir llevando un nombre—, y por eso el formulario enseña el bloque
        /// entero antes de enviarlo: lo que promete el texto y lo que contiene coinciden.
        /// </summary>
        public static string SinPerfilDeUsuario(string texto)
        {
            if (string.IsNullOrEmpty(texto)) return texto;
            try
            {
                string perfil = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                if (string.IsNullOrEmpty(perfil)) return texto;
                // Regex y no string.Replace porque este comparaba con distinción de
                // mayúsculas, y una ruta del registro rara vez viene con el mismo casing.
                return Regex.Replace(texto, Regex.Escape(perfil), "%USERPROFILE%",
                                     RegexOptions.IgnoreCase);
            }
            catch { return texto; }
        }

        /// <summary>Estado de actualización en una línea, para colar en Estado/reporte.</summary>
        public static string DescribirActualizacion()
        {
            if (Actualizaciones.HayNueva)
                return "hay v" + Actualizaciones.UltimaDisponible + " disponible";
            if (!string.IsNullOrEmpty(Actualizaciones.UltimaDisponible))
                return "al día (última comprobada v" + Actualizaciones.UltimaDisponible + ")";
            return "sin comprobar todavía";
        }

        /// <summary>Versión real de ArcGIS Desktop desde el registro (best-effort).
        /// Evita arrastrar ESRI.ArcGIS.Version solo para esto.</summary>
        public static string VersionArcMap()
        {
            foreach (string ruta in new[]
            {
                @"SOFTWARE\Wow6432Node\ESRI\ArcGIS",
                @"SOFTWARE\ESRI\ArcGIS"
            })
            {
                try
                {
                    using (RegistryKey k = Registry.LocalMachine.OpenSubKey(ruta))
                    {
                        if (k == null) continue;
                        object v = k.GetValue("RealVersion") ?? k.GetValue("Version");
                        if (v != null) return v.ToString();
                    }
                }
                catch { }
            }
            return "10.x (no detectada en registro)";
        }

        private static string DescribirSO()
        {
            try
            {
                return Environment.OSVersion.VersionString
                       + (Environment.Is64BitOperatingSystem ? " (x64)" : " (x86)");
            }
            catch { return "(desconocido)"; }
        }

        /// <summary>El MISMO buscador que usa el runner (Python27), no una copia del
        /// criterio: aquí había una ruta 10.5 escrita a mano que decía "NO encontrado" en
        /// máquinas con 10.6-10.8 donde el Python estaba perfectamente instalado.
        /// Sin él no hay execute_arcpy ni DDP.</summary>
        public static string DescribirPython27()
        {
            try { return Python27.Describir(); }
            catch { return "(no comprobable)"; }
        }
    }
}
