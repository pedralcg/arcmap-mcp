using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Xml;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Lectura de los textos ORIGINALES desde el <c>config.xml</c> del paquete instalado.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <b>Por qué no basta con leer la propiedad del control.</b> La documentación de
    /// los add-ins sugiere que ArcMap aplica <c>config.xml</c> antes de construir el
    /// control, pero no es así: en el constructor —y en los primeros
    /// <c>OnUpdate</c>— <c>Caption</c>, <c>Tooltip</c> y <c>Message</c> valen cadena
    /// vacía. Guardar esos valores como "el original" hacía que volver al español
    /// escribiera <c>Caption = ""</c>; ArcMap rellenaba ese vacío con el texto de
    /// <c>config.xml</c>, con lo que el botón parecía correcto por casualidad, pero la
    /// comparación entre lo que se ve y lo que toca se volvía ambigua y la barra se
    /// quedaba a medias al cambiar de idioma.
    /// </para>
    /// <para>
    /// <b>Este fichero es la fuente de verdad y está en disco.</b> El paquete
    /// <c>.esriaddin</c> se despliega en
    /// <c>%USERPROFILE%\Documents\ArcGIS\AddIns\Desktop{N}\{GUID}\</c> con la DLL en
    /// <c>Install\</c> y el <c>config.xml</c> un nivel por encima, así que desde la
    /// ubicación del ensamblado se llega a él sin adivinar rutas.
    /// </para>
    /// <para>
    /// Se lee UNA vez y se cachea: son doce entradas y el XML no cambia mientras el
    /// proceso vive.
    /// </para>
    /// </remarks>
    internal static class ConfigXml
    {
        /// <summary>
        /// Mapa clave del diccionario → id del elemento en <c>config.xml</c>. Se declara
        /// aquí y no en cada control para que el acoplamiento con el XML esté en un solo
        /// sitio: si cambia un id, se cambia una línea.
        /// </summary>
        private static readonly Dictionary<string, string> Ids =
            new Dictionary<string, string>(StringComparer.Ordinal)
        {
            { "combo.accion", "pedralcg_arcmap_mcp_Acciones" },
            { "combo.auto",   "pedralcg_arcmap_mcp_AutoStart" },
            { "combo.lang",   "pedralcg_arcmap_mcp_Language" },
        };

        private static bool _cargado;
        private static readonly object _lock = new object();

        // clave -> [caption, tip, message]. Cadena vacía cuando el XML no lo trae.
        private static readonly Dictionary<string, string[]> _textos =
            new Dictionary<string, string[]>(StringComparer.Ordinal);

        /// <summary>
        /// Textos originales de un control: <c>[caption, tip, message]</c>, o
        /// <c>null</c> si no se pudo leer el XML o la clave no está mapeada.
        /// </summary>
        public static string[] TextoDelBoton(string claveIdioma, string tipoControl)
        {
            Cargar();
            string[] par;
            if (claveIdioma != null && _textos.TryGetValue(claveIdioma, out par)) return par;
            return null;
        }

        private static void Cargar()
        {
            if (_cargado) return;
            lock (_lock)
            {
                if (_cargado) return;
                _cargado = true;

                try
                {
                    string ruta = LocalizarConfig();
                    if (ruta == null)
                    {
                        Log.Warn("No se encontró config.xml junto al add-in: los textos "
                                 + "originales se leerán del propio control.");
                        return;
                    }

                    var doc = new XmlDocument();
                    doc.Load(ruta);

                    foreach (KeyValuePair<string, string> par in Ids)
                    {
                        XmlNode nodo = doc.SelectSingleNode("//*[@id='" + par.Value + "']");
                        if (nodo == null) continue;

                        _textos[par.Key] = new[]
                        {
                            Atributo(nodo, "caption"),
                            Atributo(nodo, "tip"),
                            Atributo(nodo, "message"),
                        };
                    }

                    Log.Info("config.xml leído: " + _textos.Count + " controles con texto original ("
                             + ruta + ")");
                }
                catch (Exception ex)
                {
                    Log.Warn("No se pudo leer config.xml: " + ex.Message);
                }
            }
        }

        private static string Atributo(XmlNode nodo, string nombre)
        {
            XmlAttribute a = nodo.Attributes != null ? nodo.Attributes[nombre] : null;
            return a != null ? a.Value ?? "" : "";
        }

        /// <summary>
        /// Busca <c>config.xml</c>.
        /// </summary>
        /// <remarks>
        /// <b>ArcMap NO ejecuta la DLL desde la carpeta del add-in.</b> Copia el paquete a
        /// <c>%LOCALAPPDATA%\ESRI\Desktop10.x\AssemblyCache\{GUID}\</c> y carga desde
        /// ahí, y a esa caché solo viajan los ensamblados: el <c>config.xml</c> se queda
        /// en <c>%USERPROFILE%\Documents\ArcGIS\AddIns\Desktop10.x\{GUID}\</c>. Por eso
        /// "junto al ensamblado" no basta y hay que mirar también la ruta de instalación,
        /// derivada del mismo GUID que figura en el directorio de la caché.
        /// </remarks>
        private static string LocalizarConfig()
        {
            foreach (string candidato in Candidatos())
            {
                try
                {
                    if (!string.IsNullOrEmpty(candidato) && File.Exists(candidato)) return candidato;
                }
                catch { }
            }
            return null;
        }

        private static IEnumerable<string> Candidatos()
        {
            // 1. Junto al ensamblado y un par de niveles hacia arriba.
            string dirEnsamblado = null;
            try
            {
                dirEnsamblado = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            }
            catch (Exception ex)
            {
                Log.Warn("No se pudo leer la ubicación del ensamblado: " + ex.Message);
            }

            for (string d = dirEnsamblado; d != null; d = Path.GetDirectoryName(d))
            {
                yield return Path.Combine(d, "config.xml");
            }

            if (string.IsNullOrEmpty(dirEnsamblado)) yield break;

            // 2. La carpeta de instalación del add-in: mismo GUID, bajo Documents.
            //    AssemblyCache\{GUID} -> AddIns\Desktop{N}\{GUID}
            string guid;
            try { guid = Path.GetFileName(dirEnsamblado.TrimEnd(Path.DirectorySeparatorChar)); }
            catch { guid = null; }

            if (!string.IsNullOrEmpty(guid))
            {
                // El ensamblado vive en ...\Desktop10.8\AssemblyCache\{GUID}\: dos niveles
                // por encima está el nombre de la carpeta de versión que usa también la
                // carpeta de instalación (Documents\ArcGIS\AddIns\Desktop10.8\{GUID}\).
                string dirCache = Path.GetDirectoryName(dirEnsamblado.TrimEnd(Path.DirectorySeparatorChar));
                string nombreVersion = dirCache == null
                    ? null
                    : Path.GetFileName(Path.GetDirectoryName(dirCache));
                string documentos = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments);

                if (!string.IsNullOrEmpty(documentos) && !string.IsNullOrEmpty(nombreVersion))
                {
                    string instalacion = Path.Combine(
                        documentos, "ArcGIS", "AddIns", nombreVersion, guid);
                    yield return Path.Combine(instalacion, "config.xml");
                    yield return Path.Combine(instalacion, "Install", "config.xml");
                }
            }
        }
    }
}
