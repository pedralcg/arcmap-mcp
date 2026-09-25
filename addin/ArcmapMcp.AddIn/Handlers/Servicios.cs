using System;
using System.Collections.Generic;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.GISClient;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Capas de servicio (WMS) para add_layer.
    ///
    /// Receta probada en standalone el 2026-09-25 (Catastro e IGN para un MXD real):
    /// PropertySet con URL → WMSConnectionName → WMSMapLayer.Connect.
    ///
    /// La trampa: el WMSMapLayer recién conectado trae TODAS las subcapas apagadas,
    /// y encender el nodo padre no pinta nada. Aquí se encienden las pedidas (o todas),
    /// con los grupos que las contienen, y se devuelve el árbol leído de vuelta.
    /// </summary>
    internal static class Servicios
    {
        public static bool EsUrl(string fuente)
        {
            return fuente.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
                || fuente.StartsWith("https://", StringComparison.OrdinalIgnoreCase);
        }

        private sealed class Nodo
        {
            public ILayer Capa;
            public string Ruta;
            public Nodo Padre;
            public bool EsGrupo;
        }

        /// <summary>Conecta el WMS y deja encendidas las subcapas pedidas (null = todas).
        /// Si alguna pedida no existe, falla ANTES de añadir nada al mapa.</summary>
        public static ILayer CrearWms(string url, List<string> subcapas, out JObject info)
        {
            IPropertySet ps = new PropertySetClass();
            ps.SetProperty("URL", url);
            IWMSConnectionName cn = new WMSConnectionNameClass();
            cn.ConnectionProperties = ps;
            IWMSGroupLayer wms = new WMSMapLayerClass();
            bool conectado;
            try
            {
                conectado = ((IDataLayer)wms).Connect((IName)cn);
            }
            catch (Exception ex)
            {
                throw new InvalidOperationException("No se pudo conectar al WMS " + url + ": " + ex.Message, ex);
            }
            if (!conectado)
                throw new InvalidOperationException("No se pudo conectar al WMS " + url
                    + " (Connect devolvió false). ¿Responde el servidor? ¿Es la URL del servicio WMS?");

            var nodos = new List<Nodo>();
            Recorrer((ICompositeLayer)wms, "", null, nodos);
            if (nodos.Count == 0)
                throw new InvalidOperationException("El WMS " + url + " no publica ninguna capa.");

            var encender = new HashSet<Nodo>();
            if (subcapas == null)
                encender.UnionWith(nodos);
            else
            {
                var faltan = new List<string>();
                foreach (string pedida in subcapas)
                {
                    List<Nodo> casan = nodos.FindAll(n => string.Equals(n.Ruta, pedida, StringComparison.OrdinalIgnoreCase));
                    if (casan.Count == 0)
                        casan = nodos.FindAll(n => string.Equals(n.Capa.Name, pedida, StringComparison.OrdinalIgnoreCase));
                    if (casan.Count == 0)
                        faltan.Add(pedida);
                    foreach (Nodo n in casan)
                    {
                        // Un grupo pedido se enciende con todo lo que tiene debajo.
                        foreach (Nodo d in nodos)
                            if (d == n || d.Ruta.StartsWith(n.Ruta + "/", StringComparison.OrdinalIgnoreCase))
                                encender.Add(d);
                    }
                }
                if (faltan.Count > 0)
                    throw new ArgumentException("El WMS no tiene " + (faltan.Count == 1 ? "la subcapa " : "las subcapas ")
                        + string.Join(", ", faltan.ConvertAll(f => "'" + f + "'")) + ". Tiene: "
                        + string.Join(" | ", Recortar(nodos.ConvertAll(n => n.Ruta), 40)));
            }
            // Una subcapa encendida bajo un grupo apagado no pinta: se encienden sus padres.
            foreach (Nodo n in new List<Nodo>(encender))
                for (Nodo p = n.Padre; p != null; p = p.Padre)
                    encender.Add(p);
            foreach (Nodo n in nodos)
                n.Capa.Visible = encender.Contains(n);

            var arbol = new JArray();
            var encendidas = new JArray();
            foreach (Nodo n in nodos)
            {
                if (arbol.Count < 200)
                    arbol.Add(new JObject { ["ruta"] = n.Ruta, ["grupo"] = n.EsGrupo, ["visible"] = n.Capa.Visible });
                if (n.Capa.Visible && !n.EsGrupo)
                    encendidas.Add(n.Ruta);
            }
            info = new JObject
            {
                ["tipo"] = "WMS",
                ["url"] = url,
                ["num_subcapas"] = nodos.Count,
                ["subcapas_encendidas"] = encendidas,
                ["arbol"] = arbol,
                ["arbol_truncado"] = nodos.Count > arbol.Count
            };
            return (ILayer)wms;
        }

        private static void Recorrer(ICompositeLayer comp, string prefijo, Nodo padre, List<Nodo> salida)
        {
            for (int i = 0; i < comp.Count; i++)
            {
                ILayer l = comp.get_Layer(i);
                ICompositeLayer hijo = l as ICompositeLayer;
                var n = new Nodo
                {
                    Capa = l,
                    Ruta = prefijo.Length == 0 ? l.Name : prefijo + "/" + l.Name,
                    Padre = padre,
                    EsGrupo = hijo != null && hijo.Count > 0
                };
                salida.Add(n);
                if (hijo != null)
                    Recorrer(hijo, n.Ruta, n, salida);
            }
        }

        private static List<string> Recortar(List<string> l, int max)
        {
            if (l.Count <= max)
                return l;
            List<string> r = l.GetRange(0, max);
            r.Add("… (" + (l.Count - max) + " más)");
            return r;
        }
    }
}
