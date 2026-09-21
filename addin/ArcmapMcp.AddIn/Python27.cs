using System;
using System.Collections.Generic;
using System.IO;
using Microsoft.Win32;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Localiza el python.exe 2.7 de ArcGIS: el intérprete con el que el runner arcpy
    /// corre fuera del proceso (execute_arcpy, las 3 Data Driven Pages y las 5
    /// ambientales).
    ///
    /// Existe porque la ruta estaba ESCRITA A MANO — C:\Python27\ArcGIS10.5\python.exe —
    /// en tres sitios a la vez (PythonHandlers, Diagnostico y el botón Estado), mientras
    /// el instalador (install.ps1, Find-Python27) y el servidor Python sí la buscaban. En
    /// 10.6, 10.7 y 10.8 el add-in cargaba perfectamente y execute_arcpy fallaba SIEMPRE
    /// salvo que el usuario definiera ARCMAP_PYTHON27 a mano, sin que nada dijera por qué.
    ///
    /// Orden de búsqueda, el mismo criterio que install.ps1:
    ///   1. ARCMAP_PYTHON27, si apunta a un fichero que existe (manda sobre lo demás).
    ///   2. Registro de ArcGIS: HKLM\SOFTWARE\WOW6432Node\ESRI\Python10.x → PythonDir,
    ///      empezando por la versión de ESTA sesión de ArcMap.
    ///   3. Registro de Python: HKLM\...\Python\PythonCore\2.7\InstallPath (valor por
    ///      defecto), que es el que mira install.ps1.
    ///   4. C:\Python27\ArcGIS10.*\python.exe, prefiriendo también la de la sesión.
    ///
    /// El resultado se cachea: esto se llama en cada execute_arcpy y son varias lecturas
    /// de registro. Solo se cachea el ACIERTO — si no hay Python, el usuario puede
    /// instalarlo o definir la variable sin cerrar ArcMap, y volver a buscar cuesta
    /// milisegundos en un camino que además ya ha fallado.
    /// </summary>
    internal static class Python27
    {
        private static readonly object _lock = new object();
        private static string _cache;
        private static string _trazaCache;

        /// <summary>Ruta del python.exe, o null si no hay ninguno. `traza` dice dónde se
        /// ha mirado y con qué resultado: es lo que convierte "no funciona" en un
        /// diagnóstico.</summary>
        public static string Buscar(out string traza)
        {
            lock (_lock)
            {
                if (_cache != null && File.Exists(_cache))
                {
                    traza = _trazaCache;
                    return _cache;
                }

                var sitios = new List<string>();
                string hallado = BuscarSinCache(sitios);
                traza = string.Join(" | ", sitios.ToArray());
                if (hallado != null)
                {
                    _cache = hallado;
                    _trazaCache = traza;
                }
                return hallado;
            }
        }

        /// <summary>Ruta del python.exe o excepción. El mensaje dice DÓNDE se ha
        /// buscado: sin eso, "no se encuentra el Python 2.7" no se puede accionar.</summary>
        public static string Exe()
        {
            string traza;
            string exe = Buscar(out traza);
            if (exe != null)
                return exe;
            throw new InvalidOperationException(
                "No se encuentra el Python 2.7 de ArcGIS, que es quien ejecuta arcpy fuera del "
                + "proceso (execute_arcpy, Data Driven Pages y las tools ambientales). "
                + "Buscado en: " + traza + ". Define ARCMAP_PYTHON27 con la ruta completa a "
                + "python.exe si lo tienes en otro sitio.");
        }

        /// <summary>Una línea para la UI (botón Estado y reporte de problemas).</summary>
        public static string Describir()
        {
            string traza;
            string exe = Buscar(out traza);
            return exe ?? ("NO encontrado. Buscado en: " + traza);
        }

        private static string BuscarSinCache(List<string> sitios)
        {
            string env = null;
            try { env = Environment.GetEnvironmentVariable("ARCMAP_PYTHON27"); }
            catch { /* entorno ilegible: se sigue por el registro */ }

            if (!string.IsNullOrEmpty(env))
            {
                if (File.Exists(env))
                {
                    sitios.Add("ARCMAP_PYTHON27=" + env + " (OK)");
                    return env;
                }
                // Definida y apuntando a nada: se anota y se SIGUE buscando, igual que
                // install.ps1. Una variable que quedó de una instalación anterior no debe
                // tapar el Python que sí está instalado.
                sitios.Add("ARCMAP_PYTHON27=" + env + " (no existe)");
            }
            else
            {
                sitios.Add("ARCMAP_PYTHON27 (sin definir)");
            }

            string[] versiones = VersionesCandidatas();

            foreach (string v in versiones)
            {
                foreach (string raiz in new[] { @"SOFTWARE\Wow6432Node\ESRI\Python", @"SOFTWARE\ESRI\Python" })
                {
                    string clave = raiz + v;
                    string dir = LeerValor(clave, "PythonDir");
                    if (string.IsNullOrEmpty(dir)) continue;
                    // PythonDir no tiene una forma única: en esta máquina (ArcMap 10.5)
                    // vale "C:\Python27\", el PADRE del home, y en otras instalaciones es
                    // el home entero. Se prueban las dos formas antes de descartar la clave.
                    string exe = PrimeroQueExista(sitios, "HKLM\\" + clave + "\\PythonDir",
                        Path.Combine(dir, "python.exe"),
                        Path.Combine(Path.Combine(dir, "ArcGIS" + v), "python.exe"));
                    if (exe != null) return exe;
                }
            }

            foreach (string clave in new[]
            {
                @"SOFTWARE\Wow6432Node\Python\PythonCore\2.7\InstallPath",
                @"SOFTWARE\Python\PythonCore\2.7\InstallPath"
            })
            {
                string dir = LeerValor(clave, null);   // null = valor por defecto de la clave
                if (string.IsNullOrEmpty(dir)) continue;
                string exe = PrimeroQueExista(sitios, "HKLM\\" + clave,
                    Path.Combine(dir, "python.exe"));
                if (exe != null) return exe;
            }

            return BuscarEnPython27(sitios, versiones);
        }

        /// <summary>Último recurso: la instalación estándar en disco. Se prueban primero
        /// las versiones candidatas (la de la sesión la primera) y luego cualquier otra
        /// ArcGIS10.* de mayor a menor.</summary>
        private static string BuscarEnPython27(List<string> sitios, string[] versiones)
        {
            const string raizPy = @"C:\Python27";
            try
            {
                if (!Directory.Exists(raizPy))
                {
                    sitios.Add(raizPy + " (no existe)");
                    return null;
                }

                var candidatos = new List<string>();
                foreach (string v in versiones)
                    candidatos.Add(Path.Combine(raizPy, "ArcGIS" + v));
                string[] subs = Directory.GetDirectories(raizPy, "ArcGIS10.*");
                Array.Sort(subs, delegate (string a, string b) { return string.CompareOrdinal(b, a); });
                candidatos.AddRange(subs);

                foreach (string carpeta in candidatos)
                {
                    string exe = Path.Combine(carpeta, "python.exe");
                    if (File.Exists(exe))
                    {
                        sitios.Add(raizPy + @"\ArcGIS10.* → " + exe + " (OK)");
                        return exe;
                    }
                }
                sitios.Add(raizPy + @"\ArcGIS10.*\python.exe (ninguno)");
            }
            catch (Exception ex)
            {
                sitios.Add(raizPy + " (no legible: " + ex.Message + ")");
            }
            return null;
        }

        /// <summary>Versiones de ArcGIS a probar, la de ESTA sesión la primera: con dos
        /// instalaciones conviviendo, el arcpy que toca es el de la que está abierta.</summary>
        private static string[] VersionesCandidatas()
        {
            var v = new List<string>();
            string sesion = null;
            try { sesion = MxdVersion.VersionArcMapSesion(); }
            catch { /* fuera de ArcMap no hay sesión que mirar */ }
            if (!string.IsNullOrEmpty(sesion))
                v.Add(sesion);
            foreach (string otra in new[] { "10.8", "10.7", "10.6", "10.5", "10.4" })
                if (!v.Contains(otra))
                    v.Add(otra);
            return v.ToArray();
        }

        /// <summary>Primer candidato que exista de verdad; anota en la traza el sitio
        /// consultado tanto si acierta como si no.</summary>
        private static string PrimeroQueExista(List<string> sitios, string sitio, params string[] candidatos)
        {
            foreach (string exe in candidatos)
            {
                try
                {
                    if (File.Exists(exe))
                    {
                        sitios.Add(sitio + " → " + exe + " (OK)");
                        return exe;
                    }
                }
                catch { /* ruta imposible en el registro: se prueba la siguiente */ }
            }
            sitios.Add(sitio + " (sin python.exe)");
            return null;
        }

        private static string LeerValor(string clave, string nombre)
        {
            try
            {
                using (RegistryKey k = Registry.LocalMachine.OpenSubKey(clave))
                {
                    if (k == null) return null;
                    object v = k.GetValue(nombre);
                    return v == null ? null : v.ToString();
                }
            }
            catch { return null; }
        }
    }
}
