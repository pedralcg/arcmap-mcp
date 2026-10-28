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
            // Con un documento de disco en uso no hay ArcMap: lo que llega aquí es un
            // handler que no pasa por Documento (ADR-010).
            if (Documento.HayDocumentoEnDisco)
                throw Documento.SoloEnVivo("Esta herramienta");
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
        /// Devuelve null si no se puede resolver (documento sin guardar).
        ///
        /// Un documento NUEVO creado desde una plantilla y nunca guardado no está en la
        /// lista: el último item es la PLANTILLA, y save_mxd u open_mxd(guardar) la habrían
        /// sobrescrito (revisión del 2026-10-01). Por eso el nombre del fichero tiene que
        /// coincidir con el título del documento.</summary>
        public static string MxdPath(IApplication app)
        {
            try
            {
                ESRI.ArcGIS.Framework.ITemplates templates = app.Templates;
                if (templates != null && templates.Count > 0)
                {
                    string ruta = templates.get_Item(templates.Count - 1);
                    string titulo = app.Document != null ? app.Document.Title : null;
                    if (string.IsNullOrEmpty(ruta) || string.IsNullOrEmpty(titulo))
                        return ruta;
                    if (string.Equals(System.IO.Path.GetFileName(ruta), titulo, StringComparison.OrdinalIgnoreCase)
                        || string.Equals(System.IO.Path.GetFileNameWithoutExtension(ruta), titulo,
                                         StringComparison.OrdinalIgnoreCase))
                        return ruta;
                    Log.Info("MxdPath: la última plantilla (" + ruta + ") no es el documento '" + titulo
                             + "': documento sin guardar.");
                    return null;
                }
            }
            catch { }
            return null;
        }
    }
}
