using System;
using System.IO;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.esriSystem;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// add_layer / remove_layer / apply_symbology_from_layer — contrato JSON que
    /// esperan los schemas del servidor MCP. Equivalen al Layer(ruta)/AddLayer/
    /// RemoveLayer/UpdateLayer de arcpy.mapping; aquí: factory por tipo de fuente
    /// + IMapLayers, y renderer clonado desde el .lyr vía IGeoFeatureLayer
    /// (UpdateLayer no existe en ArcObjects .NET).
    /// </summary>
    internal static class LayerHandlers
    {
        public static JObject AddLayer(JObject parameters)
        {
            string fuente = (string)parameters["fuente"];
            if (string.IsNullOrEmpty(fuente))
                throw new ArgumentException("Indica 'fuente' (ruta a .shp, feature class de .gdb o ráster).");
            string posicion = ((string)parameters["posicion"] ?? "TOP").ToUpperInvariant();
            if (posicion != "TOP" && posicion != "BOTTOM" && posicion != "AUTO_ARRANGE")
                throw new ArgumentException("'posicion' debe ser TOP, BOTTOM o AUTO_ARRANGE.");
            string grupo = (string)parameters["grupo"];

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer nueva = DataAccess.CrearCapaDesdeRuta(fuente);
            IMapLayers mapLayers = (IMapLayers)map;

            if (!string.IsNullOrEmpty(grupo))
            {
                IGroupLayer grp = MapHandlers.FindLayer(map, grupo) as IGroupLayer;
                if (grp == null)
                    throw new ArgumentException("La capa indicada en 'grupo' no es una capa de grupo: " + grupo);
                if (posicion == "AUTO_ARRANGE")
                    mapLayers.InsertLayerInGroup(grp, nueva, true, -1);
                else
                    mapLayers.InsertLayerInGroup(grp, nueva, false,
                        posicion == "BOTTOM" ? ((ICompositeLayer)grp).Count : 0);
            }
            else
            {
                if (posicion == "AUTO_ARRANGE")
                    mapLayers.InsertLayer(nueva, true, -1);
                else
                    mapLayers.InsertLayer(nueva, false, posicion == "BOTTOM" ? map.LayerCount : 0);
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = nueva.Name,
                ["fuente"] = fuente,
                ["posicion"] = posicion,
                ["grupo"] = grupo
            });
        }

        public static JObject RemoveLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);

            // Si la capa vive dentro de un grupo hay que borrarla del grupo;
            // IMap.DeleteLayer solo garantiza las de primer nivel.
            IGroupLayer padre = BuscarGrupoPadre(map, lyr);
            if (padre != null)
                padre.Delete(lyr);
            else
                map.DeleteLayer(lyr);

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = capa,
                ["eliminada"] = true
            });
        }

        public static JObject ApplySymbologyFromLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string lyrFile = (string)parameters["lyr_file"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(lyrFile))
                throw new ArgumentException("Indica 'capa' y 'lyr_file' (ruta al .lyr de origen).");
            if (!File.Exists(lyrFile))
                throw new ArgumentException("No existe el archivo .lyr: " + lyrFile);

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            IGeoFeatureLayer destino = MapHandlers.FindLayer(map, capa) as IGeoFeatureLayer;
            if (destino == null)
                throw new ArgumentException("Solo capas de entidades admiten simbología desde .lyr: " + capa);

            ILayerFile lf = new LayerFileClass();
            lf.Open(lyrFile);
            try
            {
                IGeoFeatureLayer origen = PrimerGeoFeatureLayer(lf.Layer);
                if (origen == null)
                    throw new ArgumentException("El .lyr no contiene una capa de entidades: " + lyrFile);

                // Geometrías incompatibles → el renderer no aplicaría (mismo guard
                // implícito del UpdateLayer de arcpy.mapping). Si una fuente está
                // rota no se puede comprobar: se intenta igualmente.
                try
                {
                    if (destino.FeatureClass != null && origen.FeatureClass != null
                        && destino.FeatureClass.ShapeType != origen.FeatureClass.ShapeType)
                        throw new ArgumentException("Geometrías incompatibles entre la capa ("
                            + DataAccess.NombreTipoGeometria(destino.FeatureClass.ShapeType)
                            + ") y el .lyr (" + DataAccess.NombreTipoGeometria(origen.FeatureClass.ShapeType) + ").");
                }
                catch (ArgumentException) { throw; }
                catch { /* FeatureClass inaccesible: no bloquear por el guard */ }

                // Clonar el renderer para no dejar referencias vivas al layer file.
                IObjectCopy copia = new ObjectCopyClass();
                destino.Renderer = (IFeatureRenderer)copia.Copy(origen.Renderer);
            }
            finally
            {
                try { lf.Close(); } catch { }
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = capa,
                ["lyr_origen"] = lyrFile
            });
        }

        /// <summary>Primer IGeoFeatureLayer dentro de una capa (la propia, o la
        /// primera de un grupo, recursivo) — un .lyr puede envolver un grupo.</summary>
        private static IGeoFeatureLayer PrimerGeoFeatureLayer(ILayer lyr)
        {
            IGeoFeatureLayer gf = lyr as IGeoFeatureLayer;
            if (gf != null)
                return gf;
            ICompositeLayer comp = lyr as ICompositeLayer;
            if (comp == null)
                return null;
            for (int i = 0; i < comp.Count; i++)
            {
                IGeoFeatureLayer hijo = PrimerGeoFeatureLayer(comp.get_Layer(i));
                if (hijo != null)
                    return hijo;
            }
            return null;
        }

        private static IGroupLayer BuscarGrupoPadre(IMap map, ILayer objetivo)
        {
            for (int i = 0; i < map.LayerCount; i++)
            {
                if (ReferenceEquals(map.get_Layer(i), objetivo))
                    return null; // de primer nivel: la borra IMap.DeleteLayer
                IGroupLayer padre = BuscarPadreEn(map.get_Layer(i), objetivo);
                if (padre != null)
                    return padre;
            }
            return null;
        }

        private static IGroupLayer BuscarPadreEn(ILayer candidato, ILayer objetivo)
        {
            ICompositeLayer comp = candidato as ICompositeLayer;
            if (comp == null)
                return null;
            for (int i = 0; i < comp.Count; i++)
            {
                ILayer hijo = comp.get_Layer(i);
                if (ReferenceEquals(hijo, objetivo))
                    return candidato as IGroupLayer;
                IGroupLayer anidado = BuscarPadreEn(hijo, objetivo);
                if (anidado != null)
                    return anidado;
            }
            return null;
        }
    }
}
