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

        /// <summary>True si el texto parece una ruta (o un nombre de fichero con unidad), no
        /// un módulo de arcpy: `C:\tools\mis.tbx\MiModelo`. Ningún nombre de tool del sistema
        /// ni alias de toolbox lleva `\`, `/` ni `:`.</summary>
        internal static bool PareceRuta(string texto)
        {
            return texto.IndexOf('\\') >= 0 || texto.IndexOf('/') >= 0 || texto.IndexOf(':') >= 0;
        }

        /// <summary>Parte `C:\carpeta\mis.tbx\[Toolset\]Herramienta` en la ruta del toolbox
        /// (.tbx o .pyt) y el nombre de la herramienta (el último tramo: los toolsets solo
        /// organizan, el nombre es único dentro del toolbox). Devuelve false si no hay un tramo
        /// .tbx/.pyt seguido de una herramienta. Sin ArcObjects, para probarla sin ArcMap.</summary>
        internal static bool PartirRutaToolbox(string ruta, out string toolbox, out string herramienta)
        {
            toolbox = null;
            herramienta = null;
            if (string.IsNullOrEmpty(ruta)) return false;
            string s = ruta.Trim().TrimEnd('\\', '/');
            string[] tramos = s.Split('\\', '/');
            int pos = 0;
            for (int i = 0; i < tramos.Length - 1; i++)
            {
                pos += tramos[i].Length;
                string t = tramos[i].ToLowerInvariant();
                if (t.EndsWith(".tbx") || t.EndsWith(".pyt"))
                {
                    toolbox = s.Substring(0, pos);
                    herramienta = tramos[tramos.Length - 1].Trim();
                    return herramienta.Length > 0 && toolbox.Length > 4;
                }
                pos += 1; // el separador
            }
            return false;
        }

        // Alias -> ruta de los toolboxes que ha añadido este add-in. El geoprocesador es uno
        // por proceso: lo que se añade con AddToolbox lo ven todas las instancias (medido el
        // 2026-11-05), y acepta dos toolboxes con el MISMO alias sin quejarse. Este registro
        // es lo que impide que un alias repetido ejecute la herramienta de otro toolbox.
        private static readonly Dictionary<string, string> ToolboxesPorAlias =
            new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> ToolboxesSinAlias =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        /// <summary>
        /// Herramienta de un .tbx/.pyt propio por su ruta → nombre GP ejecutable. IGeoProcessor
        /// NO ejecuta por ruta (E_FAIL sin arrancar, medido en 10.5 dentro y fuera de ArcMap, PR
        /// #10): hay que añadir el toolbox y llamar a `Herramienta_alias`, que además deja leer
        /// la firma con Usage. Rechaza un alias que ya use OTRO toolbox (el GP los aceptaría los
        /// dos y podría correr la herramienta equivocada). Sin alias se llama por el nombre a
        /// secas: si está repetido el GP se niega («not unique») y aquí se explica qué hacer.
        /// Corre en el hilo de ArcMap.
        /// </summary>
        internal static string ResolverToolPorRuta(IGeoProcessor2 gp, string ruta)
        {
            string tbx, herramienta;
            if (!PartirRutaToolbox(ruta, out tbx, out herramienta))
                throw new ArgumentException("'" + ruta + "' parece una ruta, pero no de una herramienta"
                    + " de toolbox. Usa 'C:\\carpeta\\mi_toolbox.tbx\\Herramienta' (o .pyt), o la forma"
                    + " de arcpy 'modulo.Herramienta'.");
            tbx = System.IO.Path.GetFullPath(tbx);
            if (!System.IO.File.Exists(tbx))
                throw new ArgumentException("No existe el toolbox '" + tbx + "'.");

            IGPToolbox toolbox;
            try
            {
                IGPUtilities u = new GPUtilitiesClass();
                toolbox = (IGPToolbox)u.GetNameObjectFromLocation(tbx).Open();
            }
            catch (Exception ex)
            {
                throw new ArgumentException("No se pudo abrir el toolbox '" + tbx + "': " + ex.Message);
            }
            IGPTool gpTool = null;
            try { gpTool = toolbox.OpenTool(herramienta); } catch { }
            if (gpTool == null)
                throw new ArgumentException("El toolbox '" + tbx + "' no tiene la herramienta '"
                    + herramienta + "'. Tiene: " + NombresDeTools(toolbox) + ". Va el NOMBRE de la"
                    + " herramienta, no su etiqueta.");
            herramienta = gpTool.Name;

            string alias = toolbox.Alias ?? "";
            if (alias.Length > 0)
            {
                string previa;
                if (ToolboxesPorAlias.TryGetValue(alias, out previa))
                {
                    if (!string.Equals(previa, tbx, StringComparison.OrdinalIgnoreCase))
                        throw new ArgumentException("El alias '" + alias + "' del toolbox '" + tbx
                            + "' ya lo usa otro toolbox de esta sesión ('" + previa + "'). Con dos"
                            + " toolboxes del mismo alias el geoprocesador podría ejecutar la"
                            + " herramienta del otro: cambia el alias en las propiedades del toolbox.");
                }
                else
                {
                    string otro = ToolboxDeSistemaConAlias(gp, alias, toolbox);
                    if (otro != null)
                        throw new ArgumentException("El alias '" + alias + "' del toolbox '" + tbx
                            + "' es el de '" + otro + "'. Con el mismo alias el geoprocesador podría"
                            + " ejecutar la herramienta del otro: cambia el alias en las propiedades"
                            + " del toolbox, o llama a la herramienta del sistema por su nombre"
                            + " ('" + alias + "." + herramienta + "').");
                }
            }

            else if (!ToolboxesSinAlias.Contains(tbx))
            {
                // Sin alias la herramienta se llama por el nombre a secas. Si ese nombre ya
                // existe en la sesión, añadir el toolbox lo dejaría ambiguo para todos («not
                // unique»): se comprueba ANTES de AddToolbox.
                string previa = "";
                try { previa = gp.Usage(herramienta) ?? ""; } catch { }
                if (previa.IndexOf(" not found", StringComparison.OrdinalIgnoreCase) < 0
                    || previa.IndexOf("not unique", StringComparison.OrdinalIgnoreCase) >= 0)
                    throw new ArgumentException("El toolbox '" + tbx + "' no tiene alias y ya hay"
                        + " una herramienta '" + herramienta + "' en la sesión: añadirlo la dejaría"
                        + " ambigua. Ponle un alias al toolbox (propiedades del toolbox en ArcCatalog).");
            }

            gp.AddToolbox(tbx);
            if (alias.Length > 0)
                ToolboxesPorAlias[alias] = tbx;
            else
                ToolboxesSinAlias.Add(tbx);
            string nombre = alias.Length > 0 ? herramienta + "_" + alias : herramienta;
            string firma = "";
            try { firma = gp.Usage(nombre) ?? ""; } catch { }
            if (firma.IndexOf("not unique", StringComparison.OrdinalIgnoreCase) >= 0)
                throw new ArgumentException("Hay otra herramienta '" + nombre + "' en la sesión"
                    + (alias.Length == 0 ? " y el toolbox '" + tbx + "' no tiene alias" : "")
                    + ", así que el geoprocesador no sabe cuál ejecutar."
                    + (alias.Length == 0 ? " Ponle un alias al toolbox (propiedades del toolbox en ArcCatalog)."
                                         : " Cambia el alias del toolbox."));
            Log.Info("Herramienta por ruta: '" + ruta + "' -> '" + nombre + "' (toolbox " + tbx + ").");
            return nombre;
        }

        private static string NombresDeTools(IGPToolbox toolbox)
        {
            var nombres = new List<string>();
            try
            {
                IEnumGPTool ts = toolbox.Tools;
                ts.Reset();
                IGPTool t;
                while ((t = ts.Next()) != null && nombres.Count < 30)
                    nombres.Add(t.Name);
            }
            catch { }
            return nombres.Count > 0 ? string.Join(", ", nombres) : "(no se pudo listar)";
        }

        /// <summary>Nombre del toolbox YA cargado que usa este alias, o null. Se decide por
        /// RUTA, no por nombre: una copia de Data Management Tools.tbx conserva el nombre
        /// interno «Data Management Tools» y por nombre pasaba por el del sistema (medido el
        /// 2026-11-05). Añadirla de todos modos deja DOS toolboxes con alias 'management' y
        /// desde ese momento 'management.GetCount' da «not unique» en toda la sesión: por eso
        /// se rechaza ANTES de AddToolbox. Solo se admite el alias repetido si el fichero es
        /// el propio toolbox del sistema (el de la carpeta Toolboxes de la instalación).
        /// ListToolboxes da "Nombre(alias)".</summary>
        private static string ToolboxDeSistemaConAlias(IGeoProcessor2 gp, string alias, IGPToolbox toolbox)
        {
            if (EsToolboxDeLaInstalacion(toolbox.PathName))
                return null;
            string sufijo = "(" + alias + ")";
            try
            {
                IGpEnumList lista = gp.ListToolboxes("*");
                string x;
                while (!string.IsNullOrEmpty(x = lista.Next()))
                    if (x.EndsWith(sufijo, StringComparison.OrdinalIgnoreCase))
                        return x.Substring(0, x.Length - sufijo.Length);
            }
            catch (Exception ex)
            {
                // Sin la lista no se puede descartar el choque: mejor negarse que dejar la
                // sesión con un alias duplicado.
                throw new ArgumentException("No se pudo comprobar si el alias '" + alias
                    + "' ya está en uso (ListToolboxes: " + ex.Message + "); no se añade el toolbox.");
            }
            return null;
        }

        /// <summary>True si la ruta está en la carpeta ArcToolbox\Toolboxes de la instalación
        /// de ArcGIS en uso: es un toolbox del sistema, que ya está cargado.</summary>
        private static bool EsToolboxDeLaInstalacion(string ruta)
        {
            try
            {
                string full = System.IO.Path.GetFullPath(ruta ?? "");
                foreach (string raiz in RaicesInstalacion())
                {
                    string dir = System.IO.Path.GetFullPath(System.IO.Path.Combine(raiz, "ArcToolbox", "Toolboxes"))
                        .TrimEnd('\\') + "\\";
                    if (full.StartsWith(dir, StringComparison.OrdinalIgnoreCase)
                        && full.IndexOf('\\', dir.Length) < 0)
                        return true;
                }
            }
            catch { }
            return false;
        }

        /// <summary>Carpeta de la instalación de ArcGIS Desktop: la del ejecutable del proceso
        /// (".../Desktop10.5/bin/ArcMap.exe", como en MxdVersion) y, si no corre dentro de
        /// ArcMap, la InstallDir del registro de ESRI.</summary>
        private static IEnumerable<string> RaicesInstalacion()
        {
            string exe = null;
            try { exe = System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName; } catch { }
            if (exe != null)
            {
                string bin = System.IO.Path.GetDirectoryName(exe);
                if (string.Equals(System.IO.Path.GetFileName(bin), "bin", StringComparison.OrdinalIgnoreCase))
                    yield return System.IO.Path.GetDirectoryName(bin);
            }
            string version = MxdVersion.VersionArcMapSesion() ?? "10.5";
            foreach (string clave in new[] { @"SOFTWARE\Wow6432Node\ESRI\Desktop" + version, @"SOFTWARE\ESRI\Desktop" + version })
            {
                string dir = null;
                try
                {
                    using (var k = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(clave))
                        if (k != null) dir = k.GetValue("InstallDir") as string;
                }
                catch { }
                if (!string.IsNullOrEmpty(dir))
                    yield return dir;
            }
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

            // Una herramienta de un .tbx/.pyt propio llega por ruta, que Execute no admite:
            // se añade el toolbox y se pasa a 'Herramienta_alias' (PR #10). Desde aquí sigue
            // el camino normal, con la comprobación de firma incluida.
            if (PareceRuta(tool.Trim()))
                nombreGp = ResolverToolPorRuta(gp, tool.Trim());

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
            // 'capa' es alias de 'entrada' en el puente (el servidor MCP solo manda 'entrada'): se deja
            // por compatibilidad con quien hable al puente directamente.
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
        /// (GetCount_management); el alias de toolbox sale por <paramref name="alias"/>.
        /// La ruta de un .tbx propio (`C:\tools\mis.tbx\MiModelo`) pasa TAL CUAL, que el
        /// geoprocesador sí sabe ejecutar: antes se partía por el PRIMER punto (el de
        /// «ArcGIS10.8» o el de `.tbx`) y ningún modelo ni script tool se podía llamar
        /// (PR #10, psf412). Se acepta y se ignora el prefijo `arcpy.`. Todo lo que no es
        /// ruta se trata exactamente como antes, sin lista de módulos: así siguen valiendo
        /// `3d.Slope` y el alias de un toolbox propio importado (`mialias.MiModelo`).</summary>
        internal static string NombreGp(string tool, out string alias)
        {
            alias = null;
            if (string.IsNullOrEmpty(tool)) return tool;
            string s = tool.Trim();
            if (PareceRuta(s)) return s;
            if (s.StartsWith("arcpy.", StringComparison.OrdinalIgnoreCase))
                s = s.Substring("arcpy.".Length);
            int punto = s.IndexOf('.');
            if (punto > 0)
            {
                alias = AliasToolbox(s.Substring(0, punto));
                return s.Substring(punto + 1) + "_" + alias;
            }
            int guion = s.LastIndexOf('_');
            if (guion > 0)
                alias = s.Substring(guion + 1);
            return s;
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
