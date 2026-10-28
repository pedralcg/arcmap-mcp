using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// copy_layout_elements — copia elementos del layout (los logos del cajetín, sobre todo) de
    /// otro .mxd al documento, en la misma posición, y opcionalmente quita antes los viejos.
    ///
    /// Es clonar_logos_ao.py de una serie de montes: el logo de la UE y el de la Consejería (incrustado y SIN
    /// nombre, dentro del grupo del cajetín) se copiaban de un plano bueno al resto, borrando los
    /// de 2018. Por eso se busca también dentro de los grupos y se puede elegir por región.
    /// Se copia con IClone: una imagen incrustada viaja con sus bytes.
    ///
    /// El origen se abre con MapDocument y se cierra sin guardar. El servidor ya ha rechazado un
    /// origen con marcos OLE (abrirlo desde fuera con ArcMap abierto se queda colgado).
    /// </summary>
    internal static class CopiaElementosHandlers
    {
        private static readonly string[] TiposValidos = { "imagen", "texto", "grafico" };

        public static JObject CopyLayoutElements(JObject parameters)
        {
            string origen = (string)parameters["origen"];
            if (string.IsNullOrEmpty(origen) || !System.IO.Path.IsPathRooted(origen))
                throw new ArgumentException("Indica 'origen': la ruta ABSOLUTA del .mxd del que copiar.");
            if (!File.Exists(origen))
                throw new ArgumentException("No existe el .mxd de origen: " + origen);
            List<string> nombres = LeerLista(parameters["nombres"], "nombres");
            double[] region = LeerCaja(parameters["region"], "region");
            if (nombres == null && region == null)
                throw new ArgumentException("Indica qué copiar: 'nombres' (de los elementos en el origen) y/o 'region'"
                    + " ([xmin, ymin, xmax, ymax] en unidades de página: los que tengan el centro dentro).");
            List<string> tipos = LeerLista(parameters["tipos"], "tipos") ?? new List<string> { "imagen" };
            foreach (string t in tipos)
                if (!TiposValidos.Contains(t))
                    throw new ArgumentException("'tipos' admite " + string.Join(", ", TiposValidos) + " (no '" + t + "').");
            List<string> quitar = LeerLista(parameters["quitar"], "quitar");

            Documento doc = Documento.Actual();
            IGraphicsContainer gcDestino = (IGraphicsContainer)doc.PageLayout;

            // 1. Los elementos a quitar, antes de abrir el origen: si falta alguno no se abre nada.
            var aQuitar = new List<IElement>();
            if (quitar != null)
            {
                List<LayoutHandlers.ElementoLayout> destino = LayoutHandlers.Elementos(gcDestino);
                foreach (string q in quitar)
                {
                    var sueltos = destino.Where(e => e.Grupo == null
                        && string.Equals(e.Nombre, q, StringComparison.OrdinalIgnoreCase)).ToList();
                    if (sueltos.Count == 0)
                    {
                        bool enGrupo = destino.Any(e => string.Equals(e.Nombre, q, StringComparison.OrdinalIgnoreCase));
                        throw new ArgumentException("No hay ningún elemento suelto llamado '" + q + "' que quitar"
                            + (enGrupo ? " (lo hay dentro de un grupo: desagrúpalo antes, o quita el grupo entero por su nombre)."
                                       : ". Elementos con nombre: " + string.Join(", ", destino.Where(e => e.Nombre.Length > 0)
                                           .Select(e => e.Nombre).Distinct())));
                    }
                    // Por nombre se alcanzaba CUALQUIER suelto: quitar=["Layers"] borraba el marco del
                    // data frame (revisión del 2026-10-25). Los marcos de mapa, de leyenda/escala y los
                    // OLE no se quitan nunca; lo demás, si es de los `tipos` pedidos o un grupo entero.
                    foreach (var e in sueltos)
                    {
                        if (e.Elemento is IMapFrame || e.Elemento is IMapSurroundFrame || e.Elemento is IOleFrame)
                            throw new ArgumentException("'" + q + "' es un marco de mapa, de leyenda/escala o un objeto"
                                + " incrustado: copy_layout_elements no lo quita.");
                        if (!(e.Elemento is IGroupElement) && !tipos.Contains(Tipo(e.Elemento)))
                            throw new ArgumentException("'" + q + "' es de tipo '" + Tipo(e.Elemento) + "' y 'tipos' es "
                                + string.Join(", ", tipos) + ": añádelo a 'tipos' si de verdad hay que quitarlo.");
                    }
                    aQuitar.AddRange(sueltos.Select(e => e.Elemento));
                }
            }

            // 2. Clonar del origen.
            var clones = new List<Tuple<IElement, JObject>>();
            double anchoOrigen, altoOrigen;
            IMapDocument md = new MapDocumentClass();
            try
            {
                md.Open(origen, "");
                md.PageLayout.Page.QuerySize(out anchoOrigen, out altoOrigen);
                List<LayoutHandlers.ElementoLayout> todos = LayoutHandlers.Elementos((IGraphicsContainer)md.PageLayout);
                var candidatos = todos.Where(e => tipos.Contains(Tipo(e.Elemento))).ToList();
                var elegidos = candidatos.Where(e =>
                        (nombres == null || nombres.Any(n => string.Equals(n, e.Nombre, StringComparison.OrdinalIgnoreCase)))
                        && (region == null || CentroDentro(e.Elemento, region)))
                    .ToList();
                if (elegidos.Count == 0)
                    throw new ArgumentException("En el origen no hay ningún elemento de tipo " + string.Join("/", tipos)
                        + " que case" + (nombres != null ? " con los nombres " + string.Join(", ", nombres) : "")
                        + (region != null ? " con el centro en la región" : "") + ". Hay: "
                        + string.Join(" | ", candidatos.Select(Describir)));
                if (nombres != null)
                {
                    var faltan = nombres.Where(n => !elegidos.Any(e => string.Equals(e.Nombre, n, StringComparison.OrdinalIgnoreCase))).ToList();
                    if (faltan.Count > 0)
                        throw new ArgumentException("En el origen no están (con esos tipos" + (region != null ? " y en la región" : "")
                            + "): " + string.Join(", ", faltan) + ". Hay: " + string.Join(" | ", candidatos.Select(Describir)));
                }
                foreach (LayoutHandlers.ElementoLayout e in elegidos)
                {
                    IElement copia = (IElement)((IClone)e.Elemento).Clone();
                    clones.Add(Tuple.Create(copia, new JObject
                    {
                        ["nombre"] = e.Nombre,
                        ["tipo"] = Tipo(e.Elemento),
                        ["del_grupo"] = e.Grupo
                    }));
                }
            }
            finally
            {
                try { md.Close(); } catch { }
            }

            // 3. Quitar y pegar.
            foreach (IElement el in aQuitar)
                gcDestino.DeleteElement(el);
            var copiados = new JArray();
            foreach (var c in clones)
            {
                gcDestino.AddElement(c.Item1, 0);
                IEnvelope env = c.Item1.Geometry.Envelope;
                c.Item2["caja"] = new JArray(Math.Round(env.XMin, 3), Math.Round(env.YMin, 3),
                                             Math.Round(env.XMax, 3), Math.Round(env.YMax, 3));
                copiados.Add(c.Item2);
            }
            doc.RefrescarGraficosLayout();
            doc.Refrescar();

            var r = new JObject
            {
                ["origen"] = origen,
                ["copiados"] = copiados,
                ["quitados"] = quitar == null ? 0 : aQuitar.Count
            };
            double ancho, alto;
            doc.PageLayout.Page.QuerySize(out ancho, out alto);
            if (Math.Abs(ancho - anchoOrigen) > 0.01 || Math.Abs(alto - altoOrigen) > 0.01)
                r["aviso"] = "La página del origen (" + Math.Round(anchoOrigen, 2) + " x " + Math.Round(altoOrigen, 2)
                    + ") no mide lo mismo que esta (" + Math.Round(ancho, 2) + " x " + Math.Round(alto, 2)
                    + "): se han pegado en las mismas coordenadas, revisa dónde han caído.";
            return Protocol.Result(r);
        }

        private static string Tipo(IElement el)
        {
            // Las imágenes también son IFrameElement (tienen marco): se miran antes, y de los marcos
            // solo se descartan los que dependen de un mapa o son objetos incrustados.
            if (el is IPictureElement) return "imagen";
            if (el is IMapFrame || el is IMapSurroundFrame || el is IOleFrame || el is IGroupElement)
                return "otro";
            if (el is ITextElement) return "texto";
            return "grafico";
        }

        private static bool CentroDentro(IElement el, double[] caja)
        {
            IEnvelope env = el.Geometry.Envelope;
            double cx = (env.XMin + env.XMax) / 2, cy = (env.YMin + env.YMax) / 2;
            return cx >= caja[0] && cx <= caja[2] && cy >= caja[1] && cy <= caja[3];
        }

        private static string Describir(LayoutHandlers.ElementoLayout e)
        {
            IEnvelope env = e.Elemento.Geometry.Envelope;
            return (e.Nombre.Length > 0 ? "'" + e.Nombre + "'" : "(sin nombre)") + " " + Tipo(e.Elemento)
                + (e.Grupo != null ? " en " + e.Grupo : "")
                // Con punto decimal: con la coma de la cultura, «[36,09, 26, 39,47, 27,2]» no se
                // podía leer como la caja que pide 'region' (2026-10-10).
                + " [" + string.Join(", ", new[] { env.XMin, env.YMin, env.XMax, env.YMax }.Select(
                    v => Math.Round(v, 2).ToString(System.Globalization.CultureInfo.InvariantCulture))) + "]";
        }

        private static List<string> LeerLista(JToken t, string nombre)
        {
            if (!Parametros.Dado(t))
                return null;
            JArray a = t as JArray;
            if (a == null || a.Count == 0 || a.Any(v => v.Type != JTokenType.String || ((string)v).Trim().Length == 0))
                throw new ArgumentException("'" + nombre + "' es una lista de textos no vacía.");
            return a.Select(v => ((string)v).Trim()).ToList();
        }

        private static double[] LeerCaja(JToken t, string nombre)
        {
            if (!Parametros.Dado(t))
                return null;
            JArray a = t as JArray;
            if (a == null || a.Count != 4)
                throw new ArgumentException("'" + nombre + "' es [xmin, ymin, xmax, ymax] en unidades de página.");
            double[] c = a.Select(v => (double)v).ToArray();
            if (c[2] <= c[0] || c[3] <= c[1])
                throw new ArgumentException("'" + nombre + "' mal formada: xmax > xmin e ymax > ymin.");
            return c;
        }
    }
}
