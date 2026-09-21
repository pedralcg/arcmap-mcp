using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geodatabase;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// set_scale / set_extent / set_layer_visibility / set_definition_query —
    /// contrato JSON que esperan los schemas del servidor MCP. La búsqueda de
    /// capa devuelve un error accionable con la lista de capas disponibles si
    /// el nombre no existe.
    /// </summary>
    internal static class MapHandlers
    {
        /// <summary>Una capa de la TOC con su ruta de grupo ("Grupo/Subgrupo/Capa"),
        /// que es lo único que distingue dos capas con el mismo nombre.</summary>
        internal sealed class CapaEnMapa
        {
            public ILayer Capa;
            public string Nombre;
            public string Ruta;
            /// <summary>Solo en capas numeradas ("G/Capa#2"): su ruta sin el "#n". Hace
            /// que pedir "G/Capa" siga casando con las dos —y dé el error que lista las
            /// numeradas— en vez de un "capa no encontrada" que sería mentira.</summary>
            public string RutaSinNumero;
        }

        /// <summary>
        /// Todas las capas del data frame, grupos incluidos y en orden de TOC, con
        /// su ruta de grupo. Sustituye a `IMap.get_Layers(null, true)`, que LANZA
        /// E_FAIL con un data frame VACÍO: el add-in lo llamaba sin proteger en seis
        /// sitios, así que un mxd recién creado hacía fallar `list_layers` o
        /// `run_geoprocessing` por no tener capas todavía. Aquí un data frame vacío
        /// devuelve una lista vacía, que es la respuesta correcta.
        /// </summary>
        internal static List<CapaEnMapa> CapasConRuta(IMap map)
        {
            var salida = new List<CapaEnMapa>();
            if (map == null)
                return salida;
            int n;
            try { n = map.LayerCount; }
            catch { return salida; }
            for (int i = 0; i < n; i++)
            {
                ILayer lyr;
                try { lyr = map.get_Layer(i); }
                catch { continue; }
                Recoger(lyr, "", salida);
            }
            NumerarRutasRepetidas(salida);
            return salida;
        }

        /// <summary>
        /// Dos capas con el mismo nombre DENTRO DEL MISMO contenedor tienen la misma
        /// ruta de grupo, así que la ruta sola no las separa: basta añadir dos veces el
        /// mismo shapefile, que ArcMap permite sin renombrar. Como un nombre ambiguo es
        /// error y no "la primera", sin esto las dos quedaban INOPERABLES para todas las
        /// tools, `remove_layer` incluida: ni siquiera se podía deshacer el duplicado
        /// (lo cazó la verificación adversarial del 2026-09-20, antes de publicarse).
        /// Se desempata con el orden de la TOC: "Parcelas#1", "Parcelas#2". Solo llevan
        /// sufijo las rutas que se repiten; las únicas se quedan como estaban.
        /// </summary>
        private static void NumerarRutasRepetidas(List<CapaEnMapa> capas)
        {
            var cuantas = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            foreach (CapaEnMapa c in capas)
            {
                int n;
                cuantas.TryGetValue(c.Ruta, out n);
                cuantas[c.Ruta] = n + 1;
            }
            var vistas = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
            foreach (CapaEnMapa c in capas)
            {
                if (cuantas[c.Ruta] < 2)
                    continue;
                int orden;
                vistas.TryGetValue(c.Ruta, out orden);
                vistas[c.Ruta] = ++orden;
                c.RutaSinNumero = c.Ruta;
                c.Ruta = c.Ruta + "#" + orden;
            }
        }

        /// <summary>Las capas del data frame sin la ruta, para quien solo recorre.</summary>
        internal static List<ILayer> Capas(IMap map)
        {
            var salida = new List<ILayer>();
            foreach (CapaEnMapa c in CapasConRuta(map))
                salida.Add(c.Capa);
            return salida;
        }

        private static void Recoger(ILayer lyr, string prefijo, List<CapaEnMapa> destino)
        {
            if (lyr == null)
                return;
            string nombre = NombreSeguro(lyr);
            string ruta = prefijo.Length == 0 ? nombre : prefijo + "/" + nombre;
            destino.Add(new CapaEnMapa { Capa = lyr, Nombre = nombre, Ruta = ruta });

            // Se desciende por ICompositeLayer, no solo por IGroupLayer: es lo que
            // hace get_Layers(null, true), y así el listado no cambia de contenido.
            ICompositeLayer comp = lyr as ICompositeLayer;
            if (comp == null)
                return;
            int n;
            try { n = comp.Count; }
            catch { return; }
            for (int i = 0; i < n; i++)
            {
                ILayer hijo;
                try { hijo = comp.get_Layer(i); }
                catch { continue; }
                Recoger(hijo, ruta, destino);
            }
        }

        internal static string NombreSeguro(ILayer lyr)
        {
            try { return lyr.Name ?? "(sin nombre)"; }
            catch { return "(sin nombre)"; }
        }

        /// <summary>
        /// Capa por nombre, o por ruta de grupo si el nombre se repite.
        ///
        /// Antes se quedaba con la PRIMERA coincidencia en silencio, y eso en
        /// `remove_layer` borra una capa que nadie pidió: dos grupos de una serie de
        /// planos con una capa "Límite" cada uno son indistinguibles por nombre. Si
        /// hay más de una candidata NO se elige: se devuelve un error con las rutas
        /// completas para que quien llama decida.
        /// </summary>
        internal static ILayer FindLayer(IMap map, string nombre)
        {
            List<CapaEnMapa> capas = CapasConRuta(map);
            List<CapaEnMapa> candidatas = Coincidencias(capas, nombre);
            if (candidatas.Count == 1)
                return candidatas[0].Capa;
            if (candidatas.Count == 0)
                throw new ArgumentException("Capa no encontrada: " + nombre
                    + ". Disponibles: " + string.Join(", ", Rutas(capas)));
            throw new ArgumentException("Hay " + candidatas.Count + " capas que casan con '" + nombre
                + "'. Repite con UNA de estas rutas, tal cual (las que acaban en #n son capas"
                + " homónimas dentro del mismo grupo, numeradas en orden de TOC): "
                + string.Join(", ", Rutas(candidatas)));
        }

        /// <summary>Capas que casan con el texto: por ruta si trae separador
        /// ("Grupo/Capa", también admite una cola de la ruta), por nombre pelado si
        /// no. OrdinalIgnoreCase, como el resto del add-in.</summary>
        internal static List<CapaEnMapa> Coincidencias(List<CapaEnMapa> capas, string nombre)
        {
            var salida = new List<CapaEnMapa>();
            string buscado = (nombre ?? "").Replace('\\', '/').Trim('/');
            if (buscado.Length == 0)
                return salida;
            // '#' también cuenta como ruta: es el desempate de NumerarRutasRepetidas
            // ("Parcelas#2"), que no lleva barra cuando la capa cuelga de la raíz.
            bool conRuta = buscado.IndexOf('/') >= 0 || buscado.IndexOf('#') >= 0;
            foreach (CapaEnMapa c in capas)
            {
                bool casa = conRuta
                    ? (CasaRuta(c.Ruta, buscado) || CasaRuta(c.RutaSinNumero, buscado))
                    : string.Equals(c.Nombre, buscado, StringComparison.OrdinalIgnoreCase);
                if (casa)
                    salida.Add(c);
            }
            return salida;
        }

        private static bool CasaRuta(string ruta, string buscado)
        {
            return ruta != null
                && (string.Equals(ruta, buscado, StringComparison.OrdinalIgnoreCase)
                    || ruta.EndsWith("/" + buscado, StringComparison.OrdinalIgnoreCase));
        }

        internal static List<string> Rutas(List<CapaEnMapa> capas)
        {
            var salida = new List<string>();
            foreach (CapaEnMapa c in capas)
                salida.Add(c.Ruta);
            return salida;
        }

        internal static IMap FocusMap(out IMxDocument doc)
        {
            IApplication app = ArcSession.App();
            doc = ArcSession.Doc(app);
            IMap map = doc.FocusMap;
            if (map == null)
                throw new InvalidOperationException("No hay data frame activo.");
            return map;
        }

        /// <summary>Tras mutar el CONTENIDO del mapa (visibilidad, def. query, alta/
        /// baja de capa, simbología) hay que llamar a IActiveView.ContentsChanged()
        /// del MAPA: es el aviso que escuchan los map surrounds. Sin él, una leyenda
        /// con "only display checked layers" se queda con el estado viejo en los
        /// exports (el clic manual en la TOC dispara el evento; el setter
        /// programático, no).</summary>
        internal static void NotificarCambioContenido(IMap map, IMxDocument doc)
        {
            ((IActiveView)map).ContentsChanged();
            doc.UpdateContents();
            doc.ActiveView.Refresh();
        }

        /// <summary>refresh — redibuja la vista activa y la TOC (equivalente a
        /// RefreshActiveView + RefreshTOC de arcpy).</summary>
        public static JObject Refresh(JObject parameters)
        {
            IMxDocument doc;
            FocusMap(out doc);
            doc.UpdateContents();
            doc.ActiveView.Refresh();
            return Protocol.Result(new JObject { ["refrescado"] = true });
        }

        public static JObject SetScale(JObject parameters)
        {
            if (parameters["escala"] == null || parameters["escala"].Type == JTokenType.Null)
                throw new ArgumentException("Indica 'escala' (ej. 200000 = 1:200.000).");
            double escala = (double)parameters["escala"];

            IMxDocument doc;
            IMap map = FocusMap(out doc);
            map.MapScale = escala;
            doc.ActiveView.Refresh();
            return Protocol.Result(new JObject { ["escala"] = map.MapScale });
        }

        public static JObject SetExtent(JObject parameters)
        {
            JArray coords = parameters["coords"] as JArray;
            string capa = (string)parameters["capa"];

            IMxDocument doc;
            IMap map = FocusMap(out doc);

            IEnvelope ext;
            if (coords != null)
            {
                if (coords.Count != 4)
                    throw new ArgumentException("'coords' debe ser [xmin, ymin, xmax, ymax].");
                ext = new EnvelopeClass();
                ext.PutCoords((double)coords[0], (double)coords[1],
                              (double)coords[2], (double)coords[3]);
            }
            else if (!string.IsNullOrEmpty(capa))
            {
                ILayer lyr = FindLayer(map, capa);
                ext = ExtentSeleccion(lyr) ?? lyr.AreaOfInterest;
                if (ext == null || ext.IsEmpty)
                    throw new InvalidOperationException("La capa no tiene extent utilizable: " + capa);
                ext = ProyectarAlMapa(ext, map);
            }
            else
            {
                throw new ArgumentException("Indica 'coords' [xmin,ymin,xmax,ymax] o 'capa'.");
            }

            IActiveView mapView = (IActiveView)map;
            mapView.Extent = ext;
            doc.ActiveView.Refresh();

            IEnvelope e = mapView.Extent;
            return Protocol.Result(new JObject
            {
                ["extent"] = new JObject
                {
                    ["xmin"] = e.XMin,
                    ["ymin"] = e.YMin,
                    ["xmax"] = e.XMax,
                    ["ymax"] = e.YMax
                },
                ["escala"] = map.MapScale
            });
        }

        /// <summary>Extent de la selección de la capa (unión de los envelopes de las
        /// features seleccionadas), o null si no hay selección — equivalente al
        /// lyr.getSelectedExtent(False) de arcpy.mapping.
        ///
        /// El envelope acumulado sale SIN sistema de coordenadas si no se le pone:
        /// las geometrías vienen en el CRS de la fuente, así que con una capa en
        /// geográficas y el data frame en UTM el encuadre caía en el origen. Se le
        /// asigna el CRS de la fuente aquí y quien lo use lo proyecta al del mapa.</summary>
        private static IEnvelope ExtentSeleccion(ILayer lyr)
        {
            ICursor cursor = null;
            try
            {
                IFeatureSelection fsel = lyr as IFeatureSelection;
                if (fsel == null || fsel.SelectionSet == null || fsel.SelectionSet.Count == 0)
                    return null;
                fsel.SelectionSet.Search(null, true, out cursor);
                IFeatureCursor featureCursor = (IFeatureCursor)cursor;
                IEnvelope acumulado = new EnvelopeClass();
                acumulado.SetEmpty();
                // El CRS se lee de la FEATURE CLASS, no de la geometría de una fila: el
                // cursor es reciclado, y guardarse algo que cuelga de una fila reciclada
                // es justo lo que el resto del add-in evita.
                ISpatialReference srFuente = null;
                try
                {
                    IFeatureLayer fl = lyr as IFeatureLayer;
                    IGeoDataset gds = fl != null ? fl.FeatureClass as IGeoDataset : null;
                    if (gds != null)
                        srFuente = gds.SpatialReference;
                }
                catch { /* sin CRS legible: se deja sin asignar, como antes */ }
                IFeature feature;
                while ((feature = featureCursor.NextFeature()) != null)
                {
                    if (feature.Shape == null || feature.Shape.IsEmpty)
                        continue;
                    acumulado.Union(feature.Shape.Envelope);
                }
                if (acumulado.IsEmpty)
                    return null;
                if (srFuente != null)
                    acumulado.SpatialReference = srFuente;
                return acumulado;
            }
            catch
            {
                return null; // sin selección utilizable: caer al extent total
            }
            finally
            {
                DataAccess.SoltarCom(cursor);
            }
        }

        /// <summary>
        /// Envelope al CRS del data frame. El setter de IActiveView.Extent NO
        /// reproyecta: si el envelope viene en el CRS de la fuente (geográficas) y
        /// el data frame está en UTM, el encuadre se va al origen. Si alguno de los
        /// dos CRS falta o la proyección falla se devuelve el envelope original, que
        /// es exactamente lo que hacía antes.
        /// </summary>
        internal static IEnvelope ProyectarAlMapa(IEnvelope ext, IMap map)
        {
            if (ext == null || ext.IsEmpty)
                return ext;
            try
            {
                ISpatialReference srMapa = map.SpatialReference;
                ISpatialReference srExt = ext.SpatialReference;
                if (srMapa == null || srExt == null
                    || srMapa is IUnknownCoordinateSystem || srExt is IUnknownCoordinateSystem)
                    return ext;
                if (srMapa.FactoryCode != 0 && srMapa.FactoryCode == srExt.FactoryCode)
                    return ext;
                IEnvelope copia = new EnvelopeClass();
                copia.PutCoords(ext.XMin, ext.YMin, ext.XMax, ext.YMax);
                copia.SpatialReference = srExt;
                copia.Project(srMapa);
                return copia.IsEmpty ? ext : copia;
            }
            catch
            {
                return ext;
            }
        }

        public static JObject SetLayerVisibility(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");
            bool visible = parameters["visible"] == null || parameters["visible"].Type == JTokenType.Null
                ? true : (bool)parameters["visible"];

            IMxDocument doc;
            IMap map = FocusMap(out doc);
            ILayer lyr = FindLayer(map, capa);
            lyr.Visible = visible;
            NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject { ["capa"] = capa, ["visible"] = visible });
        }

        public static JObject SetDefinitionQuery(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");
            string query = (string)parameters["query"];

            IMxDocument doc;
            IMap map = FocusMap(out doc);
            ILayer lyr = FindLayer(map, capa);
            IFeatureLayerDefinition def = lyr as IFeatureLayerDefinition;
            if (def == null)
                throw new ArgumentException("La capa no admite definition query: " + capa);

            string anterior = def.DefinitionExpression ?? "";
            def.DefinitionExpression = string.IsNullOrEmpty(query) ? "" : query;
            NotificarCambioContenido(map, doc);
            return Protocol.Result(new JObject
            {
                ["capa"] = capa,
                ["query_anterior"] = anterior,
                ["query_nueva"] = def.DefinitionExpression ?? ""
            });
        }
    }
}
