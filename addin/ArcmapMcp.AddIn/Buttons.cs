using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using AddInButton = ESRI.ArcGIS.Desktop.AddIns.Button;
using AddInComboBox = ESRI.ArcGIS.Desktop.AddIns.ComboBox;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Botones de la barra "arcmap-mcp": Iniciar se muestra "pulsado" (checked)
    /// mientras el puente está activo y Detener se deshabilita cuando está parado.
    /// ArcMap re-consulta ese estado en OnUpdate al refrescar la barra.
    /// </summary>
    public class StartMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            McpExtension ext = McpExtension.Instance;
            if (ext == null)
            {
                MessageBox.Show("La extensión MCP no está cargada.", "arcmap-mcp · Error");
                return;
            }
            string error = ext.StartServer();
            if (error == null)
            {
                MessageBox.Show(
                    "El puente MCP está activo y escuchando en 127.0.0.1:" + McpServer.Port + ".\n"
                    + "Mantén ArcMap abierto mientras lo utilizas.\n\n"
                    + "pedralcg.dev",
                    "arcmap-mcp · Puente iniciado");
            }
            else
            {
                MessageBox.Show("No se ha podido iniciar el puente:\n" + error,
                                "arcmap-mcp · Error");
            }
        }

        protected override void OnUpdate()
        {
            McpExtension ext = McpExtension.Instance;
            Enabled = true;
            Checked = ext != null && ext.IsRunning;
        }
    }

    public class StopMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            McpExtension ext = McpExtension.Instance;
            if (ext == null || !ext.IsRunning)
            {
                MessageBox.Show("El puente MCP no está cargado; no hay nada que detener.",
                                "arcmap-mcp · Estado del puente");
                return;
            }
            ext.StopServer();
            MessageBox.Show("El puente MCP se ha detenido correctamente.",
                            "arcmap-mcp · Puente detenido");
        }

        protected override void OnUpdate()
        {
            McpExtension ext = McpExtension.Instance;
            Enabled = ext != null && ext.IsRunning; // gris hasta que el puente arranque
        }
    }

    /// <summary>
    /// Ficha de diagnóstico: lo que hace falta saber cuando el agente "no llega".
    /// Ofrece abrir el log directamente, que es el paso siguiente casi siempre.
    /// </summary>
    public class StatusMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            McpExtension ext = McpExtension.Instance;
            bool run = ext != null && ext.IsRunning;
            Version v = typeof(StatusMcpButton).Assembly.GetName().Version;

            string texto =
                "PUENTE\n"
                + "  Estado:        " + (run ? "ACTIVO, escuchando" : "PARADO") + "\n"
                + "  Dirección:     127.0.0.1:" + McpServer.Port + "\n"
                + "  Add-in:        arcmap-mcp " + v.ToString(3) + " (.NET)\n"
                + "  Actualización: " + Diagnostico.DescribirActualizacion() + "\n"
                + "  Autoarranque:  " + (Ajustes.Autoarranque ? "SI (se inicia al abrir ArcMap)" : "NO (hay que pulsar Iniciar)") + "\n"
                + "\nSESIÓN\n"
                + "  Documento:     " + DescribirDocumento() + "\n"
                + "  Desde:         " + Estadisticas.Inicio.ToString("HH:mm:ss") + "\n"
                + "  Peticiones:    " + Estadisticas.Peticiones + "\n"
                + "  Último:        " + Estadisticas.UltimoComando + "\n"
                + "  Errores:       " + Estadisticas.Errores + "\n"
                + "\nENTORNO\n"
                + "  Python arcpy:  " + DescribirPython27() + "\n"
                + "  Log:           " + Log.PathInfo + "\n"
                + "\n¿Abrir el log ahora?";

            if (MessageBox.Show(texto, "arcmap-mcp · Estado del puente",
                                MessageBoxButtons.YesNo, MessageBoxIcon.Information) == DialogResult.Yes)
            {
                AbrirLog();
            }
        }

        private static string DescribirDocumento()
        {
            try
            {
                var app = Handlers.ArcSession.App();
                var doc = app.Document as ESRI.ArcGIS.ArcMapUI.IMxDocument;
                if (doc == null) return "(sin documento)";
                string mapa = doc.FocusMap != null ? doc.FocusMap.Name : "?";
                int capas = doc.FocusMap != null ? doc.FocusMap.LayerCount : 0;
                return app.Document.Title + "  ·  mapa " + mapa + ", " + capas + " capas";
            }
            catch (Exception ex)
            {
                return "(no accesible: " + ex.Message + ")";
            }
        }

        /// <summary>El MISMO buscador que usa el runner (Python27), no una copia del
        /// criterio: aquí había otra ruta 10.5 escrita a mano —la tercera del proyecto—
        /// que informaba de "NO encontrado" con el Python bien instalado en 10.6-10.8.
        /// Sin enmascarar el perfil, a diferencia del reporte de problemas: esto se ve en
        /// la propia máquina del usuario y la ruta real es justo lo accionable.
        /// Sin él no hay execute_arcpy ni DDP.</summary>
        private static string DescribirPython27()
        {
            return Python27.Describir();
        }

        private static void AbrirLog()
        {
            try
            {
                if (File.Exists(Log.PathInfo))
                    Process.Start(new ProcessStartInfo { FileName = Log.PathInfo, UseShellExecute = true });
                else
                    MessageBox.Show("Todavía no hay fichero de log en " + Log.PathInfo,
                                    "arcmap-mcp · Estado del puente");
            }
            catch (Exception ex)
            {
                MessageBox.Show("No se ha podido abrir el log:\n" + ex.Message,
                                "arcmap-mcp · Estado del puente");
            }
        }
    }

    /// <summary>
    /// Autoarranque del puente, como DESPLEGABLE y no como botón: el icono de un
    /// botón no se puede repintar en caliente (la clase Button de los add-ins solo
    /// expone Caption, Message, Tooltip, Enabled y Checked) y el "pulsado" de
    /// Checked se lee mal en la barra. Un desplegable enseña el valor en texto,
    /// siempre visible y sin configurar nada.
    /// </summary>
    public class AutoStartComboBox : AddInComboBox
    {
        private const string TextoNo = "Autoarranque: No";
        private const string TextoSi = "Autoarranque: Sí";

        private readonly int _cookieNo;
        private readonly int _cookieSi;
        private bool _sincronizando;

        public AutoStartComboBox()
        {
            // La lista es cerrada (editable="false" en Config.xml): solo dos valores.
            _cookieNo = Add(TextoNo);
            _cookieSi = Add(TextoSi);
            SincronizarConPreferencia();
        }

        /// <summary>Refleja en el desplegable lo que dice el registro, sin disparar
        /// la lógica de OnSelChange (que volvería a escribir la preferencia).</summary>
        private void SincronizarConPreferencia()
        {
            _sincronizando = true;
            try { Select(Ajustes.Autoarranque ? _cookieSi : _cookieNo); }
            finally { _sincronizando = false; }
        }

        protected override void OnSelChange(int cookie)
        {
            if (_sincronizando || cookie < 0) return;

            bool activar = (cookie == _cookieSi);
            Ajustes.Autoarranque = activar;

            string aviso;
            if (activar)
            {
                // Coherencia inmediata: si se pide autoarranque, el puente debe estar
                // ya escuchando en esta sesión, no solo en la siguiente.
                string error = McpExtension.Instance != null ? McpExtension.Instance.StartServer() : null;
                aviso =
                    "Autoarranque ACTIVADO.\n\n"
                    + "El puente MCP se levantará solo cada vez que abras ArcMap.\n"
                    + (error == null
                        ? "En esta sesión ya está escuchando en 127.0.0.1:" + McpServer.Port + "."
                        : "Aviso: no se ha podido iniciar ahora mismo:\n" + error);
            }
            else
            {
                aviso =
                    "Autoarranque DESACTIVADO.\n\n"
                    + "A partir de la próxima sesión tendrás que pulsar \"Iniciar MCP\".\n"
                    + "El puente actual sigue activo hasta que lo detengas.";
            }

            // El MessageBox NO puede mostrarse aquí dentro: al cerrarse un diálogo
            // modal, el ComboBox de ArcMap revierte su selección al primer ítem y
            // vuelve a disparar OnSelChange (con "No"), pisando la preferencia recién
            // guardada. Se pospone al message pump para que corra cuando el combo ya
            // ha consolidado su selección, y se mantiene _sincronizando activo para
            // ignorar cualquier re-disparo espurio que llegue entretanto.
            _sincronizando = true;
            StaDispatcher.Post(delegate
            {
                try
                {
                    MessageBox.Show(aviso, "arcmap-mcp · Autoarranque");
                    // Reafirma el texto visible acorde al registro, por si el ciclo del
                    // diálogo intentó revertir la selección.
                    Select(Ajustes.Autoarranque ? _cookieSi : _cookieNo);
                }
                finally { _sincronizando = false; }
            });
        }

        protected override void OnUpdate()
        {
            Enabled = true;
        }
    }

    public class AboutMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            AboutFicha.Show();
        }
    }

    /// <summary>Reporte de problemas: abre el formulario con el diagnóstico y los
    /// canales (issue de GitHub o email). No adjunta el log automáticamente.</summary>
    public class ReportMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            ReportarProblema.Show();
        }
    }
}
