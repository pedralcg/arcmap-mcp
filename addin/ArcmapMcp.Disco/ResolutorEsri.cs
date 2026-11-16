using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using Microsoft.Win32;

namespace ArcmapMcp.Disco
{
    /// <summary>
    /// Resuelve los ensamblados ESRI que pide el modo disco cuando la versión instalada no
    /// es la que se usó para compilar.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <b>El problema.</b> El exe se compila contra una versión concreta de ArcGIS (10.5 en
    /// la release) y pide <c>ESRI.ArcGIS.System, Version=10.5.0.0</c>. En un equipo con
    /// 10.8 esa versión no está en la GAC, y el <c>.exe.config</c> del paquete solo lleva
    /// los <c>bindingRedirect</c> de quien lo empaquetó: en una build hecha en 10.5
    /// redirigen <c>9.3.0.0-10.4.0.0 → 10.5.0.0</c>, que no cubre una petición de 10.5 en
    /// un equipo con 10.8. Resultado: <c>FileNotFoundException</c> antes de tocar
    /// ArcObjects y <c>edit_mxd_lote</c> sin poder abrir ni un .mxd.
    /// </para>
    /// <para>
    /// <b>La solución.</b> Cuando el binding normal falla, el CLR pregunta a
    /// <c>AppDomain.AssemblyResolve</c>. Aquí se responde con el ensamblado de la versión
    /// que SÍ está instalada: primero la carpeta <c>bin</c> del Desktop que diga el
    /// registro, y si no, la GAC. Es lo único que cubre también el <c>.esriaddin</c>
    /// instalado a mano, sin pasar por el instalador.
    /// </para>
    /// <para>
    /// <b>Sin tipos ESRI a propósito.</b> Esta clase solo usa BCL: si tocara un tipo ESRI,
    /// el JIT pediría los ensamblados al compilarla y el gancho llegaría tarde.
    /// </para>
    /// </remarks>
    internal static class ResolutorEsri
    {
        private const string Prefijo = "ESRI.ArcGIS.";
        private const string TokenEsri = "8fc3cc631e44ad86";

        [ThreadStatic] private static bool _resolviendo;
        private static readonly object Candado = new object();
        private static string[] _carpetas;

        /// <summary>Gancho de <see cref="AppDomain.AssemblyResolve"/>.</summary>
        public static Assembly Resolver(object sender, ResolveEventArgs e)
        {
            // Si lo que se pide se resuelve cargando otro ensamblado, ese Load puede
            // disparar el mismo evento y volver aqui sin fin. Una vuelta por hilo basta:
            // dentro de la resolucion no se intenta resolver nada mas.
            if (_resolviendo) return null;
            _resolviendo = true;
            try
            {
                Assembly encontrado = Buscar(e.Name);
                if (encontrado == null)
                    Console.Error.WriteLine("modo disco: no se pudo resolver " + e.Name);
                return encontrado;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("modo disco: fallo al resolver " + e.Name + ": " + ex.Message);
                return null;
            }
            finally
            {
                _resolviendo = false;
            }
        }

        /// <summary>
        /// Ensamblado de la versión instalada para el nombre pedido, o <c>null</c> si no es
        /// de ESRI o no se encuentra.
        /// </summary>
        internal static Assembly Buscar(string nombreCompleto)
        {
            if (string.IsNullOrEmpty(nombreCompleto)) return null;

            AssemblyName pedido;
            try { pedido = new AssemblyName(nombreCompleto); }
            catch (Exception) { return null; }

            if (pedido.Name == null || !pedido.Name.StartsWith(Prefijo, StringComparison.OrdinalIgnoreCase))
                return null;

            foreach (string carpeta in Carpetas())
            {
                Assembly a = Cargar(Path.Combine(carpeta, pedido.Name + ".dll"), pedido.Name);
                if (a != null) return a;
            }
            return BuscarEnGac(pedido.Name);
        }

        /// <summary>Carpetas <c>bin</c> de los Desktop que declare el registro, de 10.8 a 10.4.</summary>
        private static string[] Carpetas()
        {
            lock (Candado)
            {
                if (_carpetas != null) return _carpetas;

                List<string> carpetas = new List<string>();
                for (int menor = 8; menor >= 4; menor--)
                {
                    string version = "10." + menor;
                    string[] claves =
                    {
                        @"SOFTWARE\WOW6432Node\ESRI\Desktop" + version,
                        @"SOFTWARE\ESRI\Desktop" + version
                    };
                    foreach (string clave in claves)
                    {
                        string instalador = LeerCadena(Registry.LocalMachine, clave, "InstallDir");
                        if (string.IsNullOrEmpty(instalador)) continue;
                        string bin = Path.Combine(instalador, "bin");
                        if (!Directory.Exists(bin)) continue;
                        if (!carpetas.Exists(c => string.Equals(c, bin, StringComparison.OrdinalIgnoreCase)))
                            carpetas.Add(bin);
                    }
                }

                _carpetas = carpetas.ToArray();
                return _carpetas;
            }
        }

        private static string LeerCadena(RegistryKey raiz, string clave, string valor)
        {
            try
            {
                using (RegistryKey k = raiz.OpenSubKey(clave))
                {
                    if (k == null) return null;
                    return k.GetValue(valor) as string;
                }
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>Respaldo: la copia de la GAC, la de versión más alta que haya.</summary>
        private static Assembly BuscarEnGac(string nombre)
        {
            string raiz = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                @"Microsoft.NET\assembly\GAC_MSIL", nombre);
            if (!Directory.Exists(raiz)) return null;

            string mejor = null;
            Version mejorVersion = null;
            foreach (string sub in Directory.GetDirectories(raiz))
            {
                if (Path.GetFileName(sub).IndexOf("__" + TokenEsri, StringComparison.OrdinalIgnoreCase) < 0)
                    continue;
                string ruta = Path.Combine(sub, nombre + ".dll");
                if (!File.Exists(ruta)) continue;

                Version v;
                try { v = AssemblyName.GetAssemblyName(ruta).Version; }
                catch (Exception) { continue; }
                if (mejorVersion == null || v > mejorVersion)
                {
                    mejor = ruta;
                    mejorVersion = v;
                }
            }
            if (mejor == null) return null;   // mejor solo se usa para saber que hay una version

            // Desde la GAC NO se carga por ruta: LoadFrom sobre GAC_MSIL lanza
            // FileNotFoundException, porque el CLR exige identidad. Se pide por nombre.
            try
            {
                return Assembly.Load(string.Format(
                    "{0}, Version={1}, Culture=neutral, PublicKeyToken={2}",
                    nombre, mejorVersion, TokenEsri));
            }
            catch (Exception)
            {
                return null;
            }
        }

        private static Assembly Cargar(string ruta, string nombreEsperado)
        {
            if (!File.Exists(ruta)) return null;
            try
            {
                AssemblyName real = AssemblyName.GetAssemblyName(ruta);
                if (!string.Equals(real.Name, nombreEsperado, StringComparison.OrdinalIgnoreCase))
                    return null;
                return Assembly.LoadFrom(ruta);
            }
            catch (BadImageFormatException) { return null; }
            catch (FileLoadException) { return null; }
            catch (FileNotFoundException) { return null; }
        }
    }
}
