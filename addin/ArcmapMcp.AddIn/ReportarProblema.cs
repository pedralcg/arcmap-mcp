using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Windows.Forms;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Reporte de problemas. Ofrece dos canales (issue de GitHub o email) con el
    /// contexto de diagnóstico NO SENSIBLE ya pre-rellenado. El log crudo —que puede
    /// contener nombres de capas y rutas, posibles datos de cliente (L3)— NUNCA se
    /// adjunta solo: se prepara aparte y el usuario decide, tras un aviso explícito.
    /// El formulario muestra el diagnóstico ANTES de enviar nada: el usuario ve lo
    /// que sale de su máquina.
    /// </summary>
    internal static class ReportarProblema
    {
        private const string Asunto = "arcmap-mcp: reporte de problema";

        public static void Show()
        {
            try
            {
                bool activo = McpExtension.Instance != null && McpExtension.Instance.IsRunning;
                string diagnostico = Diagnostico.TextoNoSensible(activo);
                using (var f = new FormReporte(diagnostico))
                    f.ShowDialog();
            }
            catch (Exception ex)
            {
                Log.Error("No se pudo abrir el reporte de problemas", ex);
                MessageBox.Show("No se ha podido abrir el reporte:\n" + ex.Message,
                                "arcmap-mcp · Reportar problema");
            }
        }

        /// <summary>Cuerpo del reporte: plantilla para el usuario + bloque de diagnóstico.</summary>
        public static string CuerpoReporte(string diagnostico)
        {
            return
                "## Qué esperabas que pasara\r\n(describe aquí)\r\n\r\n"
                + "## Qué pasó en realidad\r\n(describe aquí)\r\n\r\n"
                + "## Pasos para reproducirlo\r\n1. \r\n2. \r\n\r\n"
                + "## Diagnóstico (generado automáticamente — entorno y contadores, sin datos de proyecto)\r\n"
                + "```\r\n" + diagnostico + "\r\n```\r\n";
        }

        /// <summary>Abre el navegador en un issue nuevo de GitHub pre-rellenado.</summary>
        public static void AbrirIssueGitHub(string diagnostico)
        {
            string url = Actualizaciones.RepoUrl + "/issues/new"
                + "?title=" + Uri.EscapeDataString(Asunto)
                + "&body=" + Uri.EscapeDataString(CuerpoReporte(diagnostico));
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
        }

        /// <summary>Abre el cliente de correo con un email pre-rellenado.</summary>
        public static void AbrirEmail(string diagnostico)
        {
            string url = "mailto:pedro@pedralcg.dev"
                + "?subject=" + Uri.EscapeDataString(Asunto)
                + "&body=" + Uri.EscapeDataString(CuerpoReporte(diagnostico));
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
        }

        /// <summary>Copia el log al Escritorio y abre la carpeta, avisando de que puede
        /// contener datos de cliente. El usuario decide si adjuntarlo tras revisarlo.</summary>
        public static void PrepararLog()
        {
            try
            {
                if (!File.Exists(Log.PathInfo))
                {
                    MessageBox.Show("Todavía no hay fichero de log en " + Log.PathInfo,
                                    "arcmap-mcp · Reportar problema");
                    return;
                }

                string escritorio = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
                string destino = Path.Combine(escritorio,
                    "arcmap-mcp-log-" + DateTime.Now.ToString("yyyyMMdd-HHmm") + ".txt");
                File.Copy(Log.PathInfo, destino, true);

                MessageBox.Show(
                    "Se ha copiado el log a tu Escritorio:\n" + destino + "\n\n"
                    + "⚠ El log puede contener NOMBRES DE CAPAS y RUTAS de tu proyecto "
                    + "(posibles datos de cliente). Revísalo y borra lo sensible ANTES de "
                    + "adjuntarlo a un issue o enviarlo por correo.",
                    "arcmap-mcp · Revisa el log antes de compartirlo",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);

                Process.Start(new ProcessStartInfo
                {
                    FileName = "explorer.exe",
                    Arguments = "/select,\"" + destino + "\"",
                    UseShellExecute = true
                });
            }
            catch (Exception ex)
            {
                Log.Error("No se pudo preparar el log para adjuntar", ex);
                MessageBox.Show("No se ha podido preparar el log:\n" + ex.Message,
                                "arcmap-mcp · Reportar problema");
            }
        }
    }

    /// <summary>
    /// Formulario del reporte: enseña el diagnóstico (transparencia = privacidad) y
    /// ofrece los canales. Sin diseñador: WinForms a mano, coherente con el estilo del
    /// resto del add-in (MessageBox y HTML embebido, sin .resx).
    /// </summary>
    internal sealed class FormReporte : Form
    {
        private readonly string _diagnostico;

        public FormReporte(string diagnostico)
        {
            _diagnostico = diagnostico;

            Text = "arcmap-mcp · Reportar problema";
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            MaximizeBox = false;
            MinimizeBox = false;
            ClientSize = new Size(560, 460);
            Font = new Font("Segoe UI", 9f);

            var intro = new Label
            {
                // El texto decía "no incluye rutas" y el bloque traía la del intérprete
                // Python, que puede llevar el nombre del usuario. Ahora se enmascara el
                // perfil (%USERPROFILE%) y la promesa dice lo que de verdad sale: la
                // diferencia entre una promesa y una promesa que se cumple.
                Text = "Se enviará SOLO este diagnóstico (versiones, entorno y contadores de "
                       + "sesión). No incluye el documento, ni nombres de capas, ni rutas de tus "
                       + "datos; sí aparece la ruta del Python de ArcGIS, con tu carpeta de usuario "
                       + "enmascarada. Léelo abajo antes de enviarlo.",
                Location = new Point(12, 12),
                Size = new Size(536, 48),
                AutoSize = false
            };

            var caja = new TextBox
            {
                Text = _diagnostico,
                Location = new Point(12, 66),
                Size = new Size(536, 268),
                Multiline = true,
                ReadOnly = true,
                ScrollBars = ScrollBars.Vertical,
                Font = new Font("Consolas", 9f),
                BackColor = Color.White
            };

            var btnIssue = new Button
            {
                Text = "Abrir issue en GitHub",
                Location = new Point(12, 348),
                Size = new Size(170, 32)
            };
            btnIssue.Click += delegate { Lanzar(delegate { ReportarProblema.AbrirIssueGitHub(_diagnostico); }); };

            var btnEmail = new Button
            {
                Text = "Enviar por email",
                Location = new Point(190, 348),
                Size = new Size(150, 32)
            };
            btnEmail.Click += delegate { Lanzar(delegate { ReportarProblema.AbrirEmail(_diagnostico); }); };

            var btnCopiar = new Button
            {
                Text = "Copiar diagnóstico",
                Location = new Point(348, 348),
                Size = new Size(150, 32)
            };
            btnCopiar.Click += delegate
            {
                try
                {
                    Clipboard.SetText(ReportarProblema.CuerpoReporte(_diagnostico));
                    MessageBox.Show("Diagnóstico copiado al portapapeles.",
                                    "arcmap-mcp · Reportar problema");
                }
                catch (Exception ex) { Log.Error("No se pudo copiar al portapapeles", ex); }
            };

            var btnLog = new Button
            {
                Text = "Preparar log para adjuntar (revísalo antes)…",
                Location = new Point(12, 388),
                Size = new Size(340, 30)
            };
            btnLog.Click += delegate { ReportarProblema.PrepararLog(); };

            var btnCerrar = new Button
            {
                Text = "Cerrar",
                Location = new Point(438, 388),
                Size = new Size(110, 30),
                DialogResult = DialogResult.Cancel
            };

            Controls.Add(intro);
            Controls.Add(caja);
            Controls.Add(btnIssue);
            Controls.Add(btnEmail);
            Controls.Add(btnCopiar);
            Controls.Add(btnLog);
            Controls.Add(btnCerrar);
            CancelButton = btnCerrar;
        }

        private void Lanzar(Action accion)
        {
            try { accion(); }
            catch (Exception ex)
            {
                Log.Error("No se pudo abrir el canal de reporte", ex);
                MessageBox.Show("No se ha podido abrir:\n" + ex.Message,
                                "arcmap-mcp · Reportar problema");
            }
        }
    }
}
