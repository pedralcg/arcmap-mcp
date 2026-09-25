using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// move_layer — recolocar una capa en la TOC sin quitarla y volverla a añadir.
    ///
    /// El hueco (2026-09-25, montando un MXD real): add_layer solo sabe TOP / BOTTOM,
    /// así que meter un grupo entre otros dos obligaba a quitar las capas de debajo y
    /// volver a añadirlas en orden, lo que pierde la simbología que no venga de un
    /// .lyr y todo lo tocado a mano. IMapLayers.MoveLayerEx mueve el MISMO objeto de
    /// capa, también entre grupos, y la leyenda conserva sus elementos.
    ///
    /// El índice que se pide a MoveLayerEx se calcula sobre la lista del destino SIN
    /// la capa, y se comprueba leyéndolo de vuelta: la respuesta trae el orden
    /// resultante del grupo, no el que se quería.
    /// </summary>
    internal static class TocHandlers
    {
        /// <summary>Valor de 'grupo' que significa «la raíz de la TOC».</summary>
        private const string Raiz = "/";

        public static JObject MoveLayer(JObject parameters)
        {
            string capa = Texto(parameters["capa"]);
            if (capa == null)
                throw new ArgumentException("Indica 'capa' (nombre en la TOC, o ruta 'Grupo/Capa').");
            string referencia = Texto(parameters["referencia"]);
            string grupo = Texto(parameters["grupo"]);
            string posicion = (Texto(parameters["posicion"]) ?? (referencia != null ? "BEFORE" : "TOP")).ToUpperInvariant();

            if (posicion != "BEFORE" && posicion != "AFTER" && posicion != "TOP" && posicion != "BOTTOM")
                throw new ArgumentException("'posicion' debe ser BEFORE, AFTER, TOP o BOTTOM.");
            if ((posicion == "BEFORE" || posicion == "AFTER") && referencia == null)
                throw new ArgumentException("Con posicion " + posicion + " hay que indicar 'referencia' (la capa"
                    + " junto a la que se coloca).");
            if ((posicion == "TOP" || posicion == "BOTTOM") && referencia != null)
                throw new ArgumentException("Con posicion " + posicion + " no se usa 'referencia': TOP y BOTTOM"
                    + " son del grupo ('grupo', o el de la capa si no se indica). Para ponerla junto a otra,"
                    + " BEFORE o AFTER.");
            if (referencia != null && grupo != null)
                throw new ArgumentException("'referencia' y 'grupo' se excluyen: con referencia, la capa va al"
                    + " grupo de la referencia.");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
            IGroupLayer desde = LayerHandlers.BuscarGrupoPadre(map, lyr);

            IGroupLayer hacia;
            ILayer refLyr = null;
            if (referencia != null)
            {
                refLyr = MapHandlers.FindLayer(map, referencia);
                if (ReferenceEquals(refLyr, lyr))
                    throw new ArgumentException("'capa' y 'referencia' son la misma capa.");
                hacia = LayerHandlers.BuscarGrupoPadre(map, refLyr);
            }
            else if (grupo == null)
                hacia = desde;
            else if (grupo == Raiz)
                hacia = null;
            else
            {
                hacia = MapHandlers.FindLayer(map, grupo) as IGroupLayer;
                if (hacia == null)
                    throw new ArgumentException("'" + grupo + "' no es una capa de grupo. Para la raíz de la TOC,"
                        + " grupo=\"" + Raiz + "\".");
            }
            if (hacia != null && (ReferenceEquals(hacia, lyr) || Contiene(lyr, (ILayer)hacia)))
                throw new ArgumentException("No se puede meter un grupo dentro de sí mismo.");

            List<ILayer> sinElla = Hijos(map, hacia);
            sinElla.RemoveAll(l => ReferenceEquals(l, lyr));
            int k;
            if (refLyr != null)
            {
                int j = sinElla.FindIndex(l => ReferenceEquals(l, refLyr));
                k = posicion == "BEFORE" ? j : j + 1;
            }
            else
                k = posicion == "TOP" ? 0 : sinElla.Count;

            IMapLayers ml = (IMapLayers)map;
            ml.MoveLayerEx(desde, hacia, lyr, k);
            int final = Hijos(map, hacia).FindIndex(l => ReferenceEquals(l, lyr));
            if (final != k)
            {
                // Dentro del mismo grupo, MoveLayerEx no ha documentado si el índice
                // cuenta con la capa o sin ella: se corrige una vez, ya sin ambigüedad.
                ml.MoveLayerEx(hacia, hacia, lyr, k);
                final = Hijos(map, hacia).FindIndex(l => ReferenceEquals(l, lyr));
            }
            MapHandlers.NotificarCambioContenido(map, doc);

            var orden = new JArray();
            foreach (ILayer l in Hijos(map, hacia))
                orden.Add(l.Name);
            if (final != k)
                throw new InvalidOperationException("La capa no quedó donde se pedía (posición " + final + ", se"
                    + " pedía " + k + "). Orden actual: " + string.Join(" | ", ToStrings(orden)));
            return Protocol.Result(new JObject
            {
                ["capa"] = lyr.Name,
                ["grupo"] = hacia == null ? null : ((ILayer)hacia).Name,
                ["desde_grupo"] = desde == null ? null : ((ILayer)desde).Name,
                ["posicion"] = posicion,
                ["referencia"] = referencia,
                ["indice"] = final,
                ["orden"] = orden
            });
        }

        /// <summary>Capas hijas directas de un grupo, o de la raíz si grupo es null.</summary>
        private static List<ILayer> Hijos(IMap map, IGroupLayer grupo)
        {
            var salida = new List<ILayer>();
            if (grupo == null)
            {
                for (int i = 0; i < map.LayerCount; i++)
                    salida.Add(map.get_Layer(i));
            }
            else
            {
                ICompositeLayer comp = (ICompositeLayer)grupo;
                for (int i = 0; i < comp.Count; i++)
                    salida.Add(comp.get_Layer(i));
            }
            return salida;
        }

        private static bool Contiene(ILayer grupo, ILayer buscada)
        {
            ICompositeLayer comp = grupo as ICompositeLayer;
            if (comp == null)
                return false;
            for (int i = 0; i < comp.Count; i++)
            {
                ILayer h = comp.get_Layer(i);
                if (ReferenceEquals(h, buscada) || Contiene(h, buscada))
                    return true;
            }
            return false;
        }

        private static string[] ToStrings(JArray a)
        {
            var s = new string[a.Count];
            for (int i = 0; i < a.Count; i++)
                s[i] = (string)a[i];
            return s;
        }

        private static string Texto(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            string s = ((string)t).Trim();
            return s.Length == 0 ? null : s;
        }
    }
}
