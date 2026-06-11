using System;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Framework;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Acceso a la sesión viva de ArcMap. AppRef solo es co-creable DENTRO del
    /// proceso de ArcMap; co-crear vía ProgID y castear SOLO al interfaz (el cast
    /// al RCW de clase falla por identidad de tipos del singleton COM).
    /// Llamar siempre desde el hilo STA.
    /// </summary>
    internal static class ArcSession
    {
        public static IApplication App()
        {
            object appRef = Activator.CreateInstance(Type.GetTypeFromProgID("esriFramework.AppRef"));
            return (IApplication)appRef;
        }

        public static IMxDocument Doc(IApplication app)
        {
            IMxDocument doc = app.Document as IMxDocument;
            if (doc == null)
                throw new InvalidOperationException("El documento activo no es un documento de ArcMap (IMxDocument).");
            return doc;
        }

        /// <summary>Ruta del .mxd abierto: el último item de ITemplates es el
        /// documento actual (mismo truco que get_arcmap_info).
        /// Devuelve null si no se puede resolver (documento sin guardar).</summary>
        public static string MxdPath(IApplication app)
        {
            try
            {
                ESRI.ArcGIS.Framework.ITemplates templates = app.Templates;
                if (templates != null && templates.Count > 0)
                    return templates.get_Item(templates.Count - 1);
            }
            catch { }
            return null;
        }
    }
}
