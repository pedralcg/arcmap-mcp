using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using ArcmapMcp.AddIn;
using ArcmapMcp.AddIn.Handlers;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.Disco
{
    /// <summary>
    /// arcmap-mcp-disco.exe trabajo.json — aplica una lista de operaciones a UN .mxd del disco
    /// con los mismos handlers del add-in (ADR-010) y lo guarda, todo o nada.
    ///
    /// trabajo.json: {"mxd": ruta, "operaciones": [{"tool": "set_legend", ...}], "bak": true}
    /// Salida (stdout, una línea JSON): ok, operaciones (lo que devolvió cada una), guardado,
    /// bak y verificacion: las mismas lecturas de estado hechas justo antes de guardar y al
    /// REABRIR el documento. Lo leído en el mismo proceso no prueba que se haya guardado (una
    /// ruta relativa larga se pierde al escribir y en memoria se ve bien), y reabrir sí.
    ///
    /// El servidor MCP ya ha rechazado los .mxd con marcos OLE (guardarlos desde fuera los
    /// corrompe) y el documento abierto en ArcMap. Aquí no se exporta nada: un proceso que ha
    /// guardado no vuelve a exportar; eso es export_mxd_lote, en otra llamada.
    /// </summary>
    internal static class Program
    {
        /// <summary>Las operaciones del lote: herramientas vivas que pasan por la costura
        /// Documento y cambian algo. Mismos nombres y parámetros que en vivo.</summary>
        private static readonly Dictionary<string, Func<JObject, JObject>> Operaciones =
            new Dictionary<string, Func<JObject, JObject>>
            {
                { "set_legend",                  LayoutHandlers.SetLegend },
                { "set_legend_item",             LayoutHandlers.SetLegendItem },
                { "set_text_element",            LayoutHandlers.SetTextElement },
                { "set_labels",                  LabelHandlers.SetLabels },
                { "edit_symbol",                 SymbolHandlers.EditSymbol },
                { "set_single_symbology",        SymbolHandlers.SetSingleSymbology },
                { "set_unique_values_symbology", UniqueValuesHandlers.SetUniqueValuesSymbology },
                { "set_graduated_symbology",     LayerHandlers.SetGraduatedSymbology },
                { "set_raster_symbology",        RasterSymbologyHandlers.SetRasterSymbology },
                { "apply_symbology_from_layer",  LayerHandlers.ApplySymbologyFromLayer },
                { "add_layer",                   LayerHandlers.AddLayer },
                { "add_group",                   LayerHandlers.AddGroup },
                { "remove_layer",                LayerHandlers.RemoveLayer },
                { "rename_layer",                LayerHandlers.RenameLayer },
                { "duplicate_layer",             LayerHandlers.DuplicateLayer },
                { "move_layer",                  TocHandlers.MoveLayer },
                { "set_layer_visibility",        MapHandlers.SetLayerVisibility },
                { "set_definition_query",        MapHandlers.SetDefinitionQuery },
                { "set_scale",                   MapHandlers.SetScale },
                { "set_extent",                  MapHandlers.SetExtent },
                { "replace_data_source",         SourceHandlers.ReplaceDataSource },
                { "replace_text",                LayoutHandlers.ReplaceText },
                { "save_layer_as_lyr",           LayerHandlers.SaveLayerAsLyr },
                { "copy_layout_elements",        CopiaElementosHandlers.CopyLayoutElements },
                { "set_frame_clip",              DataFrameHandlers.SetFrameClip },
            };

        private static readonly HashSet<string> DeSimbologia = new HashSet<string>
        {
            "edit_symbol", "set_single_symbology", "set_unique_values_symbology", "set_graduated_symbology",
            "set_raster_symbology", "apply_symbology_from_layer"
        };

        [STAThread]
        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            JObject salida;
            int codigo;
            try
            {
                if (args.Length != 1 || !File.Exists(args[0]))
                    throw new ArgumentException("Uso: arcmap-mcp-disco.exe trabajo.json");
                JObject trabajo = JObject.Parse(File.ReadAllText(args[0], Encoding.UTF8));
                salida = Ejecutar(trabajo);
                codigo = (bool)salida["ok"] ? 0 : 1;
            }
            catch (Exception ex)
            {
                salida = new JObject { ["ok"] = false, ["error"] = ex.Message, ["traceback"] = ex.ToString() };
                codigo = 2;
            }
            Console.Out.WriteLine(salida.ToString(Formatting.None));
            Console.Out.Flush();
            return codigo;
        }

        private static JObject Ejecutar(JObject trabajo)
        {
            string mxd = (string)trabajo["mxd"];
            if (trabajo["inventario"] != null && (bool)trabajo["inventario"])
                return Inventario(mxd);
            JArray ops = trabajo["operaciones"] as JArray;
            bool bak = trabajo["bak"] == null || (bool)trabajo["bak"];
            // origen: el plano se CREA en `mxd` a partir de una plantilla (construir_planos.py
            // de una serie real: 13 hojas desde una plantilla). Se abre el origen, se aplican las
            // operaciones en memoria y se guarda con SaveAs en el destino, que reescribe las
            // rutas relativas para la carpeta nueva. Si una operación falla no se crea nada.
            string origen = (string)trabajo["origen"];
            if (string.IsNullOrEmpty(mxd))
                throw new ArgumentException("Falta 'mxd'.");
            if (origen != null)
            {
                if (!File.Exists(origen))
                    throw new ArgumentException("No existe la plantilla: " + origen);
                bool sobrescribir = trabajo["sobrescribir"] != null && (bool)trabajo["sobrescribir"];
                if (File.Exists(mxd) && !sobrescribir)
                    throw new ArgumentException("El destino ya existe: " + mxd + ". Pasa sobrescribir=True para pisarlo.");
            }
            else if (!File.Exists(mxd))
                throw new ArgumentException("No existe el .mxd: " + mxd);
            if (ops == null)
                ops = new JArray();
            // Sin operaciones solo vale si se crea desde una plantilla (copiar y nada más).
            if (ops.Count == 0 && origen == null)
                throw new ArgumentException("'operaciones' está vacía.");
            foreach (JToken op in ops)
            {
                string tool = (string)op["tool"];
                if (tool == null || !Operaciones.ContainsKey(tool))
                    throw new ArgumentException("Operación no admitida en el lote: '" + tool + "'. Admitidas: "
                        + string.Join(", ", Operaciones.Keys));
            }

            IAoInitialize ao = Licencia();
            var r = new JObject { ["mxd"] = mxd };
            try
            {
                // 1. Abrir y aplicar, todo en memoria.
                IMapDocument doc = new MapDocumentClass();
                doc.Open(origen ?? mxd, "");
                if (origen != null)
                    r["origen"] = origen;
                var hechas = new JArray();
                bool todasBien = true;
                List<Tuple<string, JObject>> lecturas = Lecturas(ops);
                List<string> antes;
                try
                {
                    using (Documento.Usar(Documento.EnDisco(doc)))
                    {
                        foreach (JToken op in ops)
                        {
                            string tool = (string)op["tool"];
                            JObject p = (JObject)op.DeepClone();
                            p.Remove("tool");
                            JObject res;
                            try { res = Operaciones[tool](p); }
                            catch (Exception ex) { res = Protocol.Error(Mensaje(ex)); }
                            res["tool"] = tool;
                            hechas.Add(res);
                            if (!(bool)res["ok"])
                            {
                                todasBien = false;
                                break;
                            }
                        }
                        antes = todasBien ? Leer(lecturas, doc) : null;
                    }
                    r["operaciones"] = hechas;
                    if (!todasBien)
                    {
                        // Todo o nada: no se guarda, y el .mxd queda como estaba.
                        r["ok"] = false;
                        r["guardado"] = false;
                        r["error"] = "Falló la operación " + hechas.Count + " (" + hechas.Last()["tool"] + "): "
                            + hechas.Last()["error"] + ". El .mxd NO se ha guardado.";
                        return r;
                    }

                    // 2. Copia de seguridad y guardado, con el ajuste de rutas del propio documento.
                    if (bak && File.Exists(mxd))
                    {
                        string copia = mxd + ".bak";
                        File.Copy(mxd, copia, true);
                        r["bak"] = copia;
                    }
                    if (origen != null)
                    {
                        string carpeta = System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(mxd));
                        if (!Directory.Exists(carpeta))
                            Directory.CreateDirectory(carpeta);
                        doc.SaveAs(mxd, doc.UsesRelativePaths, false);
                        r["creado"] = true;
                    }
                    else
                        doc.Save(doc.UsesRelativePaths, false);
                    r["guardado"] = true;
                }
                finally
                {
                    doc.Close();
                }

                // 3. Reabrir y leer lo mismo: es lo único que prueba lo guardado.
                IMapDocument otra = new MapDocumentClass();
                otra.Open(mxd, "");
                List<string> despues;
                try
                {
                    using (Documento.Usar(Documento.EnDisco(otra)))
                        despues = Leer(lecturas, otra);
                }
                finally
                {
                    otra.Close();
                }
                var diferencias = new JArray();
                // Una lectura que da error no verifica nada, aunque dé el MISMO error antes y después:
                // edit_symbol("Zonas") y luego rename_layer("Zonas"→…) leía «capa no encontrada» dos
                // veces y salía verificado (revisión del 2026-10-25). Va como diferencia, con el motivo.
                var fallidas = new JArray();
                for (int i = 0; i < lecturas.Count; i++)
                {
                    string error = ErrorDeLectura(antes[i]) ?? ErrorDeLectura(despues[i]);
                    if (error != null)
                        fallidas.Add(new JObject
                        {
                            ["lectura"] = lecturas[i].Item1,
                            ["parametros"] = lecturas[i].Item2,
                            ["error"] = error
                        });
                    else if (antes[i] != despues[i])
                        diferencias.Add(new JObject
                        {
                            ["lectura"] = lecturas[i].Item1,
                            ["antes_de_guardar"] = Recortar(antes[i]),
                            ["al_reabrir"] = Recortar(despues[i])
                        });
                }
                var lecturasJson = new JArray();
                for (int i = 0; i < lecturas.Count; i++)
                    lecturasJson.Add(new JObject
                    {
                        ["lectura"] = lecturas[i].Item1,
                        ["parametros"] = lecturas[i].Item2,
                        ["al_reabrir"] = JToken.Parse(despues[i])
                    });
                bool verificado = diferencias.Count == 0 && fallidas.Count == 0;
                r["verificacion"] = new JObject
                {
                    ["verificado"] = verificado,
                    ["diferencias"] = diferencias,
                    ["lecturas"] = lecturasJson
                };
                if (fallidas.Count > 0)
                    ((JObject)r["verificacion"])["lecturas_fallidas"] = fallidas;
                r["ok"] = verificado;
                if (diferencias.Count > 0)
                    r["error"] = "El .mxd se guardó, pero al reabrirlo " + diferencias.Count + " lectura(s) no coinciden"
                        + " con lo que había antes de guardar: mira 'verificacion.diferencias'. La copia anterior"
                        + " está en " + (r["bak"] ?? "(sin .bak)") + ".";
                else if (fallidas.Count > 0)
                    r["error"] = "El .mxd se guardó, pero " + fallidas.Count + " lectura(s) de verificación fallan y no"
                        + " prueban nada (" + (string)fallidas[0]["lectura"] + ": " + (string)fallidas[0]["error"]
                        + "): mira 'verificacion.lecturas_fallidas'. Suele ser una capa renombrada o quitada después"
                        + " de tocarla. La copia anterior está en " + (r["bak"] ?? "(sin .bak)") + ".";
                return r;
            }
            finally
            {
                ao.Shutdown();
            }
        }

        /// <summary>
        /// trabajo.json {"mxd": ruta, "inventario": true}: lee el documento SIN tocarlo ni guardarlo y
        /// devuelve su inventario rico, con los mismos handlers que la sesión viva: por capa del data
        /// frame activo, TOC (ruta, visible, fuente, query, etiquetas), simbología (edit_symbol) y
        /// etiquetado (set_labels); los textos del layout, y cada leyenda con sus entradas y su
        /// formato. Es la base de audit_folder(detalle) y de compare_mxd: comparar un plano con su
        /// plantilla solo es justo si los dos se leen con el mismo lector.
        /// </summary>
        private static JObject Inventario(string mxd)
        {
            if (string.IsNullOrEmpty(mxd) || !File.Exists(mxd))
                throw new ArgumentException("No existe el .mxd: " + mxd);
            IAoInitialize ao = Licencia();
            try
            {
                IMapDocument doc = new MapDocumentClass();
                doc.Open(mxd, "");
                try
                {
                    using (Documento.Usar(Documento.EnDisco(doc)))
                    {
                        JObject toc = Toc(doc);
                        IMap map = doc.ActiveView.FocusMap;
                        var porRuta = MapHandlers.CapasConRuta(map).ToDictionary(c => c.Ruta, c => c.Capa);
                        foreach (JObject capa in (JArray)toc["capas"])
                        {
                            string ruta = (string)capa["ruta"];
                            ILayer lyr;
                            if (!porRuta.TryGetValue(ruta, out lyr) || lyr is IGroupLayer)
                            {
                                if (lyr is IGroupLayer)
                                    capa["grupo"] = true;
                                continue;
                            }
                            if (lyr is IGeoFeatureLayer || lyr is IRasterLayer)
                                capa["simbologia"] = Leido(() => SymbolHandlers.EditSymbol(new JObject { ["capa"] = "/" + ruta }));
                            if (lyr is IGeoFeatureLayer)
                                capa["etiquetado"] = Leido(() => LabelHandlers.SetLabels(new JObject { ["capa"] = "/" + ruta }));
                        }
                        return new JObject
                        {
                            ["ok"] = true,
                            ["mxd"] = mxd,
                            ["inventario"] = new JObject
                            {
                                ["data_frame"] = toc["data_frame"],
                                ["escala"] = toc["escala"],
                                ["capas"] = toc["capas"],
                                ["textos"] = Textos(doc),
                                ["leyendas"] = LeyendasDetalle(doc)
                            }
                        };
                    }
                }
                finally
                {
                    doc.Close();
                }
            }
            finally
            {
                ao.Shutdown();
            }
        }

        /// <summary>Los textos del layout, también dentro de grupos, con el centro de lo dibujado y el
        /// tamaño: los del cajetín no suelen tener nombre, y para emparejarlos entre dos planos
        /// solo queda su posición.</summary>
        private static JArray Textos(IMapDocument doc)
        {
            var salida = new JArray();
            foreach (LayoutHandlers.ElementoLayout e in LayoutHandlers.Elementos((IGraphicsContainer)doc.PageLayout))
            {
                ITextElement te = e.Elemento as ITextElement;
                if (te == null)
                    continue;
                var o = new JObject { ["nombre"] = e.Nombre, ["grupo"] = e.Grupo, ["texto"] = te.Text };
                try
                {
                    IEnvelope env = e.Elemento.Geometry.Envelope;
                    o["centro"] = new JArray(Math.Round((env.XMin + env.XMax) / 2, 2), Math.Round((env.YMin + env.YMax) / 2, 2));
                }
                catch { }
                try { o["tamano"] = Math.Round(te.Symbol.Size, 2); } catch { }
                salida.Add(o);
            }
            return salida;
        }

        /// <summary>El mensaje si la lectura (ya serializada) es un `{"error": ...}`; null si no.</summary>
        private static string ErrorDeLectura(string leida)
        {
            try
            {
                JObject o = JToken.Parse(leida) as JObject;
                return o != null && o.Count == 1 && o["error"] != null ? (string)o["error"] ?? "error" : null;
            }
            catch { return null; }
        }

        /// <summary>El resultado de un handler de lectura sin el sobre del protocolo; su error, si falla.</summary>
        private static JObject Leido(Func<JObject> leer)
        {
            try
            {
                JObject r = leer();
                if (r["ok"] != null && !(bool)r["ok"])
                    return new JObject { ["error"] = r["error"] };
                JObject cuerpo = r["result"] as JObject ?? r;
                cuerpo.Remove("ok");
                return cuerpo;
            }
            catch (Exception ex)
            {
                return new JObject { ["error"] = Mensaje(ex) };
            }
        }

        /// <summary>Leyendas() más lo que interesa al comparar: data frame, caja y formato.</summary>
        private static JArray LeyendasDetalle(IMapDocument doc)
        {
            JArray basicas = Leyendas(doc);
            var salida = new JArray();
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            gc.Reset();
            IElement el;
            int k = 0;
            while ((el = gc.Next()) != null)
            {
                IMapSurroundFrame msf = el as IMapSurroundFrame;
                ILegend lg = msf != null ? msf.MapSurround as ILegend : null;
                if (lg == null)
                    continue;
                JObject o = (JObject)basicas[k++];
                IEnvelope env = el.Geometry.Envelope;
                o["data_frame"] = lg.Map != null ? lg.Map.Name : null;
                o["caja"] = new JArray(Math.Round(env.XMin, 2), Math.Round(env.YMin, 2), Math.Round(env.XMax, 2), Math.Round(env.YMax, 2));
                try { o["formato"] = LayoutHandlers.FormatoLeyenda(lg); }
                catch (Exception ex) { o["formato"] = new JObject { ["error"] = Mensaje(ex) }; }
                salida.Add(o);
            }
            return salida;
        }

        /// <summary>Qué leer para comprobar lo guardado: la TOC y las leyendas siempre, los
        /// textos si se tocan, y por capa la simbología o las etiquetas que se hayan tocado.</summary>
        private static List<Tuple<string, JObject>> Lecturas(JArray ops)
        {
            var l = new List<Tuple<string, JObject>>
            {
                Tuple.Create("toc", new JObject()),
                Tuple.Create("leyendas", new JObject())
            };
            Action<string, JObject> anadir = (n, p) =>
            {
                if (!l.Any(x => x.Item1 == n && JToken.DeepEquals(x.Item2, p)))
                    l.Add(Tuple.Create(n, p));
            };
            foreach (JToken op in ops)
            {
                string tool = (string)op["tool"];
                string capa = (string)op["capa"];
                if (tool == "set_text_element" || tool == "replace_text")
                    anadir("list_layout_elements", new JObject { ["tipo"] = "TEXT_ELEMENT" });
                else if (tool == "set_legend")
                    anadir("set_legend", new JObject { ["leyenda"] = op["leyenda"], ["indice"] = op["indice"] });
                else if (tool == "set_labels" && capa != null)
                    anadir("set_labels", new JObject { ["capa"] = capa });
                else if (DeSimbologia.Contains(tool) && capa != null)
                    anadir("edit_symbol", new JObject { ["capa"] = capa });
                else if (tool == "set_frame_clip")
                    anadir("set_frame_clip", new JObject { ["data_frame"] = op["data_frame"] });
                else if (tool == "set_extent" && op["coords"] is JArray && ((JArray)op["coords"]).Count == 4)
                    anadir("set_extent", new JObject { ["coords"] = op["coords"] });
                // Sin esta lectura, un set_scale que no se guardaba salía verificado (2026-10-28).
                else if (tool == "set_scale" && op["escala"] != null && op["escala"].Type != JTokenType.Null)
                    anadir("set_scale", new JObject { ["escala"] = op["escala"] });
            }
            return l;
        }

        private static List<string> Leer(List<Tuple<string, JObject>> lecturas, IMapDocument doc)
        {
            var salida = new List<string>();
            foreach (var lec in lecturas)
            {
                JToken valor;
                try
                {
                    switch (lec.Item1)
                    {
                        case "toc": valor = Toc(doc); break;
                        case "leyendas": valor = Leyendas(doc); break;
                        case "list_layout_elements": valor = LayoutHandlers.ListLayoutElements((JObject)lec.Item2.DeepClone()); break;
                        case "set_legend": valor = LayoutHandlers.SetLegend((JObject)lec.Item2.DeepClone()); break;
                        case "set_labels": valor = LabelHandlers.SetLabels((JObject)lec.Item2.DeepClone()); break;
                        case "edit_symbol": valor = SymbolHandlers.EditSymbol((JObject)lec.Item2.DeepClone()); break;
                        case "set_frame_clip": valor = DataFrameHandlers.SetFrameClip((JObject)lec.Item2.DeepClone()); break;
                        case "set_extent": valor = ComprobarExtension(doc, (JArray)lec.Item2["coords"]); break;
                        case "set_scale": valor = ComprobarEscala(doc, (double)lec.Item2["escala"]); break;
                        default: valor = "lectura desconocida"; break;
                    }
                }
                catch (Exception ex)
                {
                    valor = new JObject { ["error"] = Mensaje(ex) };
                }
                salida.Add(valor.ToString(Formatting.None));
            }
            return salida;
        }

        /// <summary>Capas del data frame activo con lo que las operaciones de TOC cambian.</summary>
        private static JObject Toc(IMapDocument doc)
        {
            IMap map = doc.ActiveView.FocusMap;
            var capas = new JArray();
            foreach (MapHandlers.CapaEnMapa c in MapHandlers.CapasConRuta(map))
            {
                var o = new JObject { ["ruta"] = c.Ruta, ["visible"] = c.Capa.Visible };
                // Una capa que deja de resolver al reabrir (la ruta relativa de ≥ 260 que
                // ArcMap no escribe) es justo lo que esta verificación tiene que cazar.
                ILayer2 l2 = c.Capa as ILayer2;
                if (l2 != null && !(c.Capa is IGroupLayer))
                    o["valida"] = l2.Valid;
                // La fuente, para que replace_data_source también se compruebe al reabrir.
                try
                {
                    IDataLayer dlc = c.Capa as IDataLayer;
                    ESRI.ArcGIS.Geodatabase.IDatasetName dsn = dlc != null && !(c.Capa is IGroupLayer)
                        ? dlc.DataSourceName as ESRI.ArcGIS.Geodatabase.IDatasetName : null;
                    if (dsn != null)
                        // Sin la barra final: en memoria el workspace de un ráster sale «...\» y
                        // al reabrir sin ella, y era una diferencia falsa en todo lote con rásteres.
                        o["fuente"] = (dsn.WorkspaceName != null ? (dsn.WorkspaceName.PathName ?? "").TrimEnd('\\', '/') : "")
                            + "|" + dsn.Name;
                }
                catch { }
                IFeatureLayerDefinition dq = c.Capa as IFeatureLayerDefinition;
                if (dq != null)
                    o["definition_query"] = dq.DefinitionExpression;
                IGeoFeatureLayer gfl = c.Capa as IGeoFeatureLayer;
                if (gfl != null)
                    o["etiquetas"] = gfl.DisplayAnnotation;
                capas.Add(o);
            }
            double? escala = null;
            try { escala = Math.Round(map.MapScale, 2); } catch { }
            return new JObject { ["data_frame"] = map.Name, ["escala"] = escala, ["capas"] = capas };
        }

        /// <summary>
        /// Comprobación de set_extent(coords). La extensión no se compara tal cual: con la vista
        /// activada (la operación la activa) sale ajustada a la proporción del marco, y al reabrir
        /// sin activar sale la guardada, que es la pedida. Lo que se comprueba en los dos casos es
        /// lo que importa: que la extensión CONTIENE lo pedido y lo ajusta por un eje.
        /// </summary>
        private static JObject ComprobarExtension(IMapDocument doc, JArray coords)
        {
            IEnvelope e = ((IActiveView)doc.ActiveView.FocusMap).Extent;
            double x0 = (double)coords[0], y0 = (double)coords[1], x1 = (double)coords[2], y1 = (double)coords[3];
            const double tol = 1.0;
            bool contiene = e.XMin <= x0 + tol && e.YMin <= y0 + tol && e.XMax >= x1 - tol && e.YMax >= y1 - tol;
            bool ajustaX = Math.Abs(e.XMin - x0) <= tol && Math.Abs(e.XMax - x1) <= tol;
            bool ajustaY = Math.Abs(e.YMin - y0) <= tol && Math.Abs(e.YMax - y1) <= tol;
            return new JObject { ["contiene_lo_pedido"] = contiene, ["ajustada_por_un_eje"] = ajustaX || ajustaY };
        }

        /// <summary>Si la escala del data frame activo es la pedida (con un 0,5 % de margen: ArcMap
        /// la guarda con decimales). Se lee igual antes de guardar y al reabrir.</summary>
        private static JObject ComprobarEscala(IMapDocument doc, double pedida)
        {
            double escala = doc.ActiveView.FocusMap.MapScale;
            return new JObject { ["escala_es_la_pedida"] = Math.Abs(escala - pedida) <= 0.005 * pedida };
        }

        /// <summary>Cada leyenda del layout con sus entradas en orden y sus interruptores.</summary>
        private static JArray Leyendas(IMapDocument doc)
        {
            var salida = new JArray();
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            gc.Reset();
            IElement el;
            while ((el = gc.Next()) != null)
            {
                IMapSurroundFrame msf = el as IMapSurroundFrame;
                ILegend lg = msf != null ? msf.MapSurround as ILegend : null;
                if (lg == null)
                    continue;
                var items = new JArray();
                for (int k = 0; k < lg.ItemCount; k++)
                {
                    ILegendItem it = lg.get_Item(k);
                    items.Add(new JObject
                    {
                        ["capa"] = it.Layer != null ? it.Layer.Name : null,
                        ["nombre"] = it.ShowLayerName,
                        ["encabezado"] = it.ShowHeading,
                        ["etiquetas"] = it.ShowLabels,
                        ["columna_nueva"] = it.NewColumn
                    });
                }
                salida.Add(new JObject { ["leyenda"] = ((IElementProperties)el).Name, ["entradas"] = items });
            }
            return salida;
        }

        private static IAoInitialize Licencia()
        {
            if (!ESRI.ArcGIS.RuntimeManager.Bind(ESRI.ArcGIS.ProductCode.Desktop))
                throw new InvalidOperationException("No se pudo enlazar con ArcGIS Desktop (¿está instalado?).");
            IAoInitialize ao = new AoInitializeClass();
            foreach (esriLicenseProductCode codigo in new[] {
                         esriLicenseProductCode.esriLicenseProductCodeAdvanced,
                         esriLicenseProductCode.esriLicenseProductCodeStandard,
                         esriLicenseProductCode.esriLicenseProductCodeBasic })
            {
                if (ao.IsProductCodeAvailable(codigo) != esriLicenseStatus.esriLicenseAvailable)
                    continue;
                if (ao.Initialize(codigo) == esriLicenseStatus.esriLicenseCheckedOut)
                    return ao;
            }
            throw new InvalidOperationException("No hay licencia de ArcGIS Desktop disponible (Basic, Standard o"
                + " Advanced) para abrir el .mxd.");
        }

        private static string Mensaje(Exception ex)
        {
            while (ex is System.Reflection.TargetInvocationException && ex.InnerException != null)
                ex = ex.InnerException;
            return ex.Message;
        }

        private static string Recortar(string s)
        {
            return s.Length > 600 ? s.Substring(0, 600) + "…" : s;
        }
    }
}
