using System;
using System.IO;
using System.Net;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Comprobación de versión contra los tags de GitHub. El repo publica solo tags
    /// anotados (sin Releases), así que se consulta la API de tags. Todo el flujo es
    /// tolerante a fallo: sin internet, con rate-limit o con un JSON raro simplemente
    /// no pasa nada (nunca lanza, nunca bloquea, nunca molesta con un error).
    /// </summary>
    internal static class Actualizaciones
    {
        private const string TagsApiUrl =
            "https://api.github.com/repos/pedralcg/arcmap-mcp/tags";
        public const string RepoUrl = "https://github.com/pedralcg/arcmap-mcp";

        /// <summary>
        /// Instrucciones de actualización. El aviso de versión nueva lleva aquí y no a la
        /// portada del repo: desde la portada el usuario tenía que adivinar que el paquete
        /// vive en addin/dist/. Instalar por él no es posible (ArcMap mantiene la DLL
        /// cargada mientras está abierto), así que lo mejor que se puede hacer es dejarle
        /// a un clic lo que tiene que ejecutar.
        /// </summary>
        public const string ActualizarUrl =
            "https://github.com/pedralcg/arcmap-mcp/blob/main/docs/INSTALL.md#actualizar";

        /// <summary>Última versión conocida (de red o de caché), sin la "v". Para el indicador.</summary>
        public static string UltimaDisponible { get; private set; }

        /// <summary>¿La última conocida es mayor que la instalada?</summary>
        public static bool HayNueva { get; private set; }

        /// <summary>Versión instalada, desde el ensamblado.</summary>
        public static Version VersionActual()
        {
            return typeof(Actualizaciones).Assembly.GetName().Version;
        }

        /// <summary>
        /// Punto de entrada del hilo de fondo (lanzado desde OnStartup). Decide si toca
        /// consultar la red (máximo 1×/día), actualiza el indicador y, si hay una versión
        /// nueva que aún no se ha avisado, invoca 'avisar' UNA sola vez para esa versión.
        /// 'avisar' recibe la versión nueva (string) y se ejecuta en el hilo de fondo:
        /// el llamante es responsable de marshalarlo al hilo UI.
        /// </summary>
        public static void ComprobarEnSegundoPlano(Action<string> avisar)
        {
            try
            {
                string hoy = DateTime.Now.ToString("yyyy-MM-dd");
                Version encontrada = null;

                if (Ajustes.UltimaComprobacion == hoy)
                {
                    // Ya se comprobó hoy: usar la caché, sin tocar la red.
                    Version.TryParse(Ajustes.UltimaVersionVista, out encontrada);
                }
                else
                {
                    encontrada = ConsultarUltima();
                    if (encontrada != null)
                    {
                        Ajustes.UltimaComprobacion = hoy;
                        Ajustes.UltimaVersionVista = encontrada.ToString();
                    }
                }

                if (encontrada == null)
                    return; // sin dato fiable: no se toca el indicador

                UltimaDisponible = encontrada.ToString(3);
                Version actual = VersionActual();
                HayNueva = encontrada > actual;

                if (HayNueva && Ajustes.VersionAvisada != encontrada.ToString())
                {
                    Ajustes.VersionAvisada = encontrada.ToString();
                    if (avisar != null) avisar(encontrada.ToString(3));
                }
            }
            catch (Exception ex)
            {
                // Nunca debe afectar al arranque de ArcMap.
                Log.Error("Comprobación de actualización falló (ignorada)", ex);
            }
        }

        /// <summary>Consulta la API de tags y devuelve la mayor versión semver, o null
        /// ante cualquier problema (offline, 403 por rate-limit, JSON inesperado).</summary>
        public static Version ConsultarUltima()
        {
            try
            {
                // net45 no habilita TLS 1.2 por defecto; GitHub API lo exige. 3072 = Tls12.
                ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072;

                var req = (HttpWebRequest)WebRequest.Create(TagsApiUrl);
                req.Method = "GET";
                req.UserAgent = "arcmap-mcp-addin"; // GitHub API responde 403 sin User-Agent
                req.Accept = "application/vnd.github+json";
                req.Timeout = 8000;
                req.ReadWriteTimeout = 8000;

                using (var resp = (HttpWebResponse)req.GetResponse())
                using (var sr = new StreamReader(resp.GetResponseStream()))
                {
                    string cuerpo = sr.ReadToEnd();
                    JArray tags = JArray.Parse(cuerpo);

                    Version mayor = null;
                    foreach (JToken t in tags)
                    {
                        string nombre = (string)t["name"];
                        if (string.IsNullOrEmpty(nombre)) continue;
                        if (nombre[0] == 'v' || nombre[0] == 'V') nombre = nombre.Substring(1);
                        if (Version.TryParse(nombre, out Version ver))
                        {
                            if (mayor == null || ver > mayor) mayor = ver;
                        }
                    }
                    return mayor;
                }
            }
            catch (Exception ex)
            {
                Log.Info("No se pudo consultar la última versión en GitHub: " + ex.Message);
                return null;
            }
        }
    }
}
