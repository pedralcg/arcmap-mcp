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
        private static readonly TimeSpan SubprocessTimeout = LeerTimeout("ARCMAP_SUBPROCESS_TIMEOUT", 1800);
        private static readonly TimeSpan StaStepTimeout = TimeSpan.FromSeconds(60);

        // Margen para que los pipes del runner den EOF una vez el proceso ya ha salido.
        // Generoso para el caso normal (milisegundos) y acotado para el patológico: un
        // nieto que heredó la salida y no muere. Ver el uso, más abajo.
        private static readonly TimeSpan FlushSalidaTimeout = TimeSpan.FromSeconds(10);

        // PIDs de los runners arrancados y aún no terminados. Se guardan PIDs y no
        // objetos Process porque el Process se libera al salir de su `using`, y esto
        // tiene que sobrevivir a eso. Sirven para un caso concreto: al cerrarse ArcMap,
        // un runner vivo IMPIDE que el proceso acabe de salir, y ArcMap se queda sin
        // ventana pero vivo, sujetando el puerto (incidente del 2026-08-27).
        //
        // Con cada PID va su HORA DE ARRANQUE, y no es adorno: Windows reutiliza los
        // PID, y desde que al parar se mata el ÁRBOL entero (taskkill /T), acertar con
        // un PID reciclado ya no se lleva un proceso ajeno sino su jerarquía completa.
        // Un PID solo es "el runner" si además arrancó cuando arrancó el runner.
        private static readonly System.Collections.Generic.Dictionary<int, DateTime> _runnersVivos =
            new System.Collections.Generic.Dictionary<int, DateTime>();

        private static int RegistrarRunner(Process p)
        {
            try
            {
                int pid = p.Id;
                DateTime arranque = p.StartTime;
                lock (_runnersVivos) _runnersVivos[pid] = arranque;
                return pid;
            }
            catch { return 0; }   // proceso ya muerto: nada que registrar
        }

        private static void OlvidarRunner(int pid)
        {
            if (pid == 0) return;
            lock (_runnersVivos) _runnersVivos.Remove(pid);
        }

        /// <summary>
        /// Mata los runners que sigan vivos. Se llama al parar el puente: si ArcMap se
        /// está cerrando, un runner vivo lo deja a medio salir — vivo, sin ventana y
        /// sujetando el puerto—, que es el cuadro del ArcMap zombi. Devuelve cuántos
        /// mató. El trabajo que estuviera haciendo se pierde, pero al cerrarse ArcMap ya
        /// no había nadie que fuera a leer su resultado.
        /// </summary>
        public static int MatarRunnersVivos()
        {
            var pids = new System.Collections.Generic.List<System.Collections.Generic.KeyValuePair<int, DateTime>>();
            lock (_runnersVivos)
            {
                pids.AddRange(_runnersVivos);
                _runnersVivos.Clear();
            }
            int muertos = 0;
            foreach (var registrado in pids)
            {
                int pid = registrado.Key;
                try
                {
                    using (Process p = Process.GetProcessById(pid))
                    {
                        if (p.HasExited) continue;
                        // ¿Sigue siendo NUESTRO runner, o Windows ya le dio ese PID a otro?
                        // Si no se puede leer la hora de arranque, no se mata: ante la duda,
                        // un runner huérfano es peor que molesto pero mejor que matar un
                        // árbol ajeno.
                        if (p.StartTime != registrado.Value)
                        {
                            Log.Info("PID " + pid + " ya no es el runner arcpy (arrancó a otra hora):"
                                     + " Windows lo ha reutilizado. No se toca.");
                            continue;
                        }
                        // Árbol entero y no solo el hijo: los procesos auxiliares de arcpy
                        // son justo los que dejan a ArcMap a medio salir. Ver MatarArbol.
                        MatarArbol(pid, "runner arcpy");
                        try { if (!p.HasExited) p.Kill(); } catch { /* ya lo mató taskkill */ }
                        muertos++;
                        Log.Info("Runner arcpy (PID " + pid + ") terminado al parar el puente.");
                    }
                }
                catch { /* ya no existe, o no se deja: no hay nada mejor que hacer */ }
            }
            return muertos;
        }

        // Margen para que taskkill haga su recorrido. Es un proceso que vive milisegundos;
        // el tope solo existe para que un taskkill colgado no bloquee a quien lo llamó.
        private static readonly TimeSpan TaskkillTimeout = TimeSpan.FromSeconds(15);

        /// <summary>
        /// Mata el proceso y TODA su descendencia. `Process.Kill()` mata solo al hijo —en
        /// net45 no existe `Kill(true)`— y arcpy lanza procesos auxiliares que quedan
        /// huérfanos sujetando los pipes de salida: es el mismo nieto del que habla el
        /// comentario largo de RunJob, y no es hipotético.
        ///
        /// Reproducido en aislado sobre .NET Framework 4.0.30319 el 2026-09-20 (cmd → cmd
        /// → ping): con `p.Kill()` del padre el ping seguía vivo; con
        /// `taskkill /PID n /T /F` desapareció el árbol entero, exit 0.
        ///
        /// taskkill en vez de recorrer WMI o Toolhelp: viene con Windows desde XP, hace el
        /// recorrido de padres él solo y no añade dependencias. Se llama SIEMPRE ANTES del
        /// Kill del hijo, que es cuando el árbol aún está entero: matando primero al padre,
        /// el nieto queda reparentado y /T ya no sabría encontrarlo.
        /// Sin redirección de salida a propósito: redirigir y no leer los pipes es
        /// exactamente la forma de colgarse, y ArcMap no tiene consola donde ensuciar.
        /// </summary>
        private static void MatarArbol(int pid, string quien)
        {
            if (pid <= 0) return;
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = Path.Combine(Environment.SystemDirectory, "taskkill.exe"),
                    Arguments = "/PID " + pid + " /T /F",
                    UseShellExecute = false,
                    CreateNoWindow = true,
                };
                using (Process t = Process.Start(psi))
                {
                    if (!t.WaitForExit((int)TaskkillTimeout.TotalMilliseconds))
                    {
                        Log.Error("taskkill sobre " + quien + " (PID " + pid + ") no terminó en "
                                  + TaskkillTimeout.TotalSeconds + " s; se sigue con el Kill del hijo.");
                        return;
                    }
                    // 128 = "no hay tal proceso": murió por su cuenta mientras tanto, no es fallo.
                    if (t.ExitCode != 0 && t.ExitCode != 128)
                        Log.Error("taskkill sobre " + quien + " (PID " + pid + ") devolvió "
                                  + t.ExitCode + ": puede quedar vivo algún proceso auxiliar de arcpy.");
                    else
                        Log.Info("Árbol de procesos de " + quien + " (PID " + pid
                                 + ") terminado con taskkill /T /F.");
                }
            }
            catch (Exception ex)
            {
                Log.Error("No se pudo lanzar taskkill sobre " + quien + " (PID " + pid + ")", ex);
            }
        }

        // execute_arcpy es INTERACTIVO: al otro lado hay alguien esperando la
        // respuesta, no un batch nocturno. Con el techo de 1800 s de los jobs
        // pesados, un execute_arcpy atascado es indistinguible de un cuelgue
        // permanente (el cliente MCP se rinde mucho antes) y no deja ni un error
        // que leer. Ya costó dos sesiones enteras: 2026-07-27 y 2026-07-29.
        // Los jobs legítimamente largos (DDP, hidrología, índices) NO pasan por
        // aquí: conservan SubprocessTimeout.
        // 900 s y no 300: abrir un mxd de 5,9 MB costó 324 s medidos el 2026-07-29, o
        // sea que un tope de 300 mataría un usar_documento=true legítimo. Esto es una
        // RED DE SEGURIDAD contra runners eternos, no un presupuesto de rendimiento:
        // quien informa de si avanza o está pillado es la traza de fase del runner.
        private static readonly TimeSpan ExecuteTimeout = LeerTimeout("ARCMAP_EXEC_TIMEOUT", 900);

        // Cada cuánto se mira la fase del runner mientras corre. 1 s es ruido
        // despreciable frente a jobs de segundos o minutos.
        private static readonly TimeSpan SondeoFase = TimeSpan.FromSeconds(1);

        /// <summary>Timeout en segundos desde variable de entorno, con defecto.
        /// Un valor ilegible o &lt;= 0 no rompe el add-in: se usa el defecto.</summary>
        private static TimeSpan LeerTimeout(string variable, int defectoSegundos)
        {
            int segundos;
            string bruto = Environment.GetEnvironmentVariable(variable);
            if (string.IsNullOrEmpty(bruto) || !int.TryParse(bruto, out segundos) || segundos <= 0)
                segundos = defectoSegundos;
            return TimeSpan.FromSeconds(segundos);
        }

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

        // Cómo se ha obtenido la copia. Viaja al resultado en `snapshot_via` porque no es
        // un detalle de implementación: decide si las capas del snapshot resuelven o no.
        public const string ViaDiscoTemp = "disco_temp";
        public const string ViaDiscoJunto = "disco_junto_al_original";
        public const string ViaSesion = "sesion_serializada";

        // Prefijo de los snapshots que se dejan JUNTO AL ORIGINAL. Empieza por '~' para
        // que ordene al final y se lea como lo que es, y lleva el nombre del add-in para
        // que quien encuentre uno huérfano sepa de dónde salió.
        private const string PrefijoJunto = "~arcmap-mcp-snap_";

        // La ruta del intérprete la resuelve Python27: estaba escrita a mano aquí
        // (C:\Python27\ArcGIS10.5\python.exe) y en otros dos sitios, y en 10.6-10.8
        // execute_arcpy fallaba siempre salvo con ARCMAP_PYTHON27 definida a mano.

        private static string WorkDir()
        {
            string dir = Path.Combine(Path.GetTempPath(), "arcmap-mcp");
            Directory.CreateDirectory(dir);
            return dir;
        }

        // Ruta del runner ya extraído en esta sesión. El nombre lleva el hash del
        // contenido, así que una vez escrito no hay nada que rehacer.
        private static string _runnerExtraido;

        /// <summary>
        /// Extrae el runner embebido a %TEMP%\arcmap-mcp\runner_&lt;hash8&gt;.py.
        ///
        /// El nombre lleva el HASH DEL CONTENIDO y no es fijo porque %TEMP% es del
        /// usuario, no del add-in: con dos ArcMap abiertos (10.5 y 10.8, por ejemplo) con
        /// versiones distintas del add-in, un `runner.py` fijo reescrito en cada llamada
        /// es una carrera — el segundo pisa el fichero justo cuando el primero lo va a
        /// ejecutar, y uno acaba corriendo el runner del otro. Con el hash en el nombre,
        /// dos contenidos distintos son dos ficheros distintos y no se estorban.
        ///
        /// Y no se reescribe si ya está: File.Create TRUNCA el destino al abrirlo, así que
        /// el fichero pasa por 0 bytes en cada llamada aunque el contenido sea el mismo.
        /// </summary>
        private static string ExtraerRunner()
        {
            if (_runnerExtraido != null && File.Exists(_runnerExtraido))
                return _runnerExtraido;

            byte[] datos;
            Assembly asm = Assembly.GetExecutingAssembly();
            using (Stream s = asm.GetManifestResourceStream("ArcmapMcp.AddIn.Python.runner.py"))
            {
                if (s == null)
                    throw new InvalidOperationException(
                        "Recurso embebido Python\\runner.py ausente del ensamblado (bug de build).");
                using (var ms = new MemoryStream())
                {
                    s.CopyTo(ms);
                    datos = ms.ToArray();
                }
            }

            string destino = Path.Combine(WorkDir(), "runner_" + Hash8(datos) + ".py");
            if (!MismoContenido(destino, datos))
            {
                // Escritura en dos pasos: un fichero temporal propio y un Move, que en el
                // mismo volumen es atómico y FALLA si el destino ya existe. Así, si otro
                // ArcMap llega a la vez, gana uno y el otro se queda con su fichero — que
                // tiene el mismo hash, o sea el mismo contenido.
                string temporal = destino + "." + Guid.NewGuid().ToString("N").Substring(0, 8) + ".tmp";
                File.WriteAllBytes(temporal, datos);
                try { File.Move(temporal, destino); }
                catch (IOException) { Borrar(temporal); }
            }
            _runnerExtraido = destino;
            return destino;
        }

        /// <summary>Huella corta del recurso, solo para nombrar el fichero. No es
        /// seguridad: es identidad de contenido.</summary>
        private static string Hash8(byte[] datos)
        {
            using (var sha = System.Security.Cryptography.SHA1.Create())
            {
                byte[] h = sha.ComputeHash(datos);
                var sb = new StringBuilder(8);
                for (int i = 0; i < 4; i++) sb.Append(h[i].ToString("x2"));
                return sb.ToString();
            }
        }

        private static bool MismoContenido(string ruta, byte[] datos)
        {
            try
            {
                if (!File.Exists(ruta)) return false;
                byte[] actual = File.ReadAllBytes(ruta);
                if (actual.Length != datos.Length) return false;
                for (int i = 0; i < datos.Length; i++)
                    if (actual[i] != datos[i]) return false;
                return true;
            }
            catch { return false; }   // ilegible (otro proceso escribiéndolo): se reescribe
        }

        /// <summary>La copia del documento y por qué vía se consiguió.</summary>
        private sealed class Instantanea
        {
            public readonly string Ruta;
            public readonly string Via;
            /// <summary>Ruta del .mxd ORIGINAL (null si está sin guardar). Viaja al runner
            /// para que el aviso de capas rotas diga de qué documento habla.</summary>
            public readonly string Original;
            public Instantanea(string ruta, string via, string original)
            { Ruta = ruta; Via = via; Original = original; }
        }

        /// <summary>El .mxd tal y como está en disco, con la casilla de rutas relativas.</summary>
        private sealed class DocumentoEnDisco
        {
            public string Ruta;
            public string Carpeta;
            public bool RutasRelativas;
        }

        /// <summary>
        /// Copia del documento vivo. Se cronometra siempre: si un documento tarda, el log
        /// lo dice en vez de dejar la impresión de que el puente se ha caído.
        ///
        /// Vía barata y SEGURA por defecto: copiar el .mxd del disco. SaveAsDocument es la
        /// única parte de execute_arcpy que corre DENTRO de ArcMap, y serializar un
        /// documento con capas pesadas o fuentes rotas puede tumbar el proceso entero (el
        /// subproceso arcpy, en cambio, muere solo). Copiar un fichero no puede hacer eso.
        /// El precio: no incluye los cambios que la sesión aún no ha guardado, y eso se
        /// avisa al llamante.
        ///
        /// PERO dónde se deja la copia no da igual. Si el documento guarda RUTAS RELATIVAS
        /// (Propiedades del documento &gt; "Store relative pathnames"), copiarlo a %TEMP%
        /// deja TODAS sus capas rotas, porque cada fuente se resuelve desde la carpeta del
        /// .mxd. Probado en aislado con arcpy el 2026-09-20: un mxd con relativePaths=True
        /// copiado a otra carpeta devuelve la lista entera en ListBrokenDataSources; con
        /// rutas absolutas, ninguna. Y no falla ruidosamente: export_ddp sacaría el atlas
        /// completo, con su leyenda y sus marcos, y sin un solo dato dentro.
        /// Por eso, con rutas relativas la copia se deja JUNTO AL ORIGINAL (mismo
        /// directorio, nombre propio y oculta, borrada en el finally de RunJobConSnapshot),
        /// y si esa carpeta no deja escribir se cae a SaveAsDocument — que sí recalcula las
        /// rutas al guardar — pero NUNCA a %TEMP%, que se sabe roto.
        /// </summary>
        private static Instantanea Snapshot(bool serializarSesion)
        {
            DocumentoEnDisco doc = serializarSesion ? null : LeerDocumentoEnDisco();
            BarrerRestos(doc == null ? null : doc.Carpeta);

            if (doc != null && !doc.RutasRelativas)
            {
                string destino = Path.Combine(WorkDir(), "snap_" + Guid.NewGuid().ToString("N") + ".mxd");
                var relojDisco = Stopwatch.StartNew();
                File.Copy(doc.Ruta, destino, true);
                relojDisco.Stop();
                Log.Info("Documento copiado del disco a %TEMP% en "
                         + relojDisco.Elapsed.TotalSeconds.ToString("0.0") + " s (sin ocupar ArcMap): "
                         + doc.Ruta + ". Vía '" + ViaDiscoTemp + "': el documento guarda rutas"
                         + " ABSOLUTAS, así que sus capas resuelven desde cualquier carpeta.");
                return new Instantanea(destino, ViaDiscoTemp, doc.Ruta);
            }

            if (doc != null)
            {
                string junto = Path.Combine(doc.Carpeta, PrefijoJunto + Guid.NewGuid().ToString("N") + ".mxd");
                try
                {
                    var relojJunto = Stopwatch.StartNew();
                    File.Copy(doc.Ruta, junto, true);
                    relojJunto.Stop();
                    // Oculto para no ensuciar la carpeta de trabajo del usuario mientras
                    // dura el job. Se ASIGNAN los atributos, no se añade Hidden a los que
                    // haya: File.Copy hereda el ReadOnly del original —un .mxd traído de
                    // red o de un CD lo trae a menudo— y un fichero de solo lectura no se
                    // puede borrar. Comprobado en aislado el 2026-09-20: con
                    // `GetAttributes(x) | Hidden` el File.Delete de después lanza
                    // UnauthorizedAccessException y la copia se queda en la carpeta del
                    // usuario en CADA llamada; asignando Hidden a secas, se borra.
                    // Que no se deje tocar los atributos no es un fallo: Borrar() insiste.
                    try { File.SetAttributes(junto, FileAttributes.Hidden); }
                    catch { }
                    Log.Info("Documento copiado JUNTO AL ORIGINAL en "
                             + relojJunto.Elapsed.TotalSeconds.ToString("0.0") + " s: " + junto
                             + ". Vía '" + ViaDiscoJunto + "', porque el documento guarda rutas"
                             + " RELATIVAS y una copia en %TEMP% dejaría todas sus capas rotas.");
                    return new Instantanea(junto, ViaDiscoJunto, doc.Ruta);
                }
                catch (Exception ex)
                {
                    Borrar(junto);
                    Log.Error("No se ha podido dejar la copia junto al original (" + doc.Carpeta + "): "
                              + ex.Message + ". El documento guarda rutas RELATIVAS, así que NO se cae"
                              + " a %TEMP% (rompería todas sus capas): se serializa desde ArcMap, que"
                              + " recalcula las rutas al guardar, aunque cueste minutos.");
                }
            }

            string ruta = Path.Combine(WorkDir(), "snap_" + Guid.NewGuid().ToString("N") + ".mxd");
            Log.Info("Copiando el documento a " + ruta + " (ArcMap queda ocupado mientras dura)");
            var reloj = Stopwatch.StartNew();
            JObject r = StaDispatcher.Invoke(delegate
            {
                IApplication app = ArcSession.App();
                string original = ArcSession.MxdPath(app);
                app.SaveAsDocument(ruta, true); // true = copia: el doc activo no cambia
                return Protocol.Result(new JObject { ["original"] = original });
            }, SnapshotTimeout, "copiar el documento (SaveAsDocument)");
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
            Log.Info("Documento copiado en " + reloj.Elapsed.TotalSeconds.ToString("0.0") + " s (" + mb + " MB)"
                     + ". Vía '" + ViaSesion + "'.");
            return new Instantanea(ruta, ViaSesion, (string)r["result"]["original"]);
        }

        /// <summary>
        /// El .mxd tal y como está EN DISCO, si existe, y si guarda rutas relativas.
        /// ArcObjects no permite consultar si el documento tiene cambios sin guardar
        /// (IDocumentDirty2 solo deja marcarlo), así que esa diferencia se comunica en el
        /// aviso del resultado en vez de intentar adivinarla.
        ///
        /// `IMxDocument.RelativePaths` es la MISMA casilla que Propiedades del documento
        /// &gt; "Store relative pathnames" (bool de lectura/escritura; también accesible
        /// como IDocumentInfo2.RelativePaths sobre el mismo objeto).
        /// </summary>
        private static DocumentoEnDisco LeerDocumentoEnDisco()
        {
            JObject r = StaDispatcher.Invoke(delegate
            {
                IApplication app = ArcSession.App();
                var o = new JObject { ["ruta"] = ArcSession.MxdPath(app) };
                try
                {
                    IMxDocument doc = app.Document as IMxDocument;
                    if (doc != null) o["relativas"] = doc.RelativePaths;
                }
                catch (Exception ex)
                {
                    o["relativas_error"] = ex.Message;
                }
                return Protocol.Result(o);
            }, StaStepTimeout, "leer la ruta del documento");

            if (!(bool)r["ok"]) return null;
            JObject res = (JObject)r["result"];
            string mxd = (string)res["ruta"];
            if (string.IsNullOrEmpty(mxd) || !File.Exists(mxd))
            {
                Log.Info("El documento no está guardado en disco: hay que serializarlo desde ArcMap.");
                return null;
            }

            string carpeta = null;
            try { carpeta = Path.GetDirectoryName(mxd); } catch { }
            if (string.IsNullOrEmpty(carpeta)) return null;

            JToken rel = res["relativas"];
            bool relativas;
            if (rel != null && rel.Type == JTokenType.Boolean)
            {
                relativas = (bool)rel;
            }
            else
            {
                // No se ha podido leer la casilla: se asume que SÍ son relativas, que es
                // la suposición segura. Si acierta, perfecto; si se equivoca, lo único que
                // pasa es que la copia se deja junto al original en vez de en %TEMP%. Al
                // revés —asumir absolutas— se entregaría un documento con TODAS las capas
                // rotas sin que nada fallara por el camino.
                relativas = true;
                Log.Error("No se ha podido leer si el documento usa rutas relativas ("
                          + ((string)res["relativas_error"] ?? "documento no accesible")
                          + "): se asume que SÍ, que es lo seguro.");
            }

            return new DocumentoEnDisco { Ruta = mxd, Carpeta = carpeta, RutasRelativas = relativas };
        }

        // Carpetas de documento ya barridas en esta sesión, y si %TEMP% ya lo está. El
        // barrido se hace UNA vez por carpeta y no en cada Snapshot: en una carpeta de red
        // con miles de ficheros, listar no es gratis y esto está en el camino caliente.
        private static readonly System.Collections.Generic.HashSet<string> _carpetasBarridas =
            new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
        private static bool _tempBarrido;

        /// <summary>
        /// Borra restos de sesiones anteriores. Si ArcMap muere a mitad de un
        /// execute_arcpy, el `finally` que borra el snapshot no llega a correr y queda un
        /// fichero suelto — y desde que la copia puede caer JUNTO AL ORIGINAL, en la
        /// carpeta de trabajo del usuario, que es donde molesta de verdad. En %TEMP% se
        /// acumula además la evidencia que se conserva a propósito ante una salida
        /// ilegible, y esa evidencia incluye el CÓDIGO DEL USUARIO: no puede crecer sin
        /// límite.
        ///
        /// Los umbrales no son decorativos. 1 día junto al original porque un snapshot
        /// recién creado puede ser de otro ArcMap trabajando ahora mismo, y borrárselo en
        /// mitad del job le rompería la ejecución. 7 días en %TEMP% porque ahí vive la
        /// evidencia de diagnóstico y una semana es lo que tarda alguien en mirarla.
        ///
        /// Best-effort absoluto: un barrido que falle no puede tumbar el snapshot que va
        /// detrás.
        /// </summary>
        private static void BarrerRestos(string carpetaDocumento)
        {
            try
            {
                bool barrerTemp;
                lock (_carpetasBarridas)
                {
                    barrerTemp = !_tempBarrido;
                    _tempBarrido = true;
                }
                if (barrerTemp)
                {
                    DateTime limite = DateTime.UtcNow.AddDays(-7);
                    string temp = WorkDir();
                    foreach (string patron in new[] { "snap_*.mxd", "job_*.json", "out_*.json", "out_*.json.fase" })
                        BorrarViejos(temp, patron, limite);
                }
            }
            catch (Exception ex)
            {
                Log.Info("Barrido de %TEMP% fallido (se sigue igual): " + ex.Message);
            }

            if (string.IsNullOrEmpty(carpetaDocumento)) return;
            try
            {
                bool primeraVez;
                lock (_carpetasBarridas) primeraVez = _carpetasBarridas.Add(carpetaDocumento);
                if (!primeraVez) return;
                BorrarViejos(carpetaDocumento, PrefijoJunto + "*.mxd", DateTime.UtcNow.AddDays(-1));
            }
            catch (Exception ex)
            {
                Log.Info("Barrido junto al documento fallido (se sigue igual): " + ex.Message);
            }
        }

        private static void BorrarViejos(string carpeta, string patron, DateTime limiteUtc)
        {
            if (!Directory.Exists(carpeta)) return;
            int borrados = 0;
            foreach (string f in Directory.GetFiles(carpeta, patron))
            {
                try
                {
                    if (File.GetLastWriteTimeUtc(f) >= limiteUtc) continue;
                    File.SetAttributes(f, FileAttributes.Normal);  // los de junto al original van ocultos
                    File.Delete(f);
                    borrados++;
                }
                catch { /* en uso por otro ArcMap, o sin permiso: ya se intentará mañana */ }
            }
            if (borrados > 0)
                Log.Info("Limpieza: " + borrados + " resto(s) '" + patron + "' anteriores a "
                         + limiteUtc.ToLocalTime().ToString("yyyy-MM-dd HH:mm") + " borrados de " + carpeta);
        }

        /// <summary>
        /// Medido el 2026-09-26: con ArcMap abierto, CIERTOS documentos bloquean al arcpy de
        /// fuera en cuanto los carga (ListDataFrames): se queda esperando una llamada COM a
        /// este ArcMap que no vuelve nunca. Con ArcMap cerrado el mismo documento abre en ~1 s,
        /// y matar ArcMap lo desbloquea al instante. No es el add-in: sin él pasa igual. Lo
        /// que lo dispara dentro del documento no está localizado (descartados capas, WMS,
        /// https e imágenes del layout). Y deja secuela: al cerrar ArcMap, la ventana se va
        /// pero el proceso no termina.
        /// </summary>
        private const string AvisoBloqueoCom =
            " Si el documento abre en segundos con ArcMap CERRADO, puede ser un bloqueo conocido"
            + " de ArcMap 10.5: con ArcMap abierto, algunos .mxd dejan al arcpy de fuera esperando"
            + " a este ArcMap para siempre. Subir el timeout no sirve; ábrelo con ArcMap cerrado."
            + " Ojo: tras esto, al cerrar ArcMap el proceso puede quedarse vivo y sin ventana; no"
            + " hay nada que guardar, termínalo por PID.";

        /// <summary>Lanza el runner con el job y devuelve su JSON de salida.
        /// Timeout duro con Kill: sin zombies de python.exe.</summary>
        private static JObject RunJob(string op, JObject parameters, string mxdSnapshot,
                                      TimeSpan? timeout = null, string mxdOriginal = null)
        {
            TimeSpan tope = timeout ?? SubprocessTimeout;
            string runner = ExtraerRunner();
            string stamp = Guid.NewGuid().ToString("N");
            string jobPath = Path.Combine(WorkDir(), "job_" + stamp + ".json");
            string outPath = Path.Combine(WorkDir(), "out_" + stamp + ".json");
            // El runner deriva esta misma ruta de su segundo argumento: no hace falta
            // pasarla, y así el contrato de argumentos del runner no cambia.
            string fasePath = outPath + ".fase";

            var job = new JObject { ["op"] = op, ["params"] = parameters ?? new JObject() };
            if (mxdSnapshot != null)
                job["mxd"] = mxdSnapshot;
            if (mxdOriginal != null)
                job["mxd_original"] = mxdOriginal;
            // UTF-8 sin BOM por fichero (nunca stdout: la consola Py2.7 en cp1252
            // rompería ñ/tildes, igual que un header '# -*- coding -*-' mal puesto).
            File.WriteAllText(jobPath, job.ToString(Formatting.None), new UTF8Encoding(false));

            // Ante salida ilegible se CONSERVAN job y out: son la única evidencia
            // para diagnosticar (se borraban siempre, y con ellos la pista).
            bool conservarEvidencia = false;
            int pidRunner = 0;
            try
            {
                var psi = new ProcessStartInfo
                {
                    FileName = Python27.Exe(),
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
                    pidRunner = RegistrarRunner(p);
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

                    // Sondeo en vez de una espera ciega: el runner va anotando su fase
                    // y aquí se registra cada cambio. Así el log dice DÓNDE se quedó
                    // pillado, que desde fuera es indistinguible de estar trabajando.
                    var reloj = Stopwatch.StartNew();
                    string fase = null;
                    TimeSpan faseDesde = TimeSpan.Zero;
                    bool termino = false;
                    while (reloj.Elapsed < tope)
                    {
                        if (p.WaitForExit((int)SondeoFase.TotalMilliseconds)) { termino = true; break; }
                        string nueva = LeerFase(fasePath);
                        if (nueva != null && nueva != fase)
                        {
                            fase = nueva;
                            faseDesde = reloj.Elapsed;
                            Log.Info("runner '" + op + "' [" + reloj.Elapsed.TotalSeconds.ToString("0")
                                     + " s] fase: " + fase);
                        }
                    }

                    if (!termino)
                    {
                        string donde = fase == null
                            ? "no llegó a anotar ninguna fase (murió al arrancar el intérprete)"
                            : "atascado en la fase '" + fase + "' desde hace "
                              + (reloj.Elapsed - faseDesde).TotalSeconds.ToString("0") + " s";
                        // El ÁRBOL, no solo el hijo: arcpy lanza procesos auxiliares que
                        // sobreviven a un p.Kill() pelado y se quedan sujetando los pipes
                        // heredados. Primero taskkill /T (que necesita al padre vivo para
                        // encontrar la descendencia) y después el Kill como red.
                        MatarArbol(PidCrudo(p), "runner '" + op + "'");
                        try { if (!p.HasExited) p.Kill(); } catch { /* ya muerto */ }
                        Log.Error("runner '" + op + "' superó el timeout de " + tope.TotalSeconds
                                  + " s: " + donde + ". Proceso " + PidSeguro(p)
                                  + " y su árbol terminados. Evidencia: " + jobPath);
                        conservarEvidencia = true;
                        return Protocol.Error("Timeout (" + tope.TotalSeconds
                            + " s) del subprocess arcpy en '" + op + "': " + donde
                            + ". Proceso terminado (sin zombies)."
                            + (fase != null && fase.StartsWith("abriendo documento")
                                ? " Se quedó ABRIENDO EL DOCUMENTO. Abrir en sí NO es caro (medido 0,7 s"
                                  + " en un .mxd de 36 capas). Mira si hay python.exe de ArcGIS"
                                  + " huérfanos de llamadas anteriores y si responden las unidades de red"
                                  + " de las capas antes de subir el timeout, que solo alarga la espera."
                                  + " Si tu código no usa mxd ni df, pasa usar_documento=false."
                                : op == "execute_code"
                                    ? " Sube ARCMAP_EXEC_TIMEOUT (segundos) si la operación es legítimamente larga."
                                    : " Sube ARCMAP_SUBPROCESS_TIMEOUT (segundos) si la operación es legítimamente larga.")
                            + (fase != null && (fase.StartsWith("abriendo documento") || fase.StartsWith("ejecutando"))
                                ? AvisoBloqueoCom : "")
                            + " Evidencia conservada: " + jobPath);
                    }
                    // Con salida redirigida en asíncrono hay que rematar con un WaitForExit
                    // final para que los buffers acaben de vaciarse. PERO **nunca sin
                    // argumentos**: esa llamada no espera a que muera el hijo (que aquí ya
                    // murió), espera al EOF de LOS DOS PIPES, y un NIETO que heredó los
                    // descriptores y sigue vivo impide ese EOF para siempre. Reproducido en
                    // aislado el 2026-08-28 sobre .NET Framework 4.8: hijo muerto a los
                    // 0,0 s, `WaitForExit()` bloqueado 12,1 s, exactamente lo que vivió el
                    // nieto. Con un nieto que no muera, bloquea indefinidamente, el handler
                    // no vuelve y el puente se queda inservible. arcpy lanza procesos
                    // auxiliares, así que el nieto no es hipotético.
                    // Se acota: como mucho FlushSalidaTimeout esperando el vaciado. Si no
                    // llega, se sigue con lo que haya — perder unas líneas de stderr es
                    // barato; colgar el puente, no.
                    if (!p.WaitForExit((int)FlushSalidaTimeout.TotalMilliseconds))
                        Log.Error("runner '" + op + "' ya terminó, pero sus pipes no dieron EOF en "
                                  + FlushSalidaTimeout.TotalSeconds + " s: algo heredó la salida y sigue"
                                  + " vivo (proceso auxiliar de arcpy). Se continúa; la salida capturada"
                                  + " puede estar incompleta.");
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
                OlvidarRunner(pidRunner);
                // La traza de fase es de usar y tirar salvo que haya que diagnosticar:
                // cuando se conserva evidencia, dice dónde se quedó el runner.
                if (!conservarEvidencia)
                {
                    Borrar(jobPath);
                    Borrar(outPath);
                    Borrar(fasePath);
                }
            }
        }

        private static string Recortar(string s)
        {
            if (string.IsNullOrEmpty(s)) return "(vacío)";
            s = s.Trim();
            return s.Length <= 2000 ? s : s.Substring(s.Length - 2000);
        }

        /// <summary>
        /// Lee la fase que el runner dice estar ejecutando. Best-effort por diseño: el
        /// fichero se está reescribiendo desde el otro proceso, así que una lectura a
        /// medias es normal y se ignora (en el siguiente sondeo se lee bien). Jamás
        /// puede tumbar el job que está vigilando.
        /// </summary>
        private static string LeerFase(string ruta)
        {
            try
            {
                if (!File.Exists(ruta)) return null;
                string texto;
                using (var fs = new FileStream(ruta, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                using (var sr = new StreamReader(fs, Encoding.UTF8))
                    texto = sr.ReadToEnd();
                if (string.IsNullOrEmpty(texto.Trim())) return null;
                JObject j = JObject.Parse(texto);
                string fase = (string)j["fase"];
                if (string.IsNullOrEmpty(fase)) return null;
                string detalle = (string)j["detalle"];
                return string.IsNullOrEmpty(detalle) ? fase : fase + " (" + detalle + ")";
            }
            catch { return null; }
        }

        /// <summary>PID para el log sin arriesgar una excepción sobre un proceso ya muerto.</summary>
        private static string PidSeguro(Process p)
        {
            try { return "PID " + p.Id; } catch { return "(PID desconocido)"; }
        }

        /// <summary>El PID como número, o 0 si ya no se puede leer.</summary>
        private static int PidCrudo(Process p)
        {
            try { return p.Id; } catch { return 0; }
        }

        /// <summary>Borra un fichero de trabajo. Limpia antes los atributos porque
        /// File.Copy hereda el ReadOnly del original, y un .mxd de solo lectura hacía que
        /// su copia —en %TEMP% o junto al original— no se pudiera borrar nunca.</summary>
        private static void Borrar(string ruta)
        {
            if (ruta == null) return;
            try
            {
                if (!File.Exists(ruta)) return;
                try { File.SetAttributes(ruta, FileAttributes.Normal); } catch { }
                File.Delete(ruta);
            }
            catch { }
        }

        /// <summary>Job de documento: snapshot + runner + limpieza del snapshot.
        /// Añade al resultado `snapshot_via`, que dice de dónde salió la copia: con
        /// rutas relativas la vía decide si las capas del snapshot resuelven o no, y eso
        /// no se puede dejar solo en el log.</summary>
        private static JObject RunJobConSnapshot(string op, JObject parameters, bool serializarSesion = false,
                                                 TimeSpan? timeout = null)
        {
            Instantanea snap = Snapshot(serializarSesion);
            try
            {
                JObject r = RunJob(op, parameters, snap.Ruta, timeout, snap.Original);
                JObject res = r["result"] as JObject;
                if (res != null)
                    res["snapshot_via"] = snap.Via;
                return r;
            }
            finally
            {
                Borrar(snap.Ruta);
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
            }, StaStepTimeout, "añadir la salida al mapa");
            if (!(bool)r["ok"])
                Log.Info("anadir_al_mapa best-effort falló para '" + ruta + "': " + (string)r["error"]);
            return (bool)r["ok"];
        }

        // ------------------------------------------------------------------ //
        // Handlers (nombre de comando y contrato JSON de los schemas del servidor MCP).
        // ------------------------------------------------------------------ //

        // Las señales son, exactamente, los nombres que el runner inyecta en el
        // namespace SOLO cuando hay snapshot: `mxd` y `df`. Si el código no nombra
        // ninguno, no puede estar usando el documento.
        //
        // `MAP` y `mapping` contaban también hasta la 2.12.0, y era un error: el runner
        // los inyecta SIEMPRE, con o sin copia. Un código que abre otros .mxd por ruta
        // (`MAP.MapDocument(r"...")`) no toca el documento vivo, y aun así pagaba la
        // copia y recibía en cada respuesta el aviso de capas rotas de un documento que
        // no había tocado (20 llamadas seguidas el 2026-09-22).
        //
        // Ninguna cuenta detrás de un punto: `.mxd` es la EXTENSIÓN de un fichero
        // (r"C:\planos\hoja.mxd", f.endswith(".mxd")) y `x.df` un atributo, no la
        // variable inyectada. El sesgo sigue siendo hacia el FALSO POSITIVO —un `mxd`
        // reasignado en un bucle cuenta—, porque copiar de más solo cuesta tiempo y
        // copiar de menos rompe el código del usuario con un NameError; el runner decide
        // después, con el código ya ejecutado, si la copia se usó de verdad antes de
        // avisar de sus capas rotas.
        private static readonly Regex SenalDocumento = new Regex(
            @"(?<!\.)\b(df|mxd)\b", RegexOptions.Compiled);

        /// <summary>
        /// ¿El código necesita el documento? Solo si menciona `mxd`, `df` o el módulo
        /// de mapping. Copiar el .mxd de una sesión cargada cuesta segundos o minutos,
        /// y la mayoría del arcpy útil (geoprocesar rásters, recorrer tablas, calcular
        /// índices) trabaja sobre ficheros en disco y no lo toca.
        /// </summary>
        private static bool NecesitaDocumento(string code)
        {
            if (string.IsNullOrEmpty(code)) return false;
            return SenalDocumento.IsMatch(code);
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
                ? RunJobConSnapshot("execute_code", parameters, serializarSesion, ExecuteTimeout)
                : RunJob("execute_code", parameters, null, ExecuteTimeout);

            if (!(bool)r["ok"])
            {
                // "no puede abrir documento de mapa" / "Nombre de archivo MXD no valido"
                // sirven igual para una ruta mala, un fichero corrupto o una version
                // superior a la de la sesion: Esri no los distingue. Aqui se leen los
                // .mxd que menciona el codigo y se dice lo que se sabe de cada uno, para
                // que el diagnostico sea inmediato en vez de media conversacion. Cuesta
                // milisegundos y solo se hace cuando YA ha fallado.
                try
                {
                    if (MxdVersion.PareceFalloDeApertura((string)r["error"]))
                    {
                        string diag = MxdVersion.DiagnosticarCodigo(
                            (string)parameters["code"], MxdVersion.VersionArcMapSesion());
                        if (diag != null)
                            r["diagnostico_mxd"] = diag;
                    }
                }
                catch (Exception ex)
                {
                    // Un diagnostico que falla no puede tapar el error de verdad.
                    Log.Error("El diagnostico de .mxd fallo", ex);
                }
                return r;
            }

            if ((bool)r["ok"])
            {
                // El aviso lo decide la vía REAL, no lo que se pidió: con rutas relativas
                // y la carpeta del documento sin permiso de escritura, un
                // serializar_sesion=false acaba en SaveAsDocument, y decirle entonces al
                // llamante que la copia salió del disco sería mentirle sobre si sus
                // cambios sin guardar están dentro.
                string via = (string)r["result"]["snapshot_via"];
                bool copiaDeDisco = via == ViaDiscoTemp || via == ViaDiscoJunto;
                r["result"]["aviso"] = conDocumento
                    ? (copiaDeDisco ? AvisoSnapshot + AvisoSnapshotDisco : AvisoSnapshot)
                    : "Ejecutado SIN copiar el documento (el código no usa mxd ni df): más rápido y sin "
                      + "ocupar ArcMap. Si necesitas la sesión, pasa usar_documento=true.";

                // El código puede "funcionar" y no cambiar nada: los cambios de
                // renderer sobre la copia se descartan por diseño (ADR-004), y hasta
                // ahora nadie lo decía. Un agente perdió una sesión entera contra
                // este límite, que está documentado pero no se menciona al devolver
                // un resultado que no ha surtido efecto. Se avisa, no se bloquea:
                // la detección es por texto y puede dar falsos positivos.
                string aviso = AvisoSimbologia((string)parameters["code"]);
                if (aviso != null)
                    r["result"]["aviso_simbologia"] = aviso;
            }
            return r;
        }

        /// <summary>
        /// Si el código toca simbología, devuelve el aviso que REDIRIGE a la
        /// herramienta que sí muta la sesión viva; null si no la toca.
        /// </summary>
        private static string AvisoSimbologia(string code)
        {
            if (string.IsNullOrEmpty(code))
                return null;
            string c = code.ToLowerInvariant();
            bool toca = c.Contains("renderer") || c.Contains("symbology")
                        || c.Contains("symbol") || c.Contains("colorramp");
            if (!toca)
                return null;

            return "OJO: tu código menciona simbología (renderer/symbology), y execute_arcpy "
                 + "opera sobre una COPIA del documento: esos cambios NO llegan a la sesión "
                 + "viva y se descartan al terminar, aunque la llamada haya ido bien. "
                 + "Para cambiar la simbología de verdad usa set_graduated_symbology (rangos), "
                 + "set_unique_values_symbology (categorías), set_raster_symbology (ráster) o "
                 + "apply_symbology_from_layer (.lyr plantilla). "
                 + "Si solo estabas LEYENDO la simbología, ignora este aviso.";
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
            }, StaStepTimeout, "aplicar el encuadre de la página DDP");
            if (!(bool)aplicado["ok"])
                return aplicado;

            return Protocol.Result(new JObject
            {
                ["page_id"] = info["page_id"],
                ["valor"] = info["valor"],
                ["escala"] = aplicado["result"]["escala"],
                // Se reexpide: este handler construye un resultado nuevo y el
                // `snapshot_via` que puso RunJobConSnapshot se quedaría en el camino.
                ["snapshot_via"] = info["snapshot_via"],
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

        /// <summary>
        /// run_geoprocessing(fuera_de_arcmap=true): el geoproceso en el runner, con
        /// ArcMap libre mientras corre. El camino nativo (GeoprocessingHandlers) congela
        /// la interfaz porque ejecuta en el hilo de ArcMap.
        ///
        /// El precio es que el runner no ve la sesión: los nombres de capa de la TOC se
        /// traducen aquí a la ruta de su fuente. Una capa con definition query o con
        /// selección NO se traduce, porque el runner procesaría la fuente ENTERA y el
        /// resultado sería otro sin avisar: se devuelve error y se remite al camino
        /// nativo, que sí las respeta.
        /// </summary>
        public static JObject GeoprocesoFuera(JObject parameters)
        {
            JArray args = parameters["params"] as JArray ?? new JArray();
            bool resolver = parameters["resolver_capas"] == null
                || parameters["resolver_capas"].Type == JTokenType.Null
                || (bool)parameters["resolver_capas"];
            bool anadir = parameters["anadir_al_mapa"] == null
                || parameters["anadir_al_mapa"].Type == JTokenType.Null
                || (bool)parameters["anadir_al_mapa"];

            string tool = (string)parameters["tool"];
            if (string.IsNullOrEmpty(tool))
                throw new ArgumentException(
                    "Indica 'tool' (ej. 'management.CopyFeatures' o 'Buffer_analysis').");

            var traducidas = new JObject();
            JArray originales = args;
            JObject t = StaDispatcher.Invoke(delegate
            {
                // La firma se comprueba AQUÍ y no en el runner: con arcpy.gp un parámetro
                // de más se ignora y dos tumban Python (violación de acceso, 2026-09-26).
                string alias;
                GeoprocessingHandlers.ComprobarFirma(new ESRI.ArcGIS.Geoprocessing.GeoProcessorClass(),
                    tool, GeoprocessingHandlers.NombreGp(tool, out alias), originales.Count);
                if (!resolver)
                    return Protocol.Result(new JObject { ["params"] = originales });
                {
                    IMxDocument doc;
                    IMap map = MapHandlers.FocusMap(out doc);
                    var capas = MapHandlers.Capas(map);
                    var nuevos = new JArray();
                    foreach (JToken a in args)
                    {
                        if (a.Type == JTokenType.Array)
                        {
                            var lista = new JArray();
                            foreach (JToken el in (JArray)a)
                                lista.Add(TraducirCapa(el, capas, traducidas));
                            nuevos.Add(lista);
                        }
                        else
                            nuevos.Add(TraducirCapa(a, capas, traducidas));
                    }
                    return Protocol.Result(new JObject { ["params"] = nuevos });
                }
            }, StaStepTimeout, "comprobar la firma y traducir capas a rutas");
            if (!(bool)t["ok"])
                return t;
            args = (JArray)t["result"]["params"];

            var job = new JObject
            {
                ["tool"] = parameters["tool"],
                ["params"] = args,
                ["sobrescribir"] = parameters["sobrescribir"] ?? false,
            };
            JObject r = RunJob("geoprocessing", job, null);
            if (!(bool)r["ok"])
                return r;
            JObject res = (JObject)r["result"];
            res["fuera_de_arcmap"] = true;
            if (traducidas.Count > 0)
                res["capas_por_ruta"] = traducidas;
            var anadidas = new JArray();
            if (anadir && res["capas_salida"] is JArray)
                foreach (JToken ruta in (JArray)res["capas_salida"])
                    if (AddToMap((string)ruta))
                        anadidas.Add(ruta);
            res["anadidas_al_mapa"] = anadidas;
            return r;
        }

        /// <summary>Nombre de capa de la TOC → ruta de su fuente (en el hilo de ArcMap).
        /// Mismo criterio que la resolución nativa: lo que tiene pinta de ruta o de SQL
        /// no se toca, y el nombre se compara sin mayúsculas.</summary>
        private static JToken TraducirCapa(JToken a, System.Collections.Generic.List<ILayer> capas,
                                           JObject traducidas)
        {
            if (a.Type != JTokenType.String)
                return a;
            string s = (string)a;
            if (s.IndexOfAny(GeoprocessingHandlers.NoResolver) >= 0)
                return a;
            foreach (ILayer lyr in capas)
            {
                if (!string.Equals(lyr.Name, s, StringComparison.OrdinalIgnoreCase))
                    continue;
                var def = lyr as IFeatureLayerDefinition;
                if (def != null && !string.IsNullOrEmpty(def.DefinitionExpression))
                    throw new ArgumentException("La capa '" + lyr.Name + "' tiene una definition query ("
                        + def.DefinitionExpression + "). Fuera de ArcMap se procesaría la fuente ENTERA:"
                        + " usa fuera_de_arcmap=false, que la respeta, o exporta antes lo filtrado.");
                var sel = lyr as IFeatureSelection;
                if (sel != null && sel.SelectionSet != null && sel.SelectionSet.Count > 0)
                    throw new ArgumentException("La capa '" + lyr.Name + "' tiene " + sel.SelectionSet.Count
                        + " entidades seleccionadas. Fuera de ArcMap se procesaría la fuente ENTERA:"
                        + " usa fuera_de_arcmap=false, que respeta la selección, o limpia la selección.");
                string workspace;
                string ruta = DataAccess.RutaFuente(lyr, out workspace);
                if (string.IsNullOrEmpty(ruta))
                    throw new ArgumentException("La capa '" + lyr.Name + "' no tiene una fuente en disco"
                        + " resoluble, así que el geoproceso fuera de ArcMap no podría abrirla."
                        + " Pásale la ruta del dato o usa fuera_de_arcmap=false.");
                traducidas[lyr.Name] = ruta;
                return ruta;
            }
            return a;
        }

        // calculate_geometry NO va por subprocess: la sesión viva mantiene un schema
        // lock sobre las fuentes cargadas en la TOC y AddGeometryAttributes añade
        // campos → nativo in-process en GeoprocessingHandlers.CalculateGeometry.
    }
}
