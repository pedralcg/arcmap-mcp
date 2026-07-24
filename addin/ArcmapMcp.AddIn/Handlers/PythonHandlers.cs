using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Path = System.IO.Path; // ESRI.ArcGIS.Geometry también define Path

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Tools que ArcObjects .NET no cubre, resueltas OUT-OF-PROCESS con el arcpy
    /// standalone de la máquina: execute_arcpy + las 3 DDP (solo existen en
    /// arcpy) + las 6 ambientales (lógica arcpy en Python\runner.py, recurso
    /// embebido).
    ///
    /// Estos handlers corren en el THREAD DE FONDO del listener (no en STA): el
    /// subprocess no congela la GUI de ArcMap — un GP pesado in-process
    /// bloquearía la aplicación y podría matar el puente.
    /// Solo los pasos ArcObjects (snapshot, añadir al mapa, extent) van al STA.
    ///
    /// Semántica de snapshot: execute_arcpy y DDP operan sobre una COPIA
    /// (SaveAsDocument) del documento vivo — leen su estado real, pero los cambios
    /// al DOCUMENTO se descartan; las escrituras a datos en disco sí son reales.
    /// </summary>
    internal static class PythonHandlers
    {
        private static readonly TimeSpan SubprocessTimeout = TimeSpan.FromSeconds(1800);
        private static readonly TimeSpan StaStepTimeout = TimeSpan.FromSeconds(60);

        // Copiar el documento (SaveAsDocument) es lo unico que puede tardar de verdad
        // en el hilo STA: un mxd con decenas de capas y rasters pesados se va a varios
        // minutos, y durante ese rato ArcMap parece colgado y el puente, muerto.
        private static readonly TimeSpan SnapshotTimeout = TimeSpan.FromSeconds(600);

        private const string AvisoSnapshot =
            "execute_arcpy corre fuera del proceso de ArcMap, sobre una COPIA del documento: lecturas, "
            + "análisis y exports operan sobre el estado real, pero los cambios al mxd "
            + "NO afectan a la sesión viva (usa las tools nativas para eso).";

        private const string AvisoSnapshotDisco =
            " La copia se ha tomado del .mxd GUARDADO EN DISCO, que es instantáneo y no ocupa ArcMap: "
            + "si has cambiado algo en la sesión y no lo has guardado, ese cambio no está aquí. "
            + "Para incluirlo, guarda el documento (save_mxd) o repite con serializar_sesion=true.";

        private static string PythonExe()
        {
            string exe = Environment.GetEnvironmentVariable("ARCMAP_PYTHON27");
            if (string.IsNullOrEmpty(exe))
                exe = @"C:\Python27\ArcGIS10.5\python.exe";
            if (!File.Exists(exe))
                throw new InvalidOperationException(
                    "No se encuentra el Python 2.7 de ArcGIS (" + exe + "). "
                    + "Define ARCMAP_PYTHON27 con la ruta correcta.");
            return exe;
        }

        private static string WorkDir()
        {
            string dir = Path.Combine(Path.GetTempPath(), "arcmap-mcp");
            Directory.CreateDirectory(dir);
            return dir;
        }

        /// <summary>Extrae el runner embebido a %TEMP%\arcmap-mcp\runner.py (cada
        /// llamada: barato y a prueba de versiones de DLL conviviendo).</summary>
        private static string ExtraerRunner()
        {
            string destino = Path.Combine(WorkDir(), "runner.py");
            Assembly asm = Assembly.GetExecutingAssembly();
            using (Stream s = asm.GetManifestResourceStream("ArcmapMcp.AddIn.Python.runner.py"))
            {
                if (s == null)
                    throw new InvalidOperationException(
                        "Recurso embebido Python\\runner.py ausente del ensamblado (bug de build).");
                using (FileStream f = File.Create(destino))
                    s.CopyTo(f);
            }
            return destino;
        }

        /// <summary>Copia del documento vivo a %TEMP% (SaveAsDocument en STA).
        /// Se cronometra siempre: si un documento tarda, el log lo dice en vez de
        /// dejar la impresion de que el puente se ha caido.</summary>
        private static string Snapshot(bool serializarSesion)
        {
            string ruta = Path.Combine(WorkDir(), "snap_" + Guid.NewGuid().ToString("N") + ".mxd");

            // Vía barata y SEGURA por defecto: copiar el .mxd del disco.
            // SaveAsDocument es la única parte de execute_arcpy que corre DENTRO de
            // ArcMap, y serializar un documento con capas pesadas o fuentes rotas
            // puede tumbar el proceso entero (el subproceso arcpy, en cambio, muere
            // solo). Copiar un fichero no puede hacer eso. El precio: no incluye los
            // cambios que la sesión aún no ha guardado, y eso se avisa al llamante.
            string origen = serializarSesion ? null : SnapshotDesdeDisco();
            if (origen != null)
            {
                var relojDisco = Stopwatch.StartNew();
                File.Copy(origen, ruta, true);
                relojDisco.Stop();
                Log.Info("Documento copiado del disco en " + relojDisco.Elapsed.TotalSeconds.ToString("0.0")
                         + " s (sin ocupar ArcMap): " + origen);
                return ruta;
            }

            Log.Info("Copiando el documento a " + ruta + " (ArcMap queda ocupado mientras dura)");
            var reloj = Stopwatch.StartNew();
            JObject r = StaDispatcher.Invoke(delegate
            {
                IApplication app = ArcSession.App();
                app.SaveAsDocument(ruta, true); // true = copia: el doc activo no cambia
                return Protocol.Result(new JObject());
            }, SnapshotTimeout);
            reloj.Stop();

            if (!(bool)r["ok"])
            {
                Log.Error("Copia del documento fallida tras " + reloj.Elapsed.TotalSeconds.ToString("0.0")
                          + " s: " + (string)r["error"]);
                throw new InvalidOperationException(
                    "No se pudo copiar el documento (" + reloj.Elapsed.TotalSeconds.ToString("0.0") + " s): "
                    + (string)r["error"]
                    + ". Si el mxd es grande, usa execute_arcpy con usar_documento=false cuando tu código "
                    + "no necesite mxd ni df.");
            }

            long mb = 0;
            try { mb = new FileInfo(ruta).Length / (1024 * 1024); } catch { }
            Log.Info("Documento copiado en " + reloj.Elapsed.TotalSeconds.ToString("0.0") + " s (" + mb + " MB)");
            return ruta;
        }

        /// <summary>
        /// Ruta del .mxd tal y como está EN DISCO, si existe. ArcObjects no permite
        /// consultar si el documento tiene cambios sin guardar (IDocumentDirty2 solo
        /// deja marcarlo), así que la diferencia se comunica en el aviso del
        /// resultado en vez de intentar adivinarla.
        /// </summary>
        private static string SnapshotDesdeDisco()
        {
            JObject r = StaDispatcher.Invoke(delegate
            {
                IApplication app = ArcSession.App();
                return Protocol.Result(new JObject { ["ruta"] = ArcSession.MxdPath(app) });
            }, StaStepTimeout);

            if (!(bool)r["ok"]) return null;
            string mxd = (string)r["result"]["ruta"];
            if (string.IsNullOrEmpty(mxd) || !File.Exists(mxd))
            {
                Log.Info("El documento no está guardado en disco: hay que serializarlo desde ArcMap.");
                return null;
            }
            return mxd;
        }

        /// <summary>Lanza el runner con el job y devuelve su JSON de salida.
        /// Timeout duro con Kill: sin zombies de python.exe.</summary>
        private static JObject RunJob(string op, JObject parameters, string mxdSnapshot)
        {
            string runner = ExtraerRunner();
            string stamp = Guid.NewGuid().ToString("N");
            string jobPath = Path.Combine(WorkDir(), "job_" + stamp + ".json");
            string outPath = Path.Combine(WorkDir(), "out_" + stamp + ".json");

            var job = new JObject { ["op"] = op, ["params"] = parameters ?? new JObject() };
            if (mxdSnapshot != null)
                job["mxd"] = mxdSnapshot;
            // UTF-8 sin BOM por fichero (nunca stdout: la consola Py2.7 en cp1252
            // rompería ñ/tildes, igual que un header '# -*- coding -*-' mal puesto).
            File.WriteAllText(jobPath, job.ToString(Formatting.None), new UTF8Encoding(false));

            // Ante salida ilegible se CONSERVAN job y out: son la única evidencia
            // para diagnosticar (se borraban siempre, y con ellos la pista).
            bool conservarEvidencia = false;
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = PythonExe(),
                    Arguments = "\"" + runner + "\" \"" + jobPath + "\" \"" + outPath + "\"",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardError = true,
                    RedirectStandardOutput = true,
                };
                int exitCode;
                string stderr;
                using (Process p = Process.Start(psi))
                {
                    // AMBOS flujos en asíncrono: leer uno solo con ReadToEnd cuelga el
                    // subprocess si el otro pipe se llena (~4 KB de mensajes de arcpy).
                    var salida = new StringBuilder();
                    var errores = new StringBuilder();
                    p.OutputDataReceived += delegate (object s, DataReceivedEventArgs e)
                    { if (e.Data != null) salida.AppendLine(e.Data); };
                    p.ErrorDataReceived += delegate (object s, DataReceivedEventArgs e)
                    { if (e.Data != null) errores.AppendLine(e.Data); };
                    p.BeginOutputReadLine();
                    p.BeginErrorReadLine();

                    if (!p.WaitForExit((int)SubprocessTimeout.TotalMilliseconds))
                    {
                        try { p.Kill(); } catch { /* ya muerto */ }
                        return Protocol.Error("Timeout (" + SubprocessTimeout.TotalSeconds
                            + "s) del subprocess arcpy; proceso terminado (sin zombies).");
                    }
                    exitCode = p.ExitCode;
                    stderr = errores.ToString();
                    if (salida.Length > 0)
                        Log.Info("runner '" + op + "' stdout: " + Recortar(salida.ToString()));
                }

                if (!File.Exists(outPath))
                    return Protocol.Error("El runner arcpy no produjo salida (exit "
                        + exitCode + "). stderr: " + Recortar(stderr));

                string texto = File.ReadAllText(outPath, Encoding.UTF8);
                if (string.IsNullOrEmpty(texto.Trim()))
                {
                    conservarEvidencia = true;
                    Log.Info("runner '" + op + "' salida VACÍA; evidencia en " + jobPath + " / " + outPath);
                    return Protocol.Error("El runner arcpy terminó (exit " + exitCode
                        + ") dejando la salida VACÍA: la operación no llegó a completarse. "
                        + "stderr: " + Recortar(stderr)
                        + " | evidencia conservada: " + jobPath + " y " + outPath);
                }

                JObject respuesta;
                try
                {
                    respuesta = JObject.Parse(texto);
                }
                catch (Exception ex)
                {
                    conservarEvidencia = true;
                    Log.Info("runner '" + op + "' salida ILEGIBLE; evidencia en " + jobPath + " / " + outPath);
                    return Protocol.Error("El runner arcpy devolvió una salida ilegible (exit "
                        + exitCode + "): " + ex.Message
                        + " | primeros bytes: " + Recortar(texto.Length > 200 ? texto.Substring(0, 200) : texto)
                        + " | stderr: " + Recortar(stderr)
                        + " | evidencia conservada: " + jobPath + " y " + outPath);
                }
                if (respuesta["ok"] == null)
                {
                    conservarEvidencia = true;
                    return Protocol.Error("El runner arcpy devolvió un JSON sin campo 'ok' (exit "
                        + exitCode + "). Evidencia conservada: " + outPath);
                }
                // Sobre de error estándar del runner (error + traceback) o resultado.
                return respuesta;
            }
            finally
            {
                if (!conservarEvidencia)
                {
                    Borrar(jobPath);
                    Borrar(outPath);
                }
            }
        }

        private static string Recortar(string s)
        {
            if (string.IsNullOrEmpty(s)) return "(vacío)";
            s = s.Trim();
            return s.Length <= 2000 ? s : s.Substring(s.Length - 2000);
        }

        private static void Borrar(string ruta)
        {
            try { if (ruta != null && File.Exists(ruta)) File.Delete(ruta); } catch { }
        }

        /// <summary>Job de documento: snapshot + runner + limpieza del snapshot.</summary>
        private static JObject RunJobConSnapshot(string op, JObject parameters, bool serializarSesion = false)
        {
            string snap = Snapshot(serializarSesion);
            try
            {
                return RunJob(op, parameters, snap);
            }
            finally
            {
                Borrar(snap);
            }
        }

        /// <summary>Añade la salida al mapa: best-effort, nunca rompe la
        /// tool. Corre en STA sobre la sesión viva.</summary>
        private static bool AddToMap(string ruta)
        {
            JObject r = StaDispatcher.Invoke(delegate
            {
                IMxDocument doc;
                IMap map = MapHandlers.FocusMap(out doc);
                ILayer lyr = DataAccess.CrearCapaDesdeRuta(ruta);
                ((IMapLayers)map).InsertLayer(lyr, true, 0);
                doc.UpdateContents();
                doc.ActiveView.Refresh();
                return Protocol.Result(new JObject());
            }, StaStepTimeout);
            if (!(bool)r["ok"])
                Log.Info("anadir_al_mapa best-effort falló para '" + ruta + "': " + (string)r["error"]);
            return (bool)r["ok"];
        }

        // ------------------------------------------------------------------ //
        // Handlers (nombre de comando y contrato JSON de los schemas del servidor MCP).
        // ------------------------------------------------------------------ //

        /// <summary>
        /// ¿El código necesita el documento? Solo si menciona `mxd`, `df` o el módulo
        /// de mapping. Copiar el .mxd de una sesión cargada cuesta segundos o minutos,
        /// y la mayoría del arcpy útil (geoprocesar rásters, recorrer tablas, calcular
        /// índices) trabaja sobre ficheros en disco y no lo toca.
        /// </summary>
        private static bool NecesitaDocumento(string code)
        {
            if (string.IsNullOrEmpty(code)) return false;
            return Regex.IsMatch(code, @"\b(mxd|df|MAP|mapping)\b");
        }

        public static JObject ExecuteArcpy(JObject parameters)
        {
            // El usuario manda: usar_documento fuerza el comportamiento en ambos
            // sentidos. Sin él, se decide leyendo el código.
            bool conDocumento;
            JToken explicito = parameters["usar_documento"];
            if (explicito != null && explicito.Type != JTokenType.Null)
                conDocumento = (bool)explicito;
            else
                conDocumento = NecesitaDocumento((string)parameters["code"]);

            // serializar_sesion fuerza la vía SaveAsDocument (la única que recoge
            // cambios sin guardar, y la única capaz de tumbar ArcMap).
            JToken serializar = parameters["serializar_sesion"];
            bool serializarSesion = serializar != null && serializar.Type != JTokenType.Null && (bool)serializar;

            JObject r = conDocumento
                ? RunJobConSnapshot("execute_code", parameters, serializarSesion)
                : RunJob("execute_code", parameters, null);

            if ((bool)r["ok"])
                r["result"]["aviso"] = conDocumento
                    ? (serializarSesion ? AvisoSnapshot : AvisoSnapshot + AvisoSnapshotDisco)
                    : "Ejecutado SIN copiar el documento (el código no usa mxd ni df): más rápido y sin "
                      + "ocupar ArcMap. Si necesitas la sesión, pasa usar_documento=true.";
            return r;
        }

        public static JObject ListDdp(JObject parameters)
        {
            return RunJobConSnapshot("list_ddp", parameters);
        }

        public static JObject ExportDdp(JObject parameters)
        {
            return RunJobConSnapshot("export_ddp", parameters);
        }

        /// <summary>goto_ddp_page APROXIMADO: el runner calcula el extent de la página
        /// sobre el snapshot y aquí se aplica al data frame vivo. La sesión viva NO
        /// cambia de página de atlas (DDP no existe en .NET): textos dinámicos y
        /// definition queries por página no se actualizan — se avisa siempre.</summary>
        public static JObject GotoDdpPage(JObject parameters)
        {
            JObject r = RunJobConSnapshot("ddp_page_extent", parameters);
            if (!(bool)r["ok"])
                return r;
            JObject info = (JObject)r["result"];
            JArray ext = (JArray)info["extent"];

            JObject aplicado = StaDispatcher.Invoke(delegate
            {
                IMxDocument doc;
                IMap map = MapHandlers.FocusMap(out doc);
                IEnvelope env = new EnvelopeClass();
                env.PutCoords((double)ext[0], (double)ext[1], (double)ext[2], (double)ext[3]);
                ((IActiveView)map).Extent = env;
                doc.ActiveView.Refresh();
                return Protocol.Result(new JObject { ["escala"] = map.MapScale });
            }, StaStepTimeout);
            if (!(bool)aplicado["ok"])
                return aplicado;

            return Protocol.Result(new JObject
            {
                ["page_id"] = info["page_id"],
                ["valor"] = info["valor"],
                ["escala"] = aplicado["result"]["escala"],
                ["aviso"] = "Aproximación: se aplica el ENCUADRE de la página al data frame "
                    + "vivo, pero el atlas de la sesión no cambia de página (DDP solo existe en "
                    + "arcpy): textos dinámicos y queries por página no se actualizan. Para "
                    + "exportar planos usa export_ddp, que sí pagina de verdad.",
            });
        }

        /// <summary>Fábrica de los 5 handlers ambientales de datos-en-disco: runner
        /// out-of-process + anadir_al_mapa nativo sobre la sesión viva al volver.</summary>
        private static JObject Ambiental(string op, JObject parameters, params string[] camposSalida)
        {
            bool anadir = parameters["anadir_al_mapa"] == null
                || parameters["anadir_al_mapa"].Type == JTokenType.Null
                || (bool)parameters["anadir_al_mapa"];
            JObject r = RunJob(op, parameters, null);
            if (!(bool)r["ok"] || !anadir)
                return r;
            JObject res = (JObject)r["result"];
            string salida = null;
            foreach (string campo in camposSalida)
            {
                if (res[campo] != null && res[campo].Type == JTokenType.String)
                {
                    salida = (string)res[campo];
                    break;
                }
            }
            if (salida != null)
                res["anadida_al_mapa"] = AddToMap(salida);
            return r;
        }

        public static JObject RasterIndex(JObject parameters)
        {
            return Ambiental("raster_index", parameters, "salida");
        }

        public static JObject Hydrology(JObject parameters)
        {
            return Ambiental("hydrology", parameters, "salida");
        }

        public static JObject Contours(JObject parameters)
        {
            return Ambiental("contours", parameters, "salida");
        }

        public static JObject TopographicProfile(JObject parameters)
        {
            return Ambiental("topographic_profile", parameters, "salida");
        }

        public static JObject LeastCostPath(JObject parameters)
        {
            return Ambiental("least_cost_path", parameters, "salida");
        }

        // calculate_geometry NO va por subprocess: la sesión viva mantiene un schema
        // lock sobre las fuentes cargadas en la TOC y AddGeometryAttributes añade
        // campos → nativo in-process en GeoprocessingHandlers.CalculateGeometry.
    }
}
