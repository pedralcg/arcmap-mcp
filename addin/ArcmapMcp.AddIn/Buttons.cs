using System.Windows.Forms;
using AddInButton = ESRI.ArcGIS.Desktop.AddIns.Button;

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

    public class StatusMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            McpExtension ext = McpExtension.Instance;
            bool run = ext != null && ext.IsRunning;
            string estado = run ? "ACTIVO" : "PARADO";
            MessageBox.Show(
                "Estado del puente MCP: " + estado + "\n"
                + "Dirección: 127.0.0.1:" + McpServer.Port + "\n\n"
                + "Log: " + Log.PathInfo,
                "arcmap-mcp · Estado del puente");
        }
    }

    public class AboutMcpButton : AddInButton
    {
        protected override void OnClick()
        {
            AboutFicha.Show();
        }
    }
}
