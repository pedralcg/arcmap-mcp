using System;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// El documento sobre el que trabaja un handler: el abierto en ArcMap (en vivo) o
    /// un .mxd abierto desde disco con IMapDocument (modo disco, ADR-010).
    ///
    /// Da lo poco que usan los handlers de leyenda, etiquetas, simbología, textos y
    /// capas: page layout, data frame activo, galería de estilos y los refrescos. En
    /// disco no hay pantalla, así que los refrescos no hacen nada; lo que sí importa
    /// en disco (IActiveView.ContentsChanged del mapa, que escuchan las leyendas) no
    /// pasa por aquí y se sigue llamando igual.
    ///
    /// Los handlers que no pasan por esta clase siguen yendo a ArcSession.App(), que
    /// con un documento de disco activo responde «solo en vivo».
    /// </summary>
    internal sealed class Documento
    {
        private readonly IMxDocument _vivo;
        private readonly IMapDocument _disco;

        // Documento de disco en uso. Lo fija el ejecutable del modo disco con Usar();
        // dentro de ArcMap es siempre null y todo va a la sesión viva.
        private static Documento _enDisco;

        private Documento(IMxDocument vivo, IMapDocument disco)
        {
            _vivo = vivo;
            _disco = disco;
        }

        internal static Documento Vivo(IMxDocument doc)
        {
            if (doc == null)
                throw new ArgumentNullException("doc");
            return new Documento(doc, null);
        }

        internal static Documento EnDisco(IMapDocument doc)
        {
            if (doc == null)
                throw new ArgumentNullException("doc");
            return new Documento(null, doc);
        }

        /// <summary>El documento de disco si hay uno en uso; si no, el de ArcMap.</summary>
        internal static Documento Actual()
        {
            if (_enDisco != null)
                return _enDisco;
            return Vivo(ArcSession.Doc(ArcSession.App()));
        }

        internal static bool HayDocumentoEnDisco
        {
            get { return _enDisco != null; }
        }

        /// <summary>Pone un documento de disco en uso hasta el Dispose del resultado.</summary>
        internal static IDisposable Usar(Documento doc)
        {
            if (doc == null || doc.EnVivo)
                throw new ArgumentException("Usar() es para un documento de disco.");
            if (_enDisco != null)
                throw new InvalidOperationException("Ya hay un documento de disco en uso.");
            _enDisco = doc;
            return new Liberador();
        }

        private sealed class Liberador : IDisposable
        {
            public void Dispose() { _enDisco = null; }
        }

        internal bool EnVivo
        {
            get { return _vivo != null; }
        }

        internal IPageLayout PageLayout
        {
            get { return _vivo != null ? _vivo.PageLayout : _disco.PageLayout; }
        }

        /// <summary>El data frame activo. En disco, el que el .mxd guardó como activo.</summary>
        internal IMap FocusMap
        {
            get { return _vivo != null ? _vivo.FocusMap : _disco.ActiveView.FocusMap; }
        }

        /// <summary>Los estilos cargados en ArcMap. En disco no hay sesión con estilos.</summary>
        internal IStyleGallery StyleGallery
        {
            get
            {
                if (_vivo == null)
                    throw SoloEnVivo("Los estilos .style");
                return _vivo.StyleGallery;
            }
        }

        /// <summary>Redibuja la tabla de contenidos. En disco no hay nada que redibujar.</summary>
        internal void UpdateContents()
        {
            if (_vivo != null)
                _vivo.UpdateContents();
        }

        /// <summary>Redibuja la vista activa. En disco no hay nada que redibujar.</summary>
        internal void Refrescar()
        {
            if (_vivo != null)
                _vivo.ActiveView.Refresh();
        }

        /// <summary>Redibuja los gráficos del layout. En disco no hay nada que redibujar.</summary>
        internal void RefrescarGraficosLayout()
        {
            if (_vivo != null)
                ((IActiveView)_vivo.PageLayout).PartialRefresh(esriViewDrawPhase.esriViewGraphics, null, null);
        }

        [System.Runtime.InteropServices.DllImport("user32.dll")]
        private static extern IntPtr GetDesktopWindow();

        private bool _layoutActivado;

        /// <summary>
        /// La pantalla del layout, para medir lo que ocupa un elemento (QueryBounds): sin
        /// pantalla, las medidas de texto salen en unidades basura. En vivo es la de la
        /// ventana de ArcMap y NO se activa otra (le quitaría el layout a la ventana). En
        /// disco se activa contra el escritorio, como se validó en los 29 planos de
        /// una serie real (2026-10-06).
        /// </summary>
        internal IDisplay PantallaLayout()
        {
            IActiveView av = (IActiveView)PageLayout;
            if (_vivo == null && !_layoutActivado)
            {
                av.Activate(GetDesktopWindow().ToInt32());
                _layoutActivado = true;
            }
            return (IDisplay)av.ScreenDisplay;
        }

        internal static InvalidOperationException SoloEnVivo(string que)
        {
            return new InvalidOperationException(que + ": solo con ArcMap abierto (en vivo); "
                + "no está entre las operaciones del modo disco.");
        }
    }
}
