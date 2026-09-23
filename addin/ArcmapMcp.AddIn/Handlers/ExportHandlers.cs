using System;
using System.IO;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geometry;
using ESRI.ArcGIS.Output;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// export_pdf / export_jpg / export_view_png — contrato JSON que esperan los
    /// schemas del servidor MCP. Gotcha .NET vs arcpy: IActiveView.Output solo dibuja con
    /// garantías la vista ACTIVA (arcpy renderizaba el layout en frío), así que si
    /// la vista pedida no es la activa se CAMBIA temporalmente con el setter de
    /// IMxDocument.ActiveView y se restaura en finally. El dibujado pasa por
    /// ITrackCancel: ESC en ArcMap aborta sin matar el puente.
    /// </summary>
    internal static class ExportHandlers
    {
        private const double ScreenDpi = 96.0;

        /// <summary>Rango admitido de dpi. El techo no es capricho: 1200 dpi sobre un
        /// A1 son ~2.000 millones de píxeles, y ArcMap es un proceso de 32 bits —
        /// eso no es un export lento, es un OutOfMemory que se lleva la sesión por
        /// delante. Mejor decirlo que intentarlo.</summary>
        private const int DpiMin = 24;
        private const int DpiMax = 600;

        public static JObject ExportPdf(JObject parameters)
        {
            string salida = Parametros.RutaDeSalida(parameters["salida"], "salida", new[] { ".pdf" });
            int dpi = Parametros.LeerEntero(parameters["dpi"], "dpi", 300, DpiMin, DpiMax);
            return Exportado(new ExportPDFClass(), parameters, salida, dpi, true, 0, 0, null);
        }

        public static JObject ExportJpg(JObject parameters)
        {
            string salida = Parametros.RutaDeSalida(parameters["salida"], "salida", new[] { ".jpg", ".jpeg" });
            int dpi = Parametros.LeerEntero(parameters["dpi"], "dpi", 230, DpiMin, DpiMax);
            IExport export = new ExportJPEGClass();
            ((IExportJPEG)export).Quality = 95;
            return Exportado(export, parameters, salida, dpi, true, 0, 0, null);
        }

        public static JObject ExportViewPng(JObject parameters)
        {
            string salida = Parametros.RutaDeSalida(parameters["salida"], "salida", new[] { ".png" });
            int dpi = Parametros.LeerEntero(parameters["dpi"], "dpi", 150, DpiMin, DpiMax);
            string modo = ((string)parameters["modo"] ?? "vista").ToLowerInvariant();
            // 0 = derivar del aspect ratio del frame. El techo es por el mismo motivo
            // que el de dpi: el bitmap se monta entero en memoria de 32 bits.
            int ancho = Parametros.LeerEntero(parameters["ancho"], "ancho", 0, 0, 10000);
            int alto = Parametros.LeerEntero(parameters["alto"], "alto", 0, 0, 10000);
            return Exportado(new ExportPNGClass(), parameters, salida, dpi, modo == "layout", ancho, alto,
                new JObject { ["modo"] = modo });
        }

        /// <summary>
        /// Envoltorio común de las tres tools: comprueba la sobrescritura, exporta a
        /// un TEMPORAL junto al destino y solo al final lo mueve encima.
        ///
        /// El temporal es el arreglo de una pérdida de datos real: al cancelar con
        /// ESC se hacía `File.Delete(salida)` sin mirar si ese fichero existía ANTES,
        /// así que reexportar un plano y arrepentirse borraba el plano bueno. Con el
        /// temporal, una cancelación no toca lo que había.
        ///
        /// `sobrescribir` va a TRUE por defecto en los export, a diferencia de
        /// save_mxd_as: una serie de planos se reexporta encima una y otra vez, y
        /// pedir permiso cada vez rompería ese flujo. Lo que sí cambia es que la
        /// respuesta DICE si se pisó algo (`sobrescrito`).
        /// </summary>
        private static JObject Exportado(IExport export, JObject parameters, string salida, int dpi,
                                         bool quierenLayout, int anchoPx, int altoPx, JObject extra)
        {
            bool sobrescribir = Parametros.LeerBool(parameters["sobrescribir"], "sobrescribir", true);
            bool sobrescrito = Parametros.ComprobarSobrescritura(salida, sobrescribir, "sobrescribir");

            string tmp = TemporalJuntoA(salida);
            string aviso = null;
            int intento = 0;
            while (true)
            {
                intento++;
                try
                {
                    aviso = Exportar(export, tmp, dpi, quierenLayout, anchoPx, altoPx);
                    break;
                }
                catch (Exception ex)
                {
                    Borrar(tmp);
                    if (!EsPendiente(ex))
                        throw;
                    if (intento >= IntentosPendiente)
                        throw new InvalidOperationException(
                            "ArcMap seguía dibujando el mapa tras " + IntentosPendiente + " intentos "
                            + "(E_PENDING, 0x8000000A): el export no se ha hecho y el fichero de destino "
                            + "no se ha tocado. Espera a que termine de pintar —capas WMS o rásters "
                            + "pesados tardan— y repite la llamada.", ex);
                    Log.Info("Export con E_PENDING (intento " + intento + " de " + IntentosPendiente
                             + "): se deja dibujar a ArcMap y se reintenta.");
                    DejarDibujar(EsperaPendiente);
                }
            }
            if (intento > 1)
                aviso = (aviso == null ? "" : aviso + " ") + "Salió al intento " + intento
                        + ": ArcMap devolvió E_PENDING (mapa aún dibujando) en los anteriores.";

            try
            {
                if (File.Exists(salida))
                    File.Delete(salida);
                File.Move(tmp, salida);
            }
            catch (Exception ex)
            {
                Borrar(tmp);
                throw new InvalidOperationException("La exportación terminó pero no se pudo escribir "
                    + salida + " (¿abierto en otro programa?): " + ex.Message, ex);
            }

            // QUÉ se ha exportado. Estas tools exportan SIEMPRE el documento abierto, y
            // hasta la 2.12.0 la respuesta no lo decía: el 2026-09-22 una llamada con un
            // `mxd=` que la tool no admite exportó en silencio OTRO plano con `ok: true`,
            // y solo el tamaño del fichero lo delató. Un export que no dice qué ha
            // exportado no es verificable.
            string documento = null;
            try { documento = ArcSession.MxdPath(ArcSession.App()); } catch { }
            var inner = new JObject
            {
                ["salida"] = salida,
                ["documento"] = documento ?? "(documento sin guardar)",
                ["dpi"] = dpi,
                ["sobrescrito"] = sobrescrito
            };
            if (extra != null)
                foreach (var prop in extra)
                    inner[prop.Key] = prop.Value;
            if (aviso != null)
                inner["aviso"] = aviso;
            return Protocol.Result(inner);
        }

        // E_PENDING (0x8000000A, "el dato necesario ... no está disponible todavía"):
        // el mapa sigue dibujando. Visto el 2026-09-21 en ~4 de ~11 pases de la
        // regresión, siempre en export_jpg, SIN disparador identificado (tres hipótesis
        // probadas y refutadas). Este reintento TAPA EL SÍNTOMA, no explica la causa:
        // que nadie lo lea como entendido. Acotado en intentos, nunca indefinido.
        private const int EPending = unchecked((int)0x8000000A);
        private const int IntentosPendiente = 3;
        private static readonly TimeSpan EsperaPendiente = TimeSpan.FromSeconds(2);

        private static bool EsPendiente(Exception ex)
        {
            for (Exception e = ex; e != null; e = e.InnerException)
            {
                var com = e as System.Runtime.InteropServices.COMException;
                if (com != null && com.ErrorCode == EPending)
                    return true;
                if (e.HResult == EPending)
                    return true;
            }
            return false;
        }

        /// <summary>Espera BOMBEANDO mensajes. Esto corre en el hilo de ArcMap: un
        /// Thread.Sleep lo congelaría y el mapa no podría acabar de dibujar, que es
        /// justo lo que se está esperando.</summary>
        private static void DejarDibujar(TimeSpan espera)
        {
            var reloj = System.Diagnostics.Stopwatch.StartNew();
            while (reloj.Elapsed < espera)
            {
                System.Windows.Forms.Application.DoEvents();
                System.Threading.Thread.Sleep(50);
            }
        }

        /// <summary>Temporal en la MISMA carpeta que el destino: así el movimiento
        /// final es un rename dentro del volumen (atómico y sin copiar gigas), y si
        /// la carpeta no admitiera escritura se vería al empezar, no al terminar.</summary>
        private static string TemporalJuntoA(string salida)
        {
            string carpeta = System.IO.Path.GetDirectoryName(salida) ?? "";
            string nombre = System.IO.Path.GetFileNameWithoutExtension(salida)
                + ".mcp-" + Guid.NewGuid().ToString("N").Substring(0, 8)
                + System.IO.Path.GetExtension(salida);
            return System.IO.Path.Combine(carpeta, nombre);
        }

        private static void Borrar(string ruta)
        {
            try { if (File.Exists(ruta)) File.Delete(ruta); }
            catch { }
        }

        /// <summary>
        /// Exporta la vista pedida (layout o data frame activo) a `salida`. Si esa
        /// vista no es la activa, cambia temporalmente y restaura al terminar.
        /// `anchoPx`/`altoPx` (solo PNG de vista) fuerzan el tamaño en píxeles; el
        /// que falte se deriva del aspect ratio del frame. Devuelve el aviso de
        /// cambio de vista, o null si no hubo cambio.
        /// </summary>
        private static string Exportar(IExport export, string salida, int dpi,
                                       bool quierenLayout, int anchoPx, int altoPx)
        {
            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);

            bool layoutActivo = doc.ActiveView is IPageLayout;
            IActiveView original = doc.ActiveView;
            bool cambiada = false;
            string aviso = null;
            if (quierenLayout != layoutActivo)
            {
                doc.ActiveView = quierenLayout
                    ? (IActiveView)doc.PageLayout
                    : (IActiveView)doc.FocusMap;
                cambiada = true;
                aviso = "la vista activa era '" + (layoutActivo ? "layout" : "vista")
                        + "'; se cambió temporalmente a '" + (quierenLayout ? "layout" : "vista")
                        + "' para exportar y se restauró.";
            }

            try
            {
                IActiveView av = doc.ActiveView;
                export.ExportFileName = salida;
                export.Resolution = dpi;

                tagRECT frame = av.ExportFrame;
                double frameAncho = frame.right - frame.left;
                double frameAlto = frame.bottom - frame.top;
                double escala = dpi / ScreenDpi;
                int outAncho = (int)Math.Round(frameAncho * escala);
                int outAlto = (int)Math.Round(frameAlto * escala);
                if (anchoPx > 0 && altoPx > 0) { outAncho = anchoPx; outAlto = altoPx; }
                else if (anchoPx > 0) { outAncho = anchoPx; outAlto = (int)Math.Round(anchoPx * frameAlto / frameAncho); }
                else if (altoPx > 0) { outAlto = altoPx; outAncho = (int)Math.Round(altoPx * frameAncho / frameAlto); }

                var outRect = new tagRECT { left = 0, top = 0, right = outAncho, bottom = outAlto };
                IEnvelope pixelBounds = new EnvelopeClass();
                pixelBounds.PutCoords(0, 0, outRect.right, outRect.bottom);
                export.PixelBounds = pixelBounds;

                ITrackCancel cancel = new CancelTrackerClass();
                cancel.CancelOnKeyPress = true;
                cancel.CancelOnClick = false;

                int hdc = export.StartExporting();
                try
                {
                    av.Output(hdc, dpi, ref outRect, null, cancel);
                }
                finally
                {
                    export.FinishExporting();
                    export.Cleanup();
                }

                if (!cancel.Continue())
                {
                    // 'salida' aquí es el TEMPORAL (ver Exportado): borrarlo no toca
                    // el fichero de destino, que es justo lo que antes se perdía.
                    Borrar(salida);
                    throw new OperationCanceledException("Export cancelado por el usuario (ESC) en ArcMap.");
                }
            }
            finally
            {
                if (cambiada)
                {
                    doc.ActiveView = original;
                    doc.ActiveView.Refresh();
                }
            }
            return aviso;
        }
    }
}
