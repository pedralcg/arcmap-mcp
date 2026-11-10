using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Las cinco acciones de la barra, sin nada de interfaz: reciben el idioma activo al
    /// que ya responden por sí mismas y muestran sus diálogos.
    /// </summary>
    /// <remarks>
    /// <b>Por qué las acciones NO son botones.</b> En las clases de add-in,
    /// <c>Button.Caption</c> solo escribe un campo privado (su <c>set_Caption</c> compila a
    /// un <c>stfld</c> y nada más): ArcMap lee ese texto UNA vez, al construir el control, y
    /// a partir de ahí dibuja el botón con lo que guardó. Asignar <c>Caption</c> en caliente
    /// cambia el getter —por eso el propio código "veía" el idioma nuevo— pero no la
    /// pantalla. Probado con asignación incondicional en cada <c>OnUpdate</c>: el botón se
    /// quedaba en el idioma de arranque.
    /// <para>
    /// Un desplegable sí se puede rehacer en caliente (<c>Clear</c> + <c>Add</c> cambia el
    /// texto visible sin reiniciar), así que las acciones viven en un desplegable. Es la
    /// única forma encontrada de cumplir "cambiar de idioma sin reiniciar ArcMap".
    /// </para>
    /// </remarks>
    internal static class AccionesMcp
    {
        /// <summary>Identifica cada acción. El orden coincide con el de los ítems.</summary>
        public enum Accion
        {
            Ninguna = -1,
            Iniciar = 0,
            Detener = 1,
            Estado = 2,
            Reportar = 3,
            AcercaDe = 4,
        }

        /// <summary>Claves del diccionario (y de config.xml, sin el sufijo).</summary>
        public static readonly string[] Claves =
        {
            "btn.start", "btn.stop", "btn.status", "btn.report", "btn.about",
        };

        /// <summary>¿Esta acción debe verse deshabilitada con el puente parado?</summary>
        public static bool SoloConPuente(Accion accion)
        {
            return accion == Accion.Detener;
        }

        /// <summary>¿El puente está escuchando ahora mismo?</summary>
        public static bool PuenteActivo()
        {
            McpExtension ext = McpExtension.Instance;
            return ext != null && ext.IsRunning;
        }

        public static void Ejecutar(Accion accion)
        {
            switch (accion)
            {
                case Accion.Iniciar: Iniciar(); break;
                case Accion.Detener: Detener(); break;
                case Accion.Estado: MostrarEstado(); break;
                case Accion.Reportar: ReportarProblema.Show(); break;
                case Accion.AcercaDe: AboutFicha.Show(); break;
            }
        }

        private static void Iniciar()
        {
            McpExtension ext = McpExtension.Instance;
            if (ext == null)
            {
                MessageBox.Show(IdiomaCfg.T("msg.ext.noCargada"), IdiomaCfg.T("msg.error.titulo"));
                return;
            }

            string error = ext.StartServer();
            if (error == null)
            {
                MessageBox.Show(
                    IdiomaCfg.T("msg.start.ok", McpServer.Port),
                    IdiomaCfg.T("msg.start.titulo"));
            }
            else
            {
                MessageBox.Show(IdiomaCfg.T("msg.start.fallo", error),
                                IdiomaCfg.T("msg.error.titulo"));
            }
        }

        private static void Detener()
        {
            McpExtension ext = McpExtension.Instance;
            if (ext == null || !ext.IsRunning)
            {
                MessageBox.Show(IdiomaCfg.T("msg.stop.sinPuente"),
                                IdiomaCfg.T("msg.stop.tituloEstado"));
                return;
            }

            ext.StopServer();
            MessageBox.Show(IdiomaCfg.T("msg.stop.ok"),
                            IdiomaCfg.T("msg.stop.titulo"));
        }

        /// <summary>
        /// Ficha de diagnóstico: lo que hace falta saber cuando el agente "no llega".
        /// Ofrece abrir el log directamente, que es el paso siguiente casi siempre.
        /// </summary>
        private static void MostrarEstado()
        {
            bool run = PuenteActivo();
            Version v = typeof(AccionesMcp).Assembly.GetName().Version;

            string texto =
                IdiomaCfg.T("estado.puente") + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.estado")) + (run ? IdiomaCfg.T("estado.activo") : IdiomaCfg.T("estado.parado")) + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.direccion")) + "127.0.0.1:" + McpServer.Port + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.addin")) + "arcmap-mcp " + v.ToString(3) + " (.NET)\n"
                + "  " + Pad(IdiomaCfg.T("estado.actualizacion")) + Diagnostico.DescribirActualizacion() + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.autoarranque")) + (Ajustes.Autoarranque ? IdiomaCfg.T("estado.auto.si") : IdiomaCfg.T("estado.auto.no")) + "\n"
                + "\n" + IdiomaCfg.T("estado.sesion") + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.documento")) + DescribirDocumento() + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.desde")) + Estadisticas.Inicio.ToString("HH:mm:ss") + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.peticiones")) + Estadisticas.Peticiones + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.ultimo")) + Estadisticas.UltimoComando + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.errores")) + Estadisticas.Errores + "\n"
                + "\n" + IdiomaCfg.T("estado.entorno") + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.python")) + Python27.Describir() + "\n"
                + "  " + Pad(IdiomaCfg.T("estado.log")) + Log.PathInfo + "\n"
                + "\n" + IdiomaCfg.T("estado.preguntaLog");

            if (MessageBox.Show(texto, IdiomaCfg.T("estado.titulo"),
                                MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
            {
                AbrirLog();
            }
        }

        /// <summary>
        /// Alinea el valor de la ficha de estado a un ancho fijo medido en CARACTERES
        /// DE DISPLAY, no en <c>string.Length</c>: un ideograma chino ocupa el doble que
        /// una letra latina, y contar caracteres dejaba la columna torcida justo en el
        /// idioma que se acaba de añadir.
        /// </summary>
        private static string Pad(string etiqueta)
        {
            const int ancho = 20;
            string relleno = new string(' ', Math.Max(1, ancho - AnchoDisplay(etiqueta)));
            return (etiqueta ?? "") + relleno;
        }

        /// <summary>Ancho aproximado en celdas de un texto monoespaciado.</summary>
        private static int AnchoDisplay(string texto)
        {
            if (string.IsNullOrEmpty(texto)) return 0;
            int ancho = 0;
            foreach (char c in texto)
            {
                bool anchoDoble =
                    (c >= '\u1100' && c <= '\u115F') ||   // Hangul Jamo
                    (c >= '\u2E80' && c <= '\uA4CF') ||   // radicales CJK, kana, ideogramas
                    (c >= '\uAC00' && c <= '\uD7A3') ||   // sílabas Hangul
                    (c >= '\uF900' && c <= '\uFAFF') ||   // compatibilidad CJK
                    (c >= '\uFE30' && c <= '\uFE4F') ||   // formas CJK
                    (c >= '\uFF00' && c <= '\uFF60') ||   // formas anchas (：（）)
                    (c >= '\uFFE0' && c <= '\uFFE6');
                ancho += anchoDoble ? 2 : 1;
            }
            return ancho;
        }

        private static string DescribirDocumento()
        {
            try
            {
                var app = Handlers.ArcSession.App();
                var doc = app.Document as ESRI.ArcGIS.ArcMapUI.IMxDocument;
                if (doc == null) return IdiomaCfg.T("estado.sinDocumento");
                string mapa = doc.FocusMap != null ? doc.FocusMap.Name : "?";
                int capas = doc.FocusMap != null ? doc.FocusMap.LayerCount : 0;
                return IdiomaCfg.T("estado.docInfo", app.Document.Title, mapa, capas);
            }
            catch (Exception ex)
            {
                return IdiomaCfg.T("estado.noAccesible", ex.Message);
            }
        }

        private static void AbrirLog()
        {
            try
            {
                if (File.Exists(Log.PathInfo))
                    Process.Start(new ProcessStartInfo { FileName = Log.PathInfo, UseShellExecute = true });
                else
                    MessageBox.Show(IdiomaCfg.T("estado.sinLog", Log.PathInfo),
                                    IdiomaCfg.T("estado.titulo"));
            }
            catch (Exception ex)
            {
                MessageBox.Show(IdiomaCfg.T("estado.log.fallo", ex.Message),
                                IdiomaCfg.T("estado.titulo"));
            }
        }
    }
}
