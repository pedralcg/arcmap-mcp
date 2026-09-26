using System;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// ¿Está ArcMap en mitad de un dibujado? Lo sabe por IDisplayEvents (DisplayStarted /
    /// DisplayFinished) de la pantalla de la vista activa.
    ///
    /// Por qué importa: los comandos llegan al hilo de ArcMap por el Dispatcher de WPF, que se
    /// despacha con un mensaje de Windows, y ArcMap bombea mensajes MIENTRAS DIBUJA (para
    /// atender ESC). Así, un comando puede ejecutarse dentro de un dibujado a medias. Hipótesis
    /// del 2026-09-26 para la caída en MaplexAnnotation.dll (+0xD76B, puntero nulo, siempre
    /// redibujando el layout y siempre en ráfagas de cambios de la TOC): Maplex retiene la capa
    /// que el comando acaba de quitar.
    ///
    /// Se engancha de forma perezosa desde cada comando (en el hilo de ArcMap): así sigue a la
    /// vista activa aunque se cambie de documento o de vista, sin eventos de documento.
    /// </summary>
    internal static class VigiaDibujo
    {
        private static IDisplayEvents_Event _enganchada;
        private static int _profundidad;

        public static bool Dibujando
        {
            get { return _profundidad > 0; }
        }

        public static int Profundidad
        {
            get { return _profundidad; }
        }

        /// <summary>Se suscribe a la pantalla de la vista activa si no lo estaba ya.
        /// Best-effort: si falla, no hay vigía, pero el comando sigue.</summary>
        public static void Asegurar()
        {
            try
            {
                IMxDocument doc = ArcmapMcp.AddIn.Handlers.ArcSession.Doc(ArcmapMcp.AddIn.Handlers.ArcSession.App());
                if (doc == null || doc.ActiveView == null)
                    return;
                var pantalla = doc.ActiveView.ScreenDisplay as IDisplayEvents_Event;
                if (pantalla == null || ReferenceEquals(pantalla, _enganchada))
                    return;
                if (_enganchada != null)
                {
                    try
                    {
                        _enganchada.DisplayStarted -= AlEmpezar;
                        _enganchada.DisplayFinished -= AlTerminar;
                    }
                    catch { /* la pantalla anterior ya no existe */ }
                }
                pantalla.DisplayStarted += AlEmpezar;
                pantalla.DisplayFinished += AlTerminar;
                _enganchada = pantalla;
                _profundidad = 0;
            }
            catch (Exception ex)
            {
                Log.Info("VigiaDibujo: no se pudo enganchar a la vista activa: " + ex.Message);
            }
        }

        private static void AlEmpezar(IDisplay display)
        {
            _profundidad++;
        }

        private static void AlTerminar(IDisplay display)
        {
            if (_profundidad > 0)
                _profundidad--;
        }
    }
}
