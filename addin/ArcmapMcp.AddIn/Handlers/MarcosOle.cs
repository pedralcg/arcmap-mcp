using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Marcos OLE (objetos incrustados, p. ej. un documento de Word) del documento vivo.
    ///
    /// Medido el 2026-09-26 con un plano real que llevaba dos, fuera de la página: con ArcMap
    /// ABIERTO, un arcpy de fuera que carga ese documento se queda esperando para siempre. La
    /// clase del marco OLE no está en el registro: la sirve ArcMap desde su propio proceso, así
    /// que el arcpy de fuera acaba llamando al ArcMap en marcha, y esa llamada no vuelve. Con
    /// ArcMap cerrado la creación falla al instante y el marco queda como PlaceholderElement.
    /// Quitar SOLO esos dos marcos lo arreglaba (con documentos de control antes y después).
    /// Secuela: después, ArcMap no termina al cerrarlo.
    ///
    /// Por eso lo que abre una copia del documento fuera de ArcMap (execute_code con documento,
    /// las tres de DDP y la verificación de save_mxd) mira esto antes y no lanza el arcpy.
    /// Corre en el hilo de ArcMap.
    /// </summary>
    internal static class MarcosOle
    {
        /// <summary>Prefijo del error. El servidor MCP lo reconoce para no verificar el guardado.</summary>
        internal const string Prefijo = "marcos_ole: ";

        internal static JArray Buscar(IMxDocument doc)
        {
            var encontrados = new JArray();
            IPage page = doc.PageLayout.Page;
            double ancho, alto;
            page.QuerySize(out ancho, out alto);
            Recorrer((IGraphicsContainer)doc.PageLayout, "layout", ancho, alto, encontrados);
            IMaps maps = doc.Maps;
            for (int i = 0; i < maps.Count; i++)
                Recorrer((IGraphicsContainer)maps.get_Item(i), "marco de datos '" + maps.get_Item(i).Name + "'",
                         -1, -1, encontrados);
            return encontrados;
        }

        private static void Recorrer(IGraphicsContainer gc, string donde, double ancho, double alto, JArray salida)
        {
            var lista = new List<IElement>();
            gc.Reset();
            IElement e;
            while ((e = gc.Next()) != null)
                lista.Add(e);
            foreach (IElement el in lista)
            {
                Anotar(el, donde, ancho, alto, salida);
                IGroupElement g = el as IGroupElement;
                if (g != null)
                    for (int i = 0; i < g.ElementCount; i++)
                        Anotar(g.get_Element(i), donde + " (dentro de un grupo)", ancho, alto, salida);
            }
        }

        private static void Anotar(IElement el, string donde, double ancho, double alto, JArray salida)
        {
            if (!(el is IOleFrame))
                return;
            var m = new JObject { ["donde"] = donde };
            try
            {
                IEnvelope env = el.Geometry.Envelope;
                m["caja"] = new JArray(Math.Round(env.XMin, 1), Math.Round(env.YMin, 1),
                                       Math.Round(env.XMax, 1), Math.Round(env.YMax, 1));
                if (ancho > 0)
                    m["fuera_de_la_pagina"] = env.XMax < 0 || env.YMax < 0 || env.XMin > ancho || env.YMin > alto;
            }
            catch (Exception ex)
            {
                m["caja"] = "no se pudo leer: " + ex.Message;
            }
            try
            {
                string nombre = ((IElementProperties)el).Name;
                if (!string.IsNullOrEmpty(nombre))
                    m["nombre"] = nombre;
            }
            catch { /* sin nombre */ }
            try
            {
                // El CLSID va al log, no a la respuesta: sirve para reconocer estos marcos
                // en un .mxd en disco sin abrirlo, que es lo siguiente que habría que hacer.
                Guid clsid;
                ((IPersist)el).GetClassID(out clsid);
                Log.Info("Marco OLE en " + donde + ": clase {" + clsid + "}");
            }
            catch { /* best-effort */ }
            salida.Add(m);
        }

        /// <summary>Lanza si el documento vivo tiene marcos OLE: lo que se iba a hacer abriría
        /// una copia fuera de ArcMap, y se quedaría colgado.</summary>
        internal static void ComprobarAntesDeCopia(IMxDocument doc, string que)
        {
            JArray m = Buscar(doc);
            if (m.Count == 0)
                return;
            throw new InvalidOperationException(Prefijo + "el documento tiene " + m.Count
                + " marco(s) OLE (objetos incrustados, p. ej. de Word): " + m.ToString(Newtonsoft.Json.Formatting.None)
                + ". " + que + " abre una copia del documento con el arcpy de fuera, y con ArcMap abierto"
                + " ese arcpy se queda esperando a este ArcMap para siempre al cargar un marco OLE"
                + " (fallo de ArcMap 10.5, no del add-in). No se ha lanzado. Opciones: quita esos marcos"
                + " (si caen fuera de la página no se imprimen) o hazlo con ArcMap cerrado.");
        }
    }
}
