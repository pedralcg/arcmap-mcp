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
            /// <summary>Encendida ella y todos los grupos que la contienen: lo que se dibuja.</summary>
            public bool VisibleEfectiva;
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
                Recoger(lyr, "", salida, true);
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

        private static void Recoger(ILayer lyr, string prefijo, List<CapaEnMapa> destino, bool padresVisibles)
        {
            if (lyr == null)
                return;
            string nombre = NombreSeguro(lyr);
            string ruta = prefijo.Length == 0 ? nombre : prefijo + "/" + nombre;
            bool visible = false;
            try { visible = padresVisibles && lyr.Visible; } catch { }
            destino.Add(new CapaEnMapa { Capa = lyr, Nombre = nombre, Ruta = ruta, VisibleEfectiva = visible });

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
                Recoger(hijo, ruta, destino, visible);
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
        /// hay más de una candidata NO se elige por orden: se devuelve un error con
        /// las rutas completas para que quien llama decida. Única excepción: si UNA
        /// de las candidatas tiene exactamente esa ruta completa (la capa de la raíz,
        /// cuya ruta es su nombre), se elige esa aunque haya homónimas en grupos.
        /// </summary>
        internal static ILayer FindLayer(IMap map, string nombre)
        {
            return FindLayer(map, nombre, false);
        }

        /// <summary>
        /// `destructiva`: para las herramientas que borran o reapuntan (remove_layer,
        /// repair_data_source). Con homónimas, la regla de la ruta exacta solo vale si la
        /// ruta viene ANCLADA a la raíz con «/» delante («/Límite», «/Grupo/Límite»): un
        /// borrado nunca adivina. Sin ancla, «Límite» con «Límite» en la raíz y «G/Límite»
        /// en un grupo es error, aunque para mirar o encender se elija la de la raíz
        /// (decisión de Pedro, 2026-10-10, opción 2 de la revisión de la 2.17.0).
        /// </summary>
        internal static ILayer FindLayer(IMap map, string nombre, bool destructiva)
        {
            List<CapaEnMapa> capas = CapasConRuta(map);
            List<CapaEnMapa> candidatas = Coincidencias(capas, nombre);
            if (candidatas.Count == 1)
                return candidatas[0].Capa;
            // La ruta COMPLETA manda sobre el nombre o la cola de ruta. Sin esto, una
            // capa de la raíz con homónimas dentro de grupos era inalcanzable: su ruta
            // es su nombre pelado, el error de abajo la ofrecía como ruta válida, y al
            // repetirla volvía a casar con todas por nombre (expediente real, 2026-10-01).
            CapaEnMapa exacta = !destructiva || Anclada(nombre) ? RutaExacta(candidatas, nombre) : null;
            if (exacta != null)
                return exacta.Capa;
            if (candidatas.Count == 0)
                throw new ArgumentException("Capa no encontrada: " + nombre
                    + ". Disponibles: " + string.Join(", ", Rutas(capas)));
            if (destructiva)
                throw new ArgumentException("Hay " + candidatas.Count + " capas que casan con '" + nombre
                    + "', y esta herramienta borra o reapunta: no elige por ti. Repite con la ruta ANCLADA"
                    + " a la raíz, con «/» delante, tal cual: "
                    + string.Join(", ", Rutas(candidatas).ConvertAll(r => "/" + r)));
            throw new ArgumentException("Hay " + candidatas.Count + " capas que casan con '" + nombre
                + "'. Repite con UNA de estas rutas, tal cual (las que acaban en #n son capas"
                + " homónimas dentro del mismo grupo, numeradas en orden de TOC): "
                + string.Join(", ", Rutas(candidatas)));
        }

        /// <summary>La ruta viene con «/» (o «\») delante: anclada a la raíz de la TOC.</summary>
        internal static bool Anclada(string nombre)
        {
            bool _v;
            string _f;
            string s = QuitarPrefijos(nombre, out _v, out _f);
            return s.StartsWith("/") || s.StartsWith("\\");
        }

        /// <summary>Capas que casan con el texto: por ruta si trae separador
        /// ("Grupo/Capa", también admite una cola de la ruta), por nombre pelado si
        /// no. OrdinalIgnoreCase, como el resto del add-in.</summary>
        internal static List<CapaEnMapa> Coincidencias(List<CapaEnMapa> capas, string nombre)
        {
            // Prefijos (2026-10-10, auditoría de una serie de montes y de una serie real), válidos en cualquier
            // herramienta que pida una capa y en edit_mxd_lote:
            //   «visible:...»  solo entre las capas que se DIBUJAN (ella y sus grupos
            //                  encendidos): las homónimas de grupos ocultos ya no estorban.
            //   «fuente:texto» la capa cuya fuente (carpeta\dataset) contiene el texto,
            //                  sin importar cómo se llame en la TOC.
            bool soloVisibles;
            string fuente;
            nombre = QuitarPrefijos(nombre, out soloVisibles, out fuente);
            if (soloVisibles)
                capas = capas.FindAll(c => c.VisibleEfectiva);
            if (fuente != null)
                return capas.FindAll(c => !(c.Capa is IGroupLayer)
                    && (FuenteDe(c.Capa) ?? "").IndexOf(fuente, StringComparison.OrdinalIgnoreCase) >= 0);
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

        /// <summary>Quita los prefijos «visible:» y «fuente:» del selector de capa.</summary>
        internal static string QuitarPrefijos(string nombre, out bool soloVisibles, out string fuente)
        {
            soloVisibles = false;
            fuente = null;
            string s = (nombre ?? "").TrimStart();
            while (true)
            {
                if (s.StartsWith("visible:", StringComparison.OrdinalIgnoreCase))
                {
                    soloVisibles = true;
                    s = s.Substring("visible:".Length).TrimStart();
                }
                else if (s.StartsWith("fuente:", StringComparison.OrdinalIgnoreCase))
                {
                    fuente = s.Substring("fuente:".Length).Trim();
                    if (fuente.Length == 0)
                        throw new ArgumentException("'fuente:' necesita un texto: fuente:Rodales_V1.");
                    return "";
                }
                else
                    return s;
            }
        }

        /// <summary>Carpeta\dataset de la fuente de una capa, o null.</summary>
        internal static string FuenteDe(ILayer lyr)
        {
            try
            {
                IDataLayer dl = lyr as IDataLayer;
                IDatasetName dsn = dl != null ? dl.DataSourceName as IDatasetName : null;
                if (dsn == null)
                    return null;
                string ws = dsn.WorkspaceName != null ? (dsn.WorkspaceName.PathName ?? "").TrimEnd('\\', '/') : "";
                return ws + "\\" + dsn.Name;
            }
            catch { return null; }
        }

        /// <summary>Un nombre nuevo de capa que no empiece por «visible:» ni «fuente:»: QuitarPrefijos
        /// los leería como selector y la capa quedaría inalcanzable por nombre.</summary>
        internal static void ComprobarSinPrefijo(string nuevo)
        {
            string s = (nuevo ?? "").TrimStart();
            if (s.StartsWith("visible:", StringComparison.OrdinalIgnoreCase)
                || s.StartsWith("fuente:", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("'nombre_nuevo' no puede empezar por 'visible:' ni 'fuente:': las tools los"
                    + " leen como selector y la capa quedaría inalcanzable por nombre.");
        }

        /// <summary>La única candidata cuya ruta es EXACTAMENTE la pedida, o null si
        /// no hay ninguna o hay más de una (dos homónimas en el mismo grupo comparten
        /// ruta sin número: ahí sigue mandando el error con las rutas #n).</summary>
        internal static CapaEnMapa RutaExacta(List<CapaEnMapa> candidatas, string nombre)
        {
            bool _v;
            string _f;
            string buscado = QuitarPrefijos(nombre, out _v, out _f).Replace('\\', '/').Trim('/');
            CapaEnMapa unica = null;
            foreach (CapaEnMapa c in candidatas)
            {
                if (!string.Equals(c.Ruta, buscado, StringComparison.OrdinalIgnoreCase))
                    continue;
                if (unica != null)
                    return null;
                unica = c;
            }
            return unica;
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

        /// <summary>Como la anterior, pero vale también para un .mxd en disco
        /// (ADR-010). Es la que usan los handlers del modo disco.</summary>
        internal static IMap FocusMap(out Documento doc)
        {
            doc = Documento.Actual();
            IMap map = doc.FocusMap;
            if (map == null)
                throw new InvalidOperationException("No hay data frame activo.");
            return map;
        }

        /// <summary>
        /// Extensión y CRS del data frame ANTES de tocar sus capas. ArcMap reencuadra al quitar
        /// o añadir capas: con remove_layer + add_layer la extensión se ampliaba unas 2,25 veces,
        /// igual en vista de datos que de composición, y un plano ya encuadrado se exportaba
        /// recortado (issue #8, 2026-10-05, con control de variables: la simbología no era).
        /// Si la vista se movió, se devuelve a donde estaba y la respuesta lo dice
        /// (`extension_restaurada`). Si lo que cambió es el CRS del data frame (se quitó la capa
        /// que lo fijaba) no se restaura a ciegas: se avisa con `crs_cambiado`.
        /// En disco no hay vista que mover; ahí la extensión se fija con set_extent en el lote.
        /// </summary>
        internal sealed class FotoVista
        {
            private readonly double xmin, ymin, xmax, ymax;
            private readonly string crs;

            private FotoVista(IEnvelope e, string crs)
            {
                xmin = e.XMin; ymin = e.YMin; xmax = e.XMax; ymax = e.YMax;
                this.crs = crs;
            }

            internal static FotoVista Tomar(IMap map, Documento doc)
            {
                if (doc == null || !doc.EnVivo)
                    return null;
                IEnvelope e = ((IActiveView)map).Extent;
                if (e == null || e.IsEmpty)
                    return null;
                return new FotoVista(e, CrsDe(map));
            }

            internal static void Restaurar(FotoVista foto, IMap map, Documento doc, JObject respuesta)
            {
                if (foto == null)
                    return;
                IActiveView vista = (IActiveView)map;
                IEnvelope ahora = vista.Extent;
                if (ahora != null && !ahora.IsEmpty && Igual(foto.xmin, ahora.XMin) && Igual(foto.ymin, ahora.YMin)
                    && Igual(foto.xmax, ahora.XMax) && Igual(foto.ymax, ahora.YMax))
                    return;
                string crsAhora = CrsDe(map);
                if (!string.Equals(foto.crs, crsAhora, StringComparison.Ordinal))
                {
                    respuesta["extension_restaurada"] = false;
                    respuesta["crs_cambiado"] = new JObject { ["antes"] = foto.crs, ["ahora"] = crsAhora };
                    return;
                }
                IEnvelope e = new EnvelopeClass();
                e.PutCoords(foto.xmin, foto.ymin, foto.xmax, foto.ymax);
                e.SpatialReference = map.SpatialReference;
                vista.Extent = e;
                doc.Refrescar();
                respuesta["extension_restaurada"] = true;
            }

            private static string CrsDe(IMap map)
            {
                ISpatialReference sr = map.SpatialReference;
                return sr == null ? null : sr.Name;
            }

            private static bool Igual(double a, double b)
            {
                return Math.Abs(a - b) <= 1e-6 * Math.Max(1.0, Math.Abs(a));
            }
        }

        /// <summary>Tras mutar el CONTENIDO del mapa (visibilidad, def. query, alta/
        /// baja de capa, simbología) hay que llamar a IActiveView.ContentsChanged()
        /// del MAPA: es el aviso que escuchan los map surrounds. Sin él, una leyenda
        /// con "only display checked layers" se queda con el estado viejo en los
        /// exports (el clic manual en la TOC dispara el evento; el setter
        /// programático, no). En disco el aviso también hace falta: lo que se guarda
        /// es la leyenda ya reconstruida.</summary>
        internal static void NotificarCambioContenido(IMap map, Documento doc)
        {
            ((IActiveView)map).ContentsChanged();
            doc.UpdateContents();
            doc.Refrescar();
        }

        internal static void NotificarCambioContenido(IMap map, IMxDocument doc)
        {
            NotificarCambioContenido(map, Documento.Vivo(doc));
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

            Documento doc;
            IMap map = FocusMap(out doc);
            // En disco, igual que set_extent: sin activar la pantalla la escala responde la pedida
            // y al guardar se queda la de antes (edit_mxd_lote daba ok y guardado, 2026-10-28).
            if (!doc.EnVivo)
                doc.PantallaLayout();
            map.MapScale = escala;
            doc.Refrescar();
            return Protocol.Result(new JObject { ["escala"] = map.MapScale });
        }

        public static JObject SetExtent(JObject parameters)
        {
            JArray coords = parameters["coords"] as JArray;
            string capa = (string)parameters["capa"];

            // Costura Documento: vale en vivo y en un .mxd en disco (edit_mxd_lote).
            Documento doc;
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

            // En disco la vista no tiene pantalla y fijar la extensión da E_PENDING
            // (0x8000000A): se activa el layout contra el escritorio, como para medir.
            if (!doc.EnVivo)
                doc.PantallaLayout();
            IActiveView mapView = (IActiveView)map;
            mapView.Extent = ext;
            doc.Refrescar();

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

            Documento doc;
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
            bool anadir = Parametros.LeerBool(parameters["anadir"], "anadir", false);
            if (anadir && string.IsNullOrWhiteSpace(query))
                throw new ArgumentException("Con 'anadir' hace falta 'query': la condición que se añade con AND.");

            Documento doc;
            IMap map = FocusMap(out doc);
            ILayer lyr = FindLayer(map, capa);
            IFeatureLayerDefinition def = lyr as IFeatureLayerDefinition;
            if (def == null)
                throw new ArgumentException("La capa no admite definition query: " + capa);

            string anterior = def.DefinitionExpression ?? "";
            bool yaEstaba = false;
            if (anadir)
            {
                // filtro28.py de una serie de montes: «(q) AND "MUP" = '...'», o solo la condición sin query.
                yaEstaba = YaIncluida(anterior, query);
                if (!yaEstaba)
                    def.DefinitionExpression = anterior.Trim().Length == 0
                        ? query.Trim() : "(" + anterior.Trim() + ") AND (" + query.Trim() + ")";
            }
            else
                def.DefinitionExpression = string.IsNullOrEmpty(query) ? "" : query;
            NotificarCambioContenido(map, doc);
            var r = new JObject
            {
                ["capa"] = capa,
                ["query_anterior"] = anterior,
                ["query_nueva"] = def.DefinitionExpression ?? ""
            };
            if (anadir)
                r["ya_estaba"] = yaEstaba;
            return Protocol.Result(r);
        }

        /// <summary>¿La condición ya es la query o uno de sus términos unidos con AND, a cualquier
        /// profundidad de paréntesis? Sin distinguir espacios de más. Así relanzar una receta del
        /// lote con anadir no encadena «((q) AND (x)) AND (x)».</summary>
        internal static bool YaIncluida(string anterior, string condicion)
        {
            string buscada = Normalizar(condicion);
            if (buscada.Length == 0)
                return false;
            var pendientes = new Stack<string>();
            pendientes.Push(Normalizar(anterior));
            while (pendientes.Count > 0)
            {
                string actual = pendientes.Pop();
                if (actual == buscada)
                    return true;
                List<string> trozos = PartirAnd(actual);
                if (trozos.Count > 1)
                    foreach (string t in trozos)
                        pendientes.Push(t.Trim());
                else if (actual.Length > 1 && actual[0] == '(' && CierraAlFinal(actual))
                    pendientes.Push(actual.Substring(1, actual.Length - 2).Trim());
            }
            return false;
        }

        private static string Normalizar(string q)
        {
            return System.Text.RegularExpressions.Regex.Replace((q ?? "").Trim(), @"\s+", " ");
        }

        /// <summary>Parte por « AND » de nivel superior: fuera de paréntesis y de comillas.</summary>
        private static List<string> PartirAnd(string q)
        {
            var trozos = new List<string>();
            int nivel = 0, inicio = 0;
            char comilla = '\0';
            for (int i = 0; i < q.Length; i++)
            {
                char c = q[i];
                if (comilla != '\0') { if (c == comilla) comilla = '\0'; continue; }
                if (c == '\'' || c == '"') comilla = c;
                else if (c == '(') nivel++;
                else if (c == ')') nivel--;
                else if (nivel == 0 && string.Compare(q, i, " AND ", 0, 5, StringComparison.OrdinalIgnoreCase) == 0
                         && i + 5 <= q.Length)
                {
                    trozos.Add(q.Substring(inicio, i - inicio));
                    inicio = i + 5;
                    i += 4;
                }
            }
            trozos.Add(q.Substring(inicio));
            return trozos;
        }

        /// <summary>¿El paréntesis que abre en la posición 0 cierra justo en la última?</summary>
        private static bool CierraAlFinal(string q)
        {
            int nivel = 0;
            char comilla = '\0';
            for (int i = 0; i < q.Length; i++)
            {
                char c = q[i];
                if (comilla != '\0') { if (c == comilla) comilla = '\0'; continue; }
                if (c == '\'' || c == '"') comilla = c;
                else if (c == '(') nivel++;
                else if (c == ')' && --nivel == 0 && i < q.Length - 1) return false;
            }
            return nivel == 0;
        }
    }
}
