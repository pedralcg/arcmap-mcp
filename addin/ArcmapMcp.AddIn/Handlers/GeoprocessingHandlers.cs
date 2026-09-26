using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Geoprocessing;
using ESRI.ArcGIS.esriSystem;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// run_geoprocessing — NATIVO por IGeoProcessor2 sobre la sesión viva:
    /// conserva la semántica de capas de la TOC con definition query y selección,
    /// que el subprocess no puede dar. Contrato JSON de los schemas del servidor
    /// MCP. Corre en STA con el timeout largo; un GP pesado congela la GUI
    /// (limitación conocida — para análisis pesados están las tools ambientales,
    /// que van out-of-process).
    /// </summary>
    internal static class GeoprocessingHandlers
    {
        // Guard de resolución: strings con pinta de ruta o de SQL no se resuelven
        // a capa (un nombre de campo o keyword que coincida se sustituiría en silencio).
        internal static readonly char[] NoResolver = "\\/:*?\"<>|='".ToCharArray();

        // Módulo arcpy -> alias de toolbox del GeoProcessor: coinciden salvo ddd.
        private static string AliasToolbox(string moduloArcpy)
        {
            return string.Equals(moduloArcpy, "ddd", StringComparison.OrdinalIgnoreCase)
                ? "3d" : moduloArcpy;
        }

        /// <summary>Checkout best-effort de la extensión que el toolbox necesita
        /// (equivalente .NET del checkout de extensiones de arcpy). Si falla,
        /// Execute dará su propio error de licencia y se devuelve tal cual.</summary>
        private static void CheckoutSiHaceFalta(string alias)
        {
            esriLicenseExtensionCode codigo;
            if (string.Equals(alias, "sa", StringComparison.OrdinalIgnoreCase))
                codigo = esriLicenseExtensionCode.esriLicenseExtensionCodeSpatialAnalyst;
            else if (string.Equals(alias, "3d", StringComparison.OrdinalIgnoreCase))
                codigo = esriLicenseExtensionCode.esriLicenseExtensionCode3DAnalyst;
            else
                return;
            try
            {
                IAoInitialize lic = new AoInitializeClass();
                if (!lic.IsExtensionCheckedOut(codigo))
                    lic.CheckOutExtension(codigo);
            }
            catch (Exception ex)
            {
                Log.Info("Checkout de extensión '" + alias + "' falló (se intenta el GP igualmente): " + ex.Message);
            }
        }

        public static JObject RunGeoprocessing(JObject parameters)
        {
            string tool = (string)parameters["tool"];
            if (string.IsNullOrEmpty(tool))
                throw new ArgumentException(
                    "Indica 'tool' (ej. 'management.CopyFeatures' o 'Buffer_analysis').");
            JArray args = parameters["params"] as JArray ?? new JArray();
            bool resolverCapas = parameters["resolver_capas"] == null
                || parameters["resolver_capas"].Type == JTokenType.Null
                || (bool)parameters["resolver_capas"];

            string alias;
            string nombreGp = NombreGp(tool, out alias);
            if (alias != null)
                CheckoutSiHaceFalta(alias);

            // Capas de la TOC disponibles para la resolución de nombres (gotcha
            // ERROR 000732 de arcpy fuera de la ventana interactiva: el GP no
            // resuelve nombres de capa por sí solo; pasar el objeto Layer honra
            // además su definition query y selección).
            var capas = new List<ILayer>();
            if (resolverCapas)
            {
                IMxDocument doc;
                IMap map = MapHandlers.FocusMap(out doc);
                // Por el helper: get_Layers(null, true) lanza E_FAIL con la TOC
                // vacía, así que un geoproceso puramente de disco fallaba por no
                // tener capas cargadas, que no tiene nada que ver.
                capas = MapHandlers.Capas(map);
            }

            IGeoProcessor2 gp = new GeoProcessorClass();

            // La firma de la tool, antes de nada. Sin esto, un nombre de tool que no
            // existe fallaba en Execute ANTES de arrancar, sin escribir mensajes propios,
            // y el error salía con los mensajes del geoproceso ANTERIOR: los mensajes
            // del geoprocesador son del proceso, no de cada GeoProcessorClass.
            // Reproducido el 2026-09-26 (era el «devuelve el error de la llamada
            // anterior» del informe de uso de agosto).
            ComprobarFirma(gp, tool, nombreGp, args.Count);

            IVariantArray valores = new VarArrayClass();
            foreach (JToken a in args)
                valores.Add(ResolverArg(a, resolverCapas, capas));

            gp.AddOutputsToMap = true; // mismo comportamiento que arcpy dentro de ArcMap
            ESRI.ArcGIS.esriSystem.ITrackCancel cancel =
                (ESRI.ArcGIS.esriSystem.ITrackCancel)new ESRI.ArcGIS.Display.CancelTrackerClass();

            // Por la misma razón: lo que quede en la cola de mensajes es de otra llamada.
            gp.ClearMessages();
            IGeoProcessorResult resultado;
            try
            {
                resultado = gp.Execute(nombreGp, valores, cancel);
            }
            catch (Exception ex)
            {
                string mensajes = Mensajes(gp);
                throw new InvalidOperationException(
                    "Geoproceso '" + tool + "' falló: " + ex.Message + ". Mensajes GP: "
                    + (string.IsNullOrWhiteSpace(mensajes)
                        ? "(ninguno: el geoproceso no llegó a arrancar)" : mensajes), ex);
            }

            var salidas = new JArray();
            try
            {
                for (int i = 0; i < resultado.OutputCount; i++)
                {
                    ESRI.ArcGIS.Geodatabase.IGPValue v = resultado.GetOutput(i);
                    salidas.Add(v != null ? v.GetAsText() : null);
                }
            }
            catch { /* salidas best-effort */ }

            return Protocol.Result(new JObject
            {
                ["tool"] = tool,
                ["salidas"] = salidas,
                ["mensajes"] = Mensajes(gp),
            });
        }

        /// <summary>calculate_geometry — NATIVO vía GeoProcessor in-process
        /// (contrato JSON de los schemas del servidor MCP). NO puede ir por
        /// subprocess: la sesión viva mantiene un schema lock sobre toda fuente
        /// cargada en la TOC y AddGeometryAttributes necesita añadir campos.
        /// In-process no hay conflicto y pasar el Layer honra def. query y selección.</summary>
        public static JObject CalculateGeometry(JObject parameters)
        {
            string entrada = (string)(parameters["entrada"] ?? parameters["capa"]);
            JToken props = parameters["propiedades"];
            if (string.IsNullOrEmpty(entrada) || props == null || props.Type == JTokenType.Null)
                throw new ArgumentException("Indica 'entrada' (capa o feature class) y 'propiedades'.");
            string propiedades = props.Type == JTokenType.Array
                ? string.Join(";", ((JArray)props).ToObject<string[]>())
                : (string)props;
            string unidadLongitud = (string)parameters["unidad_longitud"] ?? "";
            string unidadArea = (string)parameters["unidad_area"] ?? "";
            string crs = (string)parameters["crs"] ?? "";

            var gpParams = new JObject
            {
                ["tool"] = "management.AddGeometryAttributes",
                ["params"] = new JArray { entrada, propiedades, unidadLongitud, unidadArea, crs },
                // la resolución de capas usa el guard de arriba; una ruta no se toca
                ["resolver_capas"] = true,
            };
            JObject r = RunGeoprocessing(gpParams);
            if (!(bool)r["ok"])
                return r;
            return Protocol.Result(new JObject
            {
                ["entrada"] = entrada,
                ["propiedades"] = propiedades,
                ["unidad_longitud"] = unidadLongitud,
                ["unidad_area"] = unidadArea,
            });
        }

        private static object ResolverArg(JToken a, bool resolverCapas, List<ILayer> capas)
        {
            switch (a.Type)
            {
                case JTokenType.Integer:
                    // Un (long) mete un VT_I8 en el IVariantArray, y lo habitual con el
                    // GeoProcessor es VT_I4. Se manda int siempre que quepa. OJO: es un
                    // cambio PREVENTIVO (2026-09-20), no la cura de un fallo observado:
                    // no consta ningún GP que fallara con long. Si tras esta versión un
                    // geoproceso empieza a quejarse de un parámetro numérico, mirar aquí.
                    long entero = (long)a;
                    return entero >= int.MinValue && entero <= int.MaxValue
                        ? (object)(int)entero : (object)entero;
                case JTokenType.Float: return (double)a;
                case JTokenType.Boolean: return (bool)a;
                case JTokenType.Null: return "";
                case JTokenType.Date:
                    // Newtonsoft convierte SOLO un texto con pinta de fecha ISO completa
                    // ("2026-09-20T10:00:00Z") en un token Date, así que para quien llama
                    // sigue siendo un texto. Sin este caso caía en el `default`, que desde
                    // la 2.12.0 lanza, con un mensaje que le negaba haber mandado texto
                    // (probado sobre Newtonsoft 13 net45). Se devuelve en ISO invariante:
                    // el ToString() a secas dependía de la cultura de la máquina.
                    return ((DateTime)a).ToString("yyyy-MM-dd HH:mm:ss",
                                                  System.Globalization.CultureInfo.InvariantCulture);
                case JTokenType.String:
                    string s = (string)a;
                    if (resolverCapas && s.IndexOfAny(NoResolver) < 0)
                    {
                        foreach (ILayer lyr in capas)
                            if (string.Equals(lyr.Name, s, StringComparison.OrdinalIgnoreCase))
                                return lyr;
                    }
                    return s;
                case JTokenType.Array:
                    return Multivalor((JArray)a, resolverCapas, capas);
                default:
                    // Un objeto JSON ({...}) no tiene traducción a parámetro de GP. Antes
                    // se mandaba su texto tal cual y la tool fallaba con un 000732 que
                    // hablaba de una "entrada" inexistente: el mismo defecto que tenían
                    // las listas. Mejor decirlo aquí que dejar que lo adivinen.
                    string crudo = a.ToString(Newtonsoft.Json.Formatting.None);
                    if (crudo.Length > 120) crudo = crudo.Substring(0, 120) + "...";
                    throw new ArgumentException(
                        "Parámetro de geoproceso no admitido (" + a.Type + "): " + crudo
                        + ". Cada elemento de 'params' debe ser texto, número, booleano, null"
                        + " o una LISTA (multivalor, se une con ';'). Un value table se pasa como"
                        + " texto con la sintaxis del geoproceso, p. ej. \"campo1 SUM;campo2 MEAN\".");
            }
        }

        /// <summary>
        /// Lista JSON → MULTIVALOR del geoprocesador, que es una cadena con los
        /// elementos unidos por ';' (["a","b"] → "a;b"). Antes caía en el `default`
        /// y llegaba al GP el texto JSON entero (`["a","b"]`), que es exactamente el
        /// ERROR 000732 de Merge/Union/Intersect: la tool no encontraba esa "entrada".
        ///
        /// Un elemento que se resuelve a una capa de la TOC entra por su RUTA DE
        /// FUENTE, no por su nombre: en un multivalor no hay forma de pasar el objeto
        /// ILayer (el IVariantArray recibe UNA cadena), y el GP tampoco resuelve
        /// nombres de la TOC por su cuenta. El precio es que ahí se pierden la
        /// definition query y la selección de esa capa; para respetarlas hay que
        /// pasar la capa como argumento suelto, no dentro de una lista.
        /// </summary>
        private static string Multivalor(JArray arr, bool resolverCapas, List<ILayer> capas)
        {
            var partes = new List<string>();
            foreach (JToken el in arr)
            {
                if (el.Type == JTokenType.Array)
                    throw new ArgumentException("'params' no admite listas dentro de listas: "
                        + "un multivalor del geoprocesador es una lista plana de valores.");
                object v = ResolverArg(el, resolverCapas, capas);
                ILayer capa = v as ILayer;
                if (capa != null)
                {
                    string workspace;
                    string ruta = DataAccess.RutaFuente(capa, out workspace);
                    if (string.IsNullOrEmpty(ruta))
                        throw new ArgumentException("La capa '" + capa.Name + "' va dentro de una lista "
                            + "y su fuente en disco no es resoluble, así que el geoproceso no podría "
                            + "abrirla. Pásale la ruta del dato en vez del nombre de la capa.");
                    partes.Add(ruta);
                }
                else
                {
                    partes.Add(Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture));
                }
            }
            return string.Join(";", partes);
        }

        /// <summary>Forma punteada estilo arcpy (management.GetCount) → nombre GP
        /// (GetCount_management); el alias de toolbox sale por <paramref name="alias"/>.</summary>
        internal static string NombreGp(string tool, out string alias)
        {
            alias = null;
            int punto = tool.IndexOf('.');
            if (punto > 0)
            {
                alias = AliasToolbox(tool.Substring(0, punto));
                return tool.Substring(punto + 1) + "_" + alias;
            }
            int guion = tool.LastIndexOf('_');
            if (guion > 0)
                alias = tool.Substring(guion + 1);
            return tool;
        }

        /// <summary>
        /// Lanza si la tool no existe o si se le pasan más parámetros de los que admite.
        /// Lo usan las DOS vías de run_geoprocessing: dentro de ArcMap el geoprocesador
        /// ignoraba los de más sin avisar, y fuera (arcpy.gp en el runner) uno de más se
        /// ignora y DOS tumban Python con una violación de acceso (medido 2026-09-26).
        /// Corre en el hilo de ArcMap (GeoProcessorClass es STA).
        /// </summary>
        internal static void ComprobarFirma(IGeoProcessor2 gp, string tool, string nombreGp, int nArgs)
        {
            int maxParams = MaxParametros(gp, tool, nombreGp);
            if (maxParams >= 0 && nArgs > maxParams)
                throw new ArgumentException(
                    "'" + tool + "' admite " + maxParams + " parámetros y se han pasado " + nArgs
                    + ". El geoprocesador ignora los de más sin avisar, así que no se ejecuta."
                    + " Firma: " + gp.Usage(nombreGp));
        }

        /// <summary>
        /// Número máximo de parámetros según la firma que da <c>Usage</c>, p. ej.
        /// "Usage: GetCount_management in_rows" → 1. Lanza si la tool no existe. Devuelve -1
        /// si la firma no se deja leer o Usage falla, y entonces no se comprueba nada: mejor no
        /// validar que rechazar una llamada buena por un formato de firma inesperado.
        /// </summary>
        private static int MaxParametros(IGeoProcessor2 gp, string tool, string nombreGp)
        {
            string firma;
            try
            {
                firma = gp.Usage(nombreGp);
            }
            catch (Exception ex)
            {
                // Que Usage LANCE no quiere decir que la tool no exista: en las 977 tools de
                // sistema de 10.5 solo lanza en las de Data Interoperability sin la extensión
                // instalada ("Clase no registrada"). No se valida y el geoproceso da su error.
                Log.Info("Usage(" + nombreGp + ") lanzó: " + ex.Message + ". Se ejecuta sin comprobar la firma.");
                return -1;
            }
            // Con una tool inexistente Usage NO lanza: devuelve "Method X not found."
            // (medido en 10.5, en inglés aunque ArcMap esté en español). Auditado el
            // 2026-09-26 contra las 977 tools de sistema: ninguna devuelve "not found" y el
            // recuento nunca queda por debajo de lo que la tool admite (827 exactos, 148 por
            // encima: ahí los parámetros de más no se detectan, pero no se rechaza nada bueno).
            if (string.IsNullOrWhiteSpace(firma)
                || firma.IndexOf(" not found", StringComparison.OrdinalIgnoreCase) >= 0)
                throw new ArgumentException("No existe la herramienta de geoproceso '" + tool
                    + "' (nombre GP '" + nombreGp + "'). Usa la forma de arcpy, 'modulo.Herramienta'"
                    + " (p. ej. 'management.GetCount').");
            return ContarParametros(firma);
        }

        /// <summary>Cuenta los parámetros de una firma de <c>Usage</c>, que en 10.5 es
        /// "Usage: Buffer_analysis in_features out_feature_class {FULL | LEFT} {a;a...}":
        /// tras el prefijo y el nombre, un parámetro por palabra, con los opcionales entre
        /// llaves (que llevan espacios dentro). Devuelve -1 con cualquier otro formato.
        /// Internal para poder probarlo sin ArcMap.</summary>
        internal static int ContarParametros(string firma)
        {
            string s = firma.Trim();
            if (!s.StartsWith("Usage:", StringComparison.OrdinalIgnoreCase))
                return -1;
            s = s.Substring("Usage:".Length);
            int nivel = 0, n = 0;
            bool enPalabra = false;
            foreach (char c in s)
            {
                if (c == '{' || c == '[' || c == '(') nivel++;
                else if (c == '}' || c == ']' || c == ')') nivel--;
                bool blanco = char.IsWhiteSpace(c) && nivel == 0;
                if (!blanco && !enPalabra) n++;
                enPalabra = !blanco;
            }
            // La primera palabra es el nombre de la tool.
            return nivel == 0 && n > 0 ? n - 1 : -1;
        }

        private static string Mensajes(IGeoProcessor2 gp)
        {
            try
            {
                object severidad = 0;
                return gp.GetMessages(ref severidad);
            }
            catch
            {
                return "";
            }
        }
    }
}
