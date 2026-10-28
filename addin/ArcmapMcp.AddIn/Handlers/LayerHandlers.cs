using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.Geodatabase;
using ESRI.ArcGIS.Geometry;
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
                throw new ArgumentException("Indica 'fuente' (ruta a .shp, feature class de .gdb, ráster o .lyr).");
            string posicion = ((string)parameters["posicion"] ?? "TOP").ToUpperInvariant();
            if (posicion != "TOP" && posicion != "BOTTOM" && posicion != "AUTO_ARRANGE")
                throw new ArgumentException("'posicion' debe ser TOP, BOTTOM o AUTO_ARRANGE.");
            string grupo = (string)parameters["grupo"];
            string nombre = (string)parameters["nombre"];
            bool enLeyenda = Parametros.LeerBool(parameters["en_leyenda"], "en_leyenda", true);
            bool? visible = Parametros.Dado(parameters["visible"])
                ? Parametros.LeerBool(parameters["visible"], "visible", true) : (bool?)null;
            bool esServicio = Servicios.EsUrl(fuente.Trim());
            List<string> subcapas = LeerSubcapas(parameters["subcapas"]);
            if (subcapas != null && !esServicio)
                throw new ArgumentException("'subcapas' es para servicios WMS (fuente = URL http/https).");
            // si_existe: relanzar una receta del lote duplicaba la capa. corregir_arcpy.py de una serie de montes
            // ya miraba antes por NOMBRE ("ya existe, no se duplica"), y por nombre se mira aquí:
            // es lo que sale en la TOC y en la leyenda.
            string siExiste = ((string)parameters["si_existe"] ?? "anadir").ToLowerInvariant();
            if (siExiste != "anadir" && siExiste != "omitir" && siExiste != "error")
                throw new ArgumentException("'si_existe' es anadir (por defecto), omitir o error.");

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            // Con el nombre dado se mira ANTES de crear la capa (un WMS va a la red al crearse).
            if (siExiste != "anadir" && !string.IsNullOrEmpty(nombre))
            {
                JObject ya = YaExiste(map, nombre, fuente, siExiste);
                if (ya != null)
                    return Protocol.Result(ya);
            }
            JObject servicio = null;
            ILayer nueva = esServicio ? Servicios.CrearWms(fuente.Trim(), subcapas, out servicio) : CrearCapa(fuente);

            // El nombre de la TOC es el que sale en la leyenda del plano, así que
            // llega hasta aquí: sin esto la capa se queda con el nombre del
            // fichero ("Vis_plataforma_oeste_sin_pantalla.tif") y hay que
            // renombrarla a mano o rodear el puente por MakeRasterLayer.
            if (!string.IsNullOrEmpty(nombre))
                nueva.Name = nombre;
            else if (siExiste != "anadir")
            {
                JObject ya = YaExiste(map, nueva.Name, fuente, siExiste);
                if (ya != null)
                    return Protocol.Result(ya);
            }
            // Antes de insertar: un WMS que entra apagado no llega a pedir teselas.
            if (visible != null)
                nueva.Visible = visible.Value;

            MapHandlers.FotoVista vista = MapHandlers.FotoVista.Tomar(map, doc);
            int leyendasAparte = Insertar(map, doc, nueva, grupo, posicion, enLeyenda);
            ICompositeLayer comp = nueva as ICompositeLayer;
            var r = new JObject
            {
                ["capa"] = nueva.Name,
                ["fuente"] = fuente,
                ["posicion"] = posicion,
                ["grupo"] = grupo,
                ["visible"] = nueva.Visible,
                ["es_grupo"] = comp != null && !esServicio,
                ["capas_dentro"] = comp != null && !esServicio ? (JToken)comp.Count : null,
                ["en_leyenda"] = enLeyenda,
                ["leyendas_sin_tocar"] = enLeyenda ? null : (JToken)leyendasAparte
            };
            if (servicio != null)
                r["servicio"] = servicio;
            MapHandlers.FotoVista.Restaurar(vista, map, doc, r);
            return Protocol.Result(r);
        }

        /// <summary>add_layer con si_existe: la respuesta si ya hay en el data frame una capa con
        /// ese nombre (a cualquier profundidad de grupo), o null si no la hay. Con "error", lanza.</summary>
        private static JObject YaExiste(IMap map, string nombre, string fuente, string siExiste)
        {
            List<MapHandlers.CapaEnMapa> ya = MapHandlers.CapasConRuta(map)
                .Where(c => string.Equals(c.Nombre, nombre, StringComparison.OrdinalIgnoreCase)).ToList();
            if (ya.Count == 0)
                return null;
            if (siExiste == "error")
                throw new ArgumentException("Ya hay " + (ya.Count == 1 ? "una capa" : ya.Count + " capas")
                    + " llamada '" + nombre + "' (" + string.Join(", ", ya.Select(c => c.Ruta)) + "): no se añade.");
            var existentes = new JArray();
            foreach (MapHandlers.CapaEnMapa c in ya)
                existentes.Add(new JObject { ["ruta"] = c.Ruta, ["fuente"] = MapHandlers.FuenteDe(c.Capa) });
            return new JObject
            {
                ["capa"] = nombre,
                ["fuente"] = fuente,
                ["omitida"] = true,
                ["motivo"] = "ya hay una capa con ese nombre; no se duplica (si_existe='omitir')",
                ["existentes"] = existentes
            };
        }

        /// <summary>
        /// rename_layer — cambia el nombre de una capa de la sesión viva (el que sale en
        /// la TOC y en la leyenda) y refresca las leyendas.
        ///
        /// Faltó el 2026-10-01 para corregir tildes de leyenda en planos con marcos OLE,
        /// que no se pueden guardar con arcpy. El rodeo (sacar la capa a .lyr y
        /// sustituirla) perdió el «colocar en columna nueva» de su entrada de leyenda.
        /// Renombrar no toca la entrada: conserva sus interruptores y su posición.
        /// </summary>
        public static JObject RenameLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string nuevo = (string)parameters["nombre_nuevo"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC, o ruta 'Grupo/Capa').");
            if (string.IsNullOrEmpty(nuevo) || nuevo.Trim().Length == 0)
                throw new ArgumentException("Indica 'nombre_nuevo': el texto que debe mostrar la TOC y la leyenda.");
            // '/' y '#' son los separadores de ruta del add-in ("Grupo/Capa", "Capa#2"), y
            // '\' se convierte en '/' al buscar (MapHandlers.Coincidencias): una capa con uno
            // de ellos en el nombre dejaría de poder buscarse por nombre y por ruta.
            if (nuevo.IndexOfAny(new[] { '/', '\\', '#' }) >= 0)
                throw new ArgumentException("'nombre_nuevo' no puede llevar '/', '\\' ni '#': son los separadores con"
                    + " los que las tools buscan capas por ruta, y la capa quedaría inalcanzable por nombre.");
            MapHandlers.ComprobarSinPrefijo(nuevo);

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
            string anterior = lyr.Name;
            lyr.Name = nuevo;

            MapHandlers.NotificarCambioContenido(map, doc);
            int leyendas = LayoutHandlers.RefrescarLeyendas(doc, map);

            var r = new JObject
            {
                ["nombre_anterior"] = anterior,
                ["nombre_nuevo"] = lyr.Name,
                ["leyendas_refrescadas"] = leyendas
            };
            int homonimos = MapHandlers.Coincidencias(MapHandlers.CapasConRuta(map), nuevo).Count;
            if (homonimos > 1)
                r["aviso"] = "Ahora hay " + homonimos + " capas llamadas '" + nuevo + "': para referirte a"
                    + " esta usa su ruta (list_layers la muestra).";
            return Protocol.Result(r);
        }

        /// <summary>
        /// duplicate_layer — la misma fuente dibujada dos veces (una con relleno y otra solo con
        /// el rótulo, u otra definition query) sin volver a añadirla y rehacer su simbología. En
        /// una serie de montes se hizo renombrando la capa e insertándola otra vez desde arcpy (Cowork,
        /// 2026-10-06). La copia es un clon PROFUNDO (IObjectCopy): simbología, etiquetas,
        /// definition query, transparencia y escalas son suyas, y tocarlas no toca la original.
        /// </summary>
        public static JObject DuplicateLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string nuevo = (string)parameters["nombre_nuevo"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC, o ruta 'Grupo/Capa').");
            if (string.IsNullOrEmpty(nuevo) || nuevo.Trim().Length == 0)
                throw new ArgumentException("Indica 'nombre_nuevo': la copia no puede llamarse igual que la original.");
            if (nuevo.IndexOfAny(new[] { '/', '\\', '#' }) >= 0)
                throw new ArgumentException("'nombre_nuevo' no puede llevar '/', '\\' ni '#': son los separadores de ruta.");
            MapHandlers.ComprobarSinPrefijo(nuevo);
            string referencia = (string)parameters["referencia"];
            string posicion = ((string)parameters["posicion"] ?? "AFTER").ToUpperInvariant();
            if (posicion != "BEFORE" && posicion != "AFTER")
                throw new ArgumentException("'posicion' es BEFORE (encima de la referencia) o AFTER (debajo).");
            bool? etiquetas = Parametros.Dado(parameters["etiquetas"])
                ? Parametros.LeerBool(parameters["etiquetas"], "etiquetas", true) : (bool?)null;
            bool enLeyenda = Parametros.LeerBool(parameters["en_leyenda"], "en_leyenda", false);

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer original = MapHandlers.FindLayer(map, capa);
            // Dos capas homónimas ya dieron guerra (la de la raíz inalcanzable, 2026-10-01).
            if (MapHandlers.Coincidencias(MapHandlers.CapasConRuta(map), nuevo).Count > 0)
                throw new ArgumentException("Ya hay una capa llamada '" + nuevo + "': elige otro nombre.");
            ILayer refLyr = string.IsNullOrEmpty(referencia) ? original : MapHandlers.FindLayer(map, referencia);

            ILayer copia = (ILayer)new ObjectCopyClass().Copy(original);
            copia.Name = nuevo;
            IGeoFeatureLayer gfCopia = copia as IGeoFeatureLayer;
            if (etiquetas != null)
            {
                if (gfCopia == null)
                    throw new ArgumentException("'etiquetas' es para capas de entidades.");
                gfCopia.DisplayAnnotation = etiquetas.Value;
            }

            IGroupLayer padre = BuscarGrupoPadre(map, refLyr);
            int k = TocHandlers.Hijos(map, padre).FindIndex(l => ReferenceEquals(l, refLyr));
            int indice = posicion == "BEFORE" ? k : k + 1;
            int leyendasAparte = InsertarEn(map, doc, copia, padre, indice, enLeyenda);

            var orden = new JArray();
            foreach (ILayer l in TocHandlers.Hijos(map, padre))
                orden.Add(l.Name);
            var r = new JObject
            {
                ["capa_original"] = original.Name,
                ["capa_nueva"] = copia.Name,
                ["grupo"] = padre == null ? null : ((ILayer)padre).Name,
                ["indice"] = TocHandlers.Hijos(map, padre).FindIndex(l => ReferenceEquals(l, copia)),
                ["orden"] = orden,
                ["en_leyenda"] = enLeyenda,
                ["leyendas_sin_tocar"] = enLeyenda ? null : (JToken)leyendasAparte
            };
            IGeoFeatureLayer gfOriginal = original as IGeoFeatureLayer;
            if (gfCopia != null && gfOriginal != null)
            {
                r["etiquetas"] = gfCopia.DisplayAnnotation;
                // La prueba de que es un clon y no la misma simbología compartida.
                r["simbologia_propia"] = !ReferenceEquals(gfCopia.Renderer, gfOriginal.Renderer);
                IFeatureLayerDefinition dq = copia as IFeatureLayerDefinition;
                if (dq != null)
                    r["definition_query"] = dq.DefinitionExpression;
            }
            return Protocol.Result(r);
        }

        /// <summary>Inserta en `grupo` (null = raíz) en la posición `indice`, con el AutoAdd de
        /// las leyendas apagado si `enLeyenda` es false (ver Insertar). Devuelve cuántas
        /// leyendas se dejaron sin tocar.</summary>
        private static int InsertarEn(IMap map, Documento doc, ILayer nueva, IGroupLayer grupo, int indice,
                                      bool enLeyenda)
        {
            var leyendas = enLeyenda ? new List<ILegend>() : LayoutHandlers.LeyendasDelMapa(doc, map);
            var autoAdd = new List<bool>();
            foreach (ILegend lg in leyendas)
            {
                autoAdd.Add(lg.AutoAdd);
                lg.AutoAdd = false;
            }
            try
            {
                IMapLayers ml = (IMapLayers)map;
                if (grupo != null)
                    ml.InsertLayerInGroup(grupo, nueva, false, indice);
                else
                    ml.InsertLayer(nueva, false, indice);
                MapHandlers.NotificarCambioContenido(map, doc);
            }
            finally
            {
                for (int i = 0; i < leyendas.Count; i++)
                    leyendas[i].AutoAdd = autoAdd[i];
            }
            return leyendas.Count;
        }

        private static List<string> LeerSubcapas(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            JArray a = t as JArray;
            if (a == null || a.Count == 0)
                throw new ArgumentException("'subcapas' debe ser una lista de nombres (o rutas 'Grupo/Subcapa') del"
                    + " servicio. Para encenderlas todas, no la pases.");
            var l = new List<string>();
            foreach (JToken x in a)
            {
                if (x.Type != JTokenType.String || string.IsNullOrEmpty(((string)x).Trim()))
                    throw new ArgumentException("'subcapas' solo admite textos. Recibido: " + x.ToString());
                l.Add(((string)x).Trim());
            }
            return l;
        }

        /// <summary>
        /// add_group — crea una capa de grupo vacía. arcpy no sabe (en el bloque 03 de
        /// una serie de montes hubo que guardar un grupo vacío existente como .lyr y usarlo de
        /// plantilla); en ArcObjects es un GroupLayer nuevo.
        /// </summary>
        public static JObject AddGroup(JObject parameters)
        {
            string nombre = (string)parameters["nombre"];
            if (string.IsNullOrEmpty(nombre))
                throw new ArgumentException("Indica 'nombre' para el grupo nuevo.");
            string posicion = ((string)parameters["posicion"] ?? "TOP").ToUpperInvariant();
            if (posicion != "TOP" && posicion != "BOTTOM")
                throw new ArgumentException("'posicion' debe ser TOP o BOTTOM.");
            string padre = (string)parameters["grupo"];
            bool visible = Parametros.LeerBool(parameters["visible"], "visible", true);
            bool enLeyenda = Parametros.LeerBool(parameters["en_leyenda"], "en_leyenda", true);

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);

            // Un homónimo no impide crearlo, pero a partir de ahí 'grupo' y el resto de
            // tools pedirán la ruta: se avisa para que no sea una sorpresa.
            int homonimos = MapHandlers.Coincidencias(MapHandlers.CapasConRuta(map), nombre).Count;

            ILayer grp = new GroupLayerClass();
            grp.Name = nombre;
            grp.Visible = visible;
            int leyendasAparte = Insertar(map, doc, grp, padre, posicion, enLeyenda);

            var r = new JObject
            {
                ["grupo"] = nombre,
                ["dentro_de"] = padre,
                ["posicion"] = posicion,
                ["visible"] = visible,
                ["en_leyenda"] = enLeyenda,
                ["leyendas_sin_tocar"] = enLeyenda ? null : (JToken)leyendasAparte
            };
            if (homonimos > 0)
                r["aviso"] = "Ya había " + homonimos + " capa(s) llamada(s) '" + nombre
                    + "': para referirte a este grupo usa su ruta (list_layers la muestra).";
            return Protocol.Result(r);
        }

        /// <summary>
        /// Inserta la capa en el mapa o en un grupo. Con `enLeyenda` = false apaga el
        /// AutoAdd de las leyendas de ESE mapa mientras entra y lo deja como estaba:
        /// con AutoAdd (lo normal) cada capa añadida entra también en la leyenda del
        /// plano, y en uno que ya va justo la desborda. Devuelve cuántas leyendas se
        /// han dejado sin tocar.
        /// </summary>
        private static int Insertar(IMap map, Documento doc, ILayer nueva, string grupo,
                                    string posicion, bool enLeyenda)
        {
            IMapLayers mapLayers = (IMapLayers)map;
            IGroupLayer grp = null;
            if (!string.IsNullOrEmpty(grupo))
            {
                grp = MapHandlers.FindLayer(map, grupo) as IGroupLayer;
                if (grp == null)
                    throw new ArgumentException("La capa indicada en 'grupo' no es una capa de grupo: " + grupo);
            }

            var leyendas = enLeyenda ? new System.Collections.Generic.List<ILegend>()
                                     : LayoutHandlers.LeyendasDelMapa(doc, map);
            var autoAdd = new System.Collections.Generic.List<bool>();
            foreach (ILegend lg in leyendas)
            {
                autoAdd.Add(lg.AutoAdd);
                lg.AutoAdd = false;
            }
            try
            {
                if (grp != null)
                {
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
                // Dentro del try: la leyenda reacciona al aviso de cambio de contenido,
                // y tiene que llegarle con el AutoAdd aún apagado.
                MapHandlers.NotificarCambioContenido(map, doc);
            }
            finally
            {
                for (int i = 0; i < leyendas.Count; i++)
                    leyendas[i].AutoAdd = autoAdd[i];
            }
            return leyendas.Count;
        }

        /// <summary>
        /// Crea la capa desde la ruta. Un `.lyr` se abre como layer file: trae
        /// dentro el nombre, la simbología, la transparencia y —si es de grupo—
        /// el árbol entero, que es lo único que permite reproducir de una vez la
        /// estructura de grupos de un proyecto de QGIS. El resto de fuentes van
        /// por la factory de siempre.
        ///
        /// `lf.Close()` cierra el fichero pero NO invalida la capa ya obtenida:
        /// es el mismo patrón que usa el Add Data de ArcMap, y cerrarlo evita
        /// dejar el `.lyr` bloqueado en disco mientras dure la sesión.
        /// </summary>
        private static ILayer CrearCapa(string fuente)
        {
            if (!".lyr".Equals(System.IO.Path.GetExtension(fuente), StringComparison.OrdinalIgnoreCase))
                return DataAccess.CrearCapaDesdeRuta(fuente);

            if (!File.Exists(fuente))
                throw new ArgumentException("No existe el archivo .lyr: " + fuente);
            ILayerFile lf = new LayerFileClass();
            ILayer capa;
            try
            {
                lf.Open(fuente);
                capa = lf.Layer;
            }
            finally
            {
                // En finally, no tras el Open: si Open lanzaba (fichero de otra
                // versión, corrupto), el .lyr se quedaba bloqueado en disco el resto
                // de la sesión de ArcMap.
                try { lf.Close(); } catch { }
            }
            if (capa == null)
                throw new ArgumentException("El .lyr no contiene ninguna capa: " + fuente);
            return capa;
        }

        /// <summary>
        /// save_layer_as_lyr — guarda una capa del documento (o un grupo, con su árbol) como .lyr:
        /// el «Guardar como archivo de capa» de ArcMap y el lyr.saveACopy de arcpy. Era la base de
        /// la réplica de una serie a 10 montes (plantilla_lyr_b3.py, ~28 .lyr) y de los .lyr de
        /// otra serie (lyr_zonif.py). Se escribe a un temporal y se reabre ANTES de dejarlo en su
        /// sitio: lo que se dice guardado es lo que se lee del fichero, no lo que había en memoria.
        /// </summary>
        public static JObject SaveLayerAsLyr(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string ruta = (string)parameters["ruta"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC, o ruta 'Grupo/Capa').");
            if (string.IsNullOrEmpty(ruta) || !System.IO.Path.IsPathRooted(ruta))
                throw new ArgumentException("Indica 'ruta': la ruta ABSOLUTA del .lyr que se va a crear.");
            if (!".lyr".Equals(System.IO.Path.GetExtension(ruta), StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("'ruta' tiene que acabar en .lyr: " + ruta);
            bool sobrescribir = Parametros.LeerBool(parameters["sobrescribir"], "sobrescribir", false);
            string carpeta = System.IO.Path.GetDirectoryName(ruta);
            if (!Directory.Exists(carpeta))
                throw new ArgumentException("No existe la carpeta de destino: " + carpeta);
            if (File.Exists(ruta) && !sobrescribir)
                throw new ArgumentException("Ya existe " + ruta + ". Pasa sobrescribir=True para pisarlo.");

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
            JObject esperado = ResumenCapa(lyr);

            string temporal = System.IO.Path.Combine(carpeta, "~" + Guid.NewGuid().ToString("N").Substring(0, 8) + ".lyr");
            try
            {
                ILayerFile lf = new LayerFileClass();
                try
                {
                    lf.New(temporal);
                    lf.ReplaceContents(lyr);
                    lf.Save();
                }
                finally
                {
                    try { lf.Close(); } catch { }
                }
                JObject leido;
                ILayerFile rl = new LayerFileClass();
                try
                {
                    rl.Open(temporal);
                    if (rl.Layer == null)
                        throw new InvalidOperationException("El .lyr escrito no contiene ninguna capa al reabrirlo.");
                    leido = ResumenCapa(rl.Layer);
                }
                finally
                {
                    try { rl.Close(); } catch { }
                }
                var diferencias = new JArray();
                foreach (var p in esperado.Properties())
                    if (!JToken.DeepEquals(p.Value, leido[p.Name]))
                        diferencias.Add(new JObject { ["que"] = p.Name, ["capa"] = p.Value, ["lyr"] = leido[p.Name] });
                if (diferencias.Count > 0)
                    throw new InvalidOperationException("El .lyr reabierto no coincide con la capa ("
                        + diferencias.ToString(Newtonsoft.Json.Formatting.None) + "); no se deja en " + ruta + ".");
                if (File.Exists(ruta))
                    File.Delete(ruta);
                File.Move(temporal, ruta);
                return Protocol.Result(new JObject
                {
                    ["capa"] = lyr.Name,
                    ["ruta"] = ruta,
                    ["bytes"] = new FileInfo(ruta).Length,
                    ["verificado"] = true,
                    ["leido"] = leido
                });
            }
            finally
            {
                try { if (File.Exists(temporal)) File.Delete(temporal); } catch { }
            }
        }

        /// <summary>Lo que se compara entre la capa y el .lyr reabierto: nombre, tipo, fuente,
        /// renderer, etiquetas, definition query, transparencia y, en un grupo, sus hijas.</summary>
        private static JObject ResumenCapa(ILayer lyr)
        {
            var r = new JObject
            {
                ["nombre"] = lyr.Name,
                ["tipo"] = TipoCapa(lyr),
                ["visible"] = lyr.Visible
            };
            string fuente = MapHandlers.FuenteDe(lyr);
            if (fuente != null)
                r["fuente"] = fuente;
            IGeoFeatureLayer gf = lyr as IGeoFeatureLayer;
            if (gf != null)
            {
                r["renderer"] = gf.Renderer == null ? null : TipoRenderer(gf.Renderer);
                r["etiquetas"] = gf.DisplayAnnotation;
            }
            IRasterLayer rl = lyr as IRasterLayer;
            if (rl != null && rl.Renderer != null)
                r["renderer"] = TipoRenderer(rl.Renderer);
            IFeatureLayerDefinition dq = lyr as IFeatureLayerDefinition;
            if (dq != null)
                r["definition_query"] = dq.DefinitionExpression ?? "";
            ILayerEffects ef = lyr as ILayerEffects;
            if (ef != null && ef.SupportsTransparency)
                r["transparencia"] = ef.Transparency;
            ICompositeLayer comp = lyr as ICompositeLayer;
            if (comp != null && lyr is IGroupLayer)
            {
                var hijas = new JArray();
                for (int i = 0; i < comp.Count; i++)
                    hijas.Add(ResumenCapa(comp.get_Layer(i)));
                r["hijas"] = hijas;
            }
            return r;
        }

        private static string TipoCapa(ILayer lyr)
        {
            if (lyr is IGroupLayer) return "grupo";
            if (lyr is IFeatureLayer) return "entidades";
            if (lyr is IRasterLayer) return "raster";
            return "otra";
        }

        /// <summary>El nombre de la clase COM del renderer («UniqueValueRenderer»...), sacado de su
        /// interfaz, que es lo que no cambia al guardar y reabrir.</summary>
        private static string TipoRenderer(object renderer)
        {
            if (renderer is IUniqueValueRenderer) return "valores_unicos";
            if (renderer is IClassBreaksRenderer) return "graduado";
            if (renderer is ISimpleRenderer) return "simple";
            if (renderer is IRasterClassifyColorRampRenderer) return "raster_clasificado";
            if (renderer is IRasterUniqueValueRenderer) return "raster_valores_unicos";
            if (renderer is IRasterStretchColorRampRenderer) return "raster_estirado";
            if (renderer is IRasterRGBRenderer) return "raster_rgb";
            return "otro";
        }

        public static JObject RemoveLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC).");

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            // Destructiva: con homónimas exige la ruta anclada («/Capa»), no adivina.
            ILayer lyr = MapHandlers.FindLayer(map, capa, true);

            // Si la capa vive dentro de un grupo hay que borrarla del grupo;
            // IMap.DeleteLayer solo garantiza las de primer nivel.
            IGroupLayer padre = BuscarGrupoPadre(map, lyr);
            MapHandlers.FotoVista vista = MapHandlers.FotoVista.Tomar(map, doc);
            if (padre != null)
                padre.Delete(lyr);
            else
                map.DeleteLayer(lyr);

            MapHandlers.NotificarCambioContenido(map, doc);
            var r = new JObject
            {
                ["capa"] = capa,
                ["eliminada"] = true
            };
            MapHandlers.FotoVista.Restaurar(vista, map, doc, r);
            return Protocol.Result(r);
        }

        public static JObject ApplySymbologyFromLayer(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string lyrFile = (string)parameters["lyr_file"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(lyrFile))
                throw new ArgumentException("Indica 'capa' y 'lyr_file' (ruta al .lyr de origen).");
            if (!File.Exists(lyrFile))
                throw new ArgumentException("No existe el archivo .lyr: " + lyrFile);

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer objetivo = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer destino = objetivo as IGeoFeatureLayer;
            IRasterLayer destinoRaster = objetivo as IRasterLayer;
            if (destino == null && destinoRaster == null)
                throw new ArgumentException("Solo capas de entidades o ráster admiten simbología desde .lyr: " + capa);

            JObject detalle;
            ILayerFile lf = new LayerFileClass();
            try
            {
                // El Open va DENTRO del try: si lanza, el finally cierra igualmente
                // y el .lyr no se queda bloqueado.
                lf.Open(lyrFile);
                detalle = destino != null
                    ? AplicarAEntidades(destino, lf.Layer, lyrFile)
                    : AplicarARaster(destinoRaster, lf.Layer, lyrFile);
            }
            finally
            {
                try { lf.Close(); } catch { }
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            detalle["capa"] = capa;
            detalle["lyr_origen"] = lyrFile;
            return Protocol.Result(detalle);
        }

        private static JObject AplicarAEntidades(IGeoFeatureLayer destino, ILayer raizLyr, string lyrFile)
        {
            IGeoFeatureLayer origen = PrimerGeoFeatureLayer(raizLyr);
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
            return new JObject { ["tipo"] = "entidades" };
        }

        /// <summary>
        /// Simbología ráster desde un .lyr. Es la única vía para un renderer que
        /// las tools no saben construir —valores únicos, colormap, RGB compuesto—:
        /// `set_raster_symbology` solo clasifica o estira, y sobre un ráster
        /// categórico (una máscara 0/1/2, una reclasificación, un `paletted` traído
        /// de QGIS) la clasificación falla porque no hay histograma que cortar.
        ///
        /// El renderer se clona y se REENCAJA en el ráster de destino: un
        /// IRasterRenderer lleva dentro el ráster sobre el que se construyó, y sin
        /// `Raster` + `Update()` el clon pintaría contra el dato del .lyr.
        ///
        /// La transparencia viaja también, y a propósito: en un ráster de
        /// visibilidad o de afección la opacidad no es decoración, es lo que deja
        /// ver la ortofoto debajo, y se pierde en cuanto la capa se recrea.
        /// </summary>
        private static JObject AplicarARaster(IRasterLayer destino, ILayer raizLyr, string lyrFile)
        {
            IRasterLayer origen = PrimerRasterLayer(raizLyr);
            if (origen == null)
                throw new ArgumentException("El .lyr no contiene una capa ráster: " + lyrFile);
            if (origen.Renderer == null)
                throw new ArgumentException("La capa ráster del .lyr no tiene renderer: " + lyrFile);
            if (destino.Raster == null)
                throw new ArgumentException("La capa de destino no tiene ráster accesible (¿fuente rota?): " + destino.Name);

            IObjectCopy copia = new ObjectCopyClass();
            IRasterRenderer renderer = (IRasterRenderer)copia.Copy(origen.Renderer);
            renderer.Raster = destino.Raster;
            renderer.Update();
            destino.Renderer = renderer;

            var detalle = new JObject
            {
                ["tipo"] = "raster",
                ["renderer"] = NombreRenderer(renderer)
            };

            ILayerEffects efOrigen = origen as ILayerEffects;
            ILayerEffects efDestino = destino as ILayerEffects;
            if (efOrigen != null && efDestino != null && efDestino.SupportsTransparency)
            {
                efDestino.Transparency = efOrigen.Transparency;
                detalle["transparencia"] = efOrigen.Transparency;
            }
            return detalle;
        }

        private static string NombreRenderer(IRasterRenderer renderer)
        {
            if (renderer is IRasterUniqueValueRenderer) return "valores únicos";
            if (renderer is IRasterClassifyColorRampRenderer) return "clasificado";
            if (renderer is IRasterStretchColorRampRenderer) return "estirado";
            if (renderer is IRasterRGBRenderer) return "RGB";
            return renderer.GetType().Name;
        }

        /// <summary>
        /// Simbología graduada (class breaks) sobre la capa VIVA, por un campo
        /// numérico. Cubre el hueco que no tapan las tools nativas: execute_arcpy no
        /// puede cambiar el renderer de la sesión (opera sobre una copia), y
        /// apply_symbology_from_layer necesita un .lyr plantilla. Aquí se construye
        /// el IClassBreaksRenderer directamente y se aplica in-process.
        ///
        /// El histograma se calcula sobre TODOS los valores del campo en la fuente
        /// (BasicTableHistogram no honra definition query ni selección); si necesitas
        /// clasificar un subconjunto, filtra antes con una definition query permanente.
        /// </summary>
        public static JObject SetGraduatedSymbology(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string campo = (string)parameters["campo"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(campo))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC) y 'campo' (campo numérico a graduar).");

            // Cortes fijos (los umbrales de un documento técnico): mandan sobre el método.
            double[] cortesFijos = LeerCortes(parameters["cortes"]);
            if (cortesFijos != null && (Parametros.Dado(parameters["num_clases"]) || Parametros.Dado(parameters["metodo"])))
                throw new ArgumentException("Con 'cortes' sobran 'num_clases' y 'metodo': las clases son las que dan los cortes.");
            int numClases = Parametros.LeerEntero(parameters["num_clases"], "num_clases", 5, 2, 32);
            string metodo = cortesFijos != null ? "cortes"
                : ((string)parameters["metodo"] ?? "natural_breaks").ToLowerInvariant();

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            IGeoFeatureLayer gfl = MapHandlers.FindLayer(map, capa) as IGeoFeatureLayer;
            if (gfl == null)
                throw new ArgumentException("Solo las capas de entidades admiten simbología graduada: " + capa);

            IFeatureClass fc = gfl.FeatureClass;
            if (fc == null)
                throw new ArgumentException("La capa no tiene fuente de datos accesible (¿rota?): " + capa);

            // El campo se busca en la DISPLAY TABLE, no en la feature class: los
            // campos de un join existen en la capa (y list_fields los ofrece) pero no
            // en la clase base, así que graduar por uno daba "Campo no encontrado".
            // Sin joins, la display table ES la feature class y no cambia nada.
            IFields camposCapa = QueryHandlers.CamposDeCapa(gfl);
            int idxCampo = camposCapa.FindField(campo);
            if (idxCampo < 0)
                throw new ArgumentException("Campo no encontrado en la capa '" + capa + "': " + campo
                    + ". Disponibles: " + NombresDeCampos(camposCapa));
            IField f = camposCapa.get_Field(idxCampo);
            if (!EsCampoNumerico(f.Type))
                throw new ArgumentException("El campo '" + campo + "' no es numérico (es "
                    + DataAccess.NombreTipoCampo(f.Type) + "); la simbología graduada necesita un campo numérico.");

            // Histograma de los valores del campo → clasificación en cortes.
            // Se tipa como la coclase (no ITableHistogram) para llamar a GetHistogram
            // sin arrastrar el tipo IHistogram, que vive en otro ensamblado no referenciado.
            BasicTableHistogramClass tableHist = new BasicTableHistogramClass();
            tableHist.Field = campo;
            tableHist.Table = QueryHandlers.TablaDeCapa(gfl);
            object dataValues, dataFreq;
            tableHist.GetHistogram(out dataValues, out dataFreq);

            double[] cortes;
            double? maximoDatos = null;
            if (cortesFijos != null)
            {
                // [mínimo de los datos, corte 1, ..., corte n]: la misma forma que da el clasificador.
                double[] vals = dataValues as double[];
                double minimo = vals != null && vals.Length > 0 ? vals.Min() : cortesFijos[0];
                if (vals != null && vals.Length > 0)
                    maximoDatos = vals.Max();
                cortes = new[] { Math.Min(minimo, cortesFijos[0]) }.Concat(cortesFijos).ToArray();
            }
            else
            {
                IClassifyGEN clasificador = Clasificador(metodo);
                int clasesDeseadas = numClases;
                try
                {
                    clasificador.Classify(dataValues, dataFreq, ref clasesDeseadas);
                }
                catch (Exception ex)
                {
                    throw new ArgumentException("No se pudo clasificar el campo '" + campo + "' con método '"
                        + metodo + "': " + ex.Message + ". ¿Tiene el campo suficientes valores distintos?");
                }
                cortes = (double[])clasificador.ClassBreaks;
                if (cortes == null || cortes.Length < 2)
                    throw new ArgumentException("El campo '" + campo + "' no tiene variación suficiente para "
                        + numClases + " clases (valores todos iguales o insuficientes).");
            }
            int n = cortes.Length - 1; // el clasificador puede reducir el número real de clases
            string[] etiquetas = LeerEtiquetasDeClases(parameters["etiquetas"], cortes);

            // Rampa de color: por defecto amarillo claro → rojo oscuro (secuencial),
            // interpolada en CIE Lab y NO en HSV. Medido el 2026-09-04 sobre el
            // ráster (ver RasterSymbologyHandlers.LeerAlgoritmo): HSV interpola el
            // TONO dando la vuelta a la rueda de color y devuelve un arcoíris del
            // que no se lee ningún orden — justo lo contrario de lo que pide una
            // simbología graduada. Se construye con el MISMO helper que el ráster,
            // que además lanza si CreateRamp falla en vez de seguir con una leyenda
            // entera del mismo color.
            IColor desde = Parametros.LeerColor(parameters["color_desde"], "color_desde", 255, 255, 178);
            IColor hasta = Parametros.LeerColor(parameters["color_hasta"], "color_hasta", 189, 0, 38);
            IColor[] paleta = RasterSymbologyHandlers.ConstruirRampa(desde, hasta, n,
                RasterSymbologyHandlers.LeerAlgoritmo(parameters["algoritmo"]));

            esriGeometryType shp = fc.ShapeType;
            double tam = LeerDouble(parameters["tamano"],
                shp == esriGeometryType.esriGeometryPolyline ? 2.0
                : shp == esriGeometryType.esriGeometryPolygon ? 0.4 : 6.0);

            IClassBreaksRenderer render = new ClassBreaksRendererClass
            {
                Field = campo,
                BreakCount = n,
                SortClassesAscending = true,
                MinimumBreak = cortes[0]
            };
            for (int i = 0; i < n; i++)
            {
                render.set_Break(i, cortes[i + 1]);
                render.set_Symbol(i, SimboloGraduado(shp, paleta[i], tam));
                render.set_Label(i, etiquetas != null ? etiquetas[i] : EtiquetaClase(cortes[i], cortes[i + 1]));
            }

            gfl.Renderer = (IFeatureRenderer)render;
            MapHandlers.NotificarCambioContenido(map, doc);

            // Leído de vuelta del renderer de la capa, no de lo que se quiso poner.
            IClassBreaksRenderer leido = (IClassBreaksRenderer)gfl.Renderer;
            var breaksJson = new JArray { leido.MinimumBreak };
            var etiquetasJson = new JArray();
            for (int i = 0; i < leido.BreakCount; i++)
            {
                breaksJson.Add(leido.get_Break(i));
                etiquetasJson.Add(leido.get_Label(i));
            }

            var r = new JObject
            {
                ["capa"] = gfl.Name,
                ["campo"] = campo,
                ["metodo"] = metodo,
                ["num_clases"] = leido.BreakCount,
                ["cortes"] = breaksJson,
                ["etiquetas"] = etiquetasJson
            };
            if (maximoDatos != null && maximoDatos.Value > cortes[n])
                r["aviso"] = "El máximo del campo (" + FormatoNum(maximoDatos.Value) + ") pasa del último corte ("
                    + FormatoNum(cortes[n]) + "): las entidades por encima no se dibujan. Añade un corte final"
                    + " mayor o igual que el máximo.";
            return Protocol.Result(r);
        }

        /// <summary>Límites superiores de cada clase, estrictamente crecientes; null si no vienen.</summary>
        internal static double[] LeerCortes(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            JArray a = t as JArray;
            if (a == null || a.Count < 1 || a.Count > 32)
                throw new ArgumentException("'cortes' es una lista de 1 a 32 números: el límite SUPERIOR de cada clase,"
                    + " de menor a mayor.");
            var v = new double[a.Count];
            for (int i = 0; i < a.Count; i++)
            {
                if (a[i].Type != JTokenType.Integer && a[i].Type != JTokenType.Float)
                    throw new ArgumentException("'cortes[" + i + "]' no es un número: " + a[i]);
                v[i] = (double)a[i];
                if (i > 0 && v[i] <= v[i - 1])
                    throw new ArgumentException("'cortes' tiene que ir de menor a mayor y sin repetir: "
                        + v[i - 1] + " y luego " + v[i] + ".");
            }
            return v;
        }

        /// <summary>
        /// Etiquetas de las clases: una lista (una por clase) o "auto", que las escribe a partir
        /// de los cortes con coma decimal: «≤ a», «a - b», …, «> y». ≤ y no <, porque una clase
        /// por cortes INCLUYE su límite superior. null si no vienen: el que llama pone las suyas.
        /// `cortes` lleva el mínimo delante (n + 1 valores para n clases).
        /// </summary>
        internal static string[] LeerEtiquetasDeClases(JToken t, double[] cortes)
        {
            if (!Parametros.Dado(t))
                return null;
            int n = cortes.Length - 1;
            if (t.Type == JTokenType.String)
            {
                // "auto<": la primera como «< a» en vez de «≤ a». Es la convención de unos planos
                // reales, aunque la clase incluya su límite.
                string modo = ((string)t).Trim();
                bool menor = string.Equals(modo, "auto<", StringComparison.OrdinalIgnoreCase);
                if (!menor && !string.Equals(modo, "auto", StringComparison.OrdinalIgnoreCase))
                    throw new ArgumentException("'etiquetas' es una lista (una por clase), \"auto\" (≤ a, a - b, > b)"
                        + " o \"auto<\" (< a, a - b, > b).");
                var es = CultureInfo.GetCultureInfo("es-ES");
                Func<double, string> f = v => v.ToString("0.##", es);
                var auto = new string[n];
                for (int i = 0; i < n; i++)
                    auto[i] = n == 1 ? f(cortes[0]) + " - " + f(cortes[1])
                        : i == 0 ? (menor ? "< " : "≤ ") + f(cortes[1])
                        : i == n - 1 ? "> " + f(cortes[n - 1])
                        : f(cortes[i]) + " - " + f(cortes[i + 1]);
                return auto;
            }
            JArray a = t as JArray;
            if (a == null || a.Count != n)
                throw new ArgumentException("'etiquetas' trae " + (a == null ? "algo que no es una lista" : a.Count + " textos")
                    + " y hay " + n + " clases: una por clase, o \"auto\".");
            return a.Select(x => (string)x ?? "").ToArray();
        }

        private static bool EsCampoNumerico(esriFieldType tipo)
        {
            return tipo == esriFieldType.esriFieldTypeSmallInteger
                || tipo == esriFieldType.esriFieldTypeInteger
                || tipo == esriFieldType.esriFieldTypeSingle
                || tipo == esriFieldType.esriFieldTypeDouble;
        }

        /// <summary>Método de clasificación arcpy → coclase IClassifyGEN.</summary>
        private static IClassifyGEN Clasificador(string metodo)
        {
            switch (metodo)
            {
                case "natural_breaks":
                case "jenks":
                    return new NaturalBreaksClass();
                case "quantile":
                case "cuantil":
                    return new QuantileClass();
                case "equal_interval":
                case "intervalo_igual":
                    return new EqualIntervalClass();
                case "geometrical_interval":
                case "intervalo_geometrico":
                    return new GeometricalIntervalClass();
                case "standard_deviation":
                case "desviacion_estandar":
                    return new StandardDeviationClass();
                default:
                    throw new ArgumentException("Método de clasificación no soportado: '" + metodo
                        + "'. Usa natural_breaks, quantile, equal_interval, geometrical_interval o standard_deviation.");
            }
        }

        /// <summary>Símbolo relleno de un color según la geometría de la capa.</summary>
        private static ISymbol SimboloGraduado(esriGeometryType shp, IColor color, double tam)
        {
            switch (shp)
            {
                case esriGeometryType.esriGeometryPolygon:
                case esriGeometryType.esriGeometryMultiPatch:
                    ISimpleFillSymbol fill = new SimpleFillSymbolClass { Color = color };
                    ILineSymbol borde = new SimpleLineSymbolClass
                    {
                        Color = GrisBorde(),
                        Width = tam
                    };
                    fill.Outline = borde;
                    return (ISymbol)fill;
                case esriGeometryType.esriGeometryPolyline:
                    return (ISymbol)new SimpleLineSymbolClass { Color = color, Width = tam };
                default: // punto / multipunto
                    return (ISymbol)new SimpleMarkerSymbolClass { Color = color, Size = tam };
            }
        }

        private static IColor GrisBorde()
        {
            return new RgbColorClass { Red = 130, Green = 130, Blue = 130 };
        }

        private static string NombresDeCampos(IFields campos)
        {
            var nombres = new System.Collections.Generic.List<string>();
            for (int i = 0; i < campos.FieldCount; i++)
                nombres.Add(campos.get_Field(i).Name);
            return string.Join(", ", nombres);
        }

        private static double LeerDouble(JToken t, double porDefecto)
        {
            return t != null && t.Type != JTokenType.Null ? (double)t : porDefecto;
        }

        private static string EtiquetaClase(double lo, double hi)
        {
            return FormatoNum(lo) + " - " + FormatoNum(hi);
        }

        private static string FormatoNum(double v)
        {
            // Enteros sin decimales; el resto con hasta 2 decimales, sin ceros de cola.
            if (Math.Abs(v - Math.Round(v)) < 1e-9)
                return ((long)Math.Round(v)).ToString(CultureInfo.InvariantCulture);
            return v.ToString("0.##", CultureInfo.InvariantCulture);
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

        private static IRasterLayer PrimerRasterLayer(ILayer lyr)
        {
            IRasterLayer rl = lyr as IRasterLayer;
            if (rl != null)
                return rl;
            ICompositeLayer comp = lyr as ICompositeLayer;
            if (comp == null)
                return null;
            for (int i = 0; i < comp.Count; i++)
            {
                IRasterLayer hijo = PrimerRasterLayer(comp.get_Layer(i));
                if (hijo != null)
                    return hijo;
            }
            return null;
        }

        internal static IGroupLayer BuscarGrupoPadre(IMap map, ILayer objetivo)
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
