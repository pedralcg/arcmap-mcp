using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Geometry;
using Newtonsoft.Json.Linq;
using Path = System.IO.Path;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Símbolos de los estilos .style del usuario (IStyleGallery).
    ///
    /// El hueco (2026-09-25): los estilos propios —Pedro10.0.style, iden.style— solo
    /// se podían usar a mano desde el selector de símbolos; ninguna tool los leía.
    ///
    /// - list_style_symbols: sin estilo, los estilos cargados en ArcMap; con estilo,
    ///   sus símbolos (nombre, categoría, clase), filtrables.
    /// - apply_style_symbol: pone un símbolo del estilo como símbolo único de una
    ///   capa, por la misma vía que set_single_symbology.
    ///
    /// Un .style que no estaba cargado se añade a la galería solo mientras dura la
    /// llamada y se quita después: la galería es la del usuario (sus «Referencias de
    /// estilo») y no se deja cambiada por una consulta.
    ///
    /// Las clases de la galería se buscan por lo que CREAN (relleno, línea,
    /// marcador) y no solo por su nombre: en un ArcMap en español el nombre puede
    /// salir traducido, igual que el del motor de etiquetas.
    /// </summary>
    internal static class StyleHandlers
    {
        private const int LimiteMaximo = 2000;

        public static JObject ListStyleSymbols(JObject parameters)
        {
            IMxDocument doc;
            MapHandlers.FocusMap(out doc);
            IStyleGallery galeria = doc.StyleGallery;

            string estilo = Texto(parameters["estilo"]);
            if (estilo == null)
                return Protocol.Result(EstilosCargados(galeria));

            string patron = Texto(parameters["patron"]);
            Regex filtro = Filtro(patron);
            int limite = Parametros.LeerEntero(parameters["limite"], "limite", 200, 1, LimiteMaximo);
            List<ClaseGaleria> clases = ClasesPedidas(galeria, Texto(parameters["clase"]), null);

            using (var uso = new UsoDeEstilo(galeria, estilo))
            {
                var simbolos = new JArray();
                var porClase = new JObject();
                int total = 0;
                foreach (ClaseGaleria k in clases)
                {
                    int enClase = 0;
                    foreach (IStyleGalleryItem it in Items(galeria, k.Nombre, uso.Ruta))
                    {
                        string nombre = it.Name ?? "";
                        string categoria = it.Category ?? "";
                        if (filtro != null && !filtro.IsMatch(nombre) && !filtro.IsMatch(categoria))
                            continue;
                        enClase++;
                        total++;
                        if (simbolos.Count < limite)
                            simbolos.Add(new JObject
                            {
                                ["nombre"] = nombre,
                                ["categoria"] = categoria,
                                // El nombre NO es único ni con su categoría: ESRI.style trae
                                // dos «Verde» en «Predeterminado». El id sí.
                                ["id"] = it.ID,
                                ["clase"] = k.Nombre,
                                ["tipo_simbolo"] = TipoSimbolo(it.Item as ISymbol)
                            });
                    }
                    porClase[k.Nombre] = enClase;
                }
                return Protocol.Result(new JObject
                {
                    ["estilo"] = uso.RutaCompleta,
                    ["estaba_cargado"] = uso.EstabaCargado,
                    ["patron"] = patron,
                    ["por_clase"] = porClase,
                    ["total"] = total,
                    ["devueltos"] = simbolos.Count,
                    ["truncado"] = total > simbolos.Count,
                    ["simbolos"] = simbolos
                });
            }
        }

        public static JObject ApplyStyleSymbol(JObject parameters)
        {
            IMxDocument doc;
            IMap map;
            IGeoFeatureLayer gfl = SymbolHandlers.CapaDeEntidades(parameters, out doc, out map);
            esriGeometryType shp = SymbolHandlers.Geometria(gfl);

            string estilo = Texto(parameters["estilo"]);
            if (estilo == null)
                throw new ArgumentException("Indica 'estilo': ruta a un .style o nombre de uno cargado en ArcMap"
                    + " (list_style_symbols sin parámetros los lista).");
            string nombre = Texto(parameters["nombre_simbolo"]);
            int? id = Parametros.Dado(parameters["id_simbolo"])
                ? Parametros.LeerEntero(parameters["id_simbolo"], "id_simbolo", 0, 0, int.MaxValue) : (int?)null;
            if (nombre == null && id == null)
                throw new ArgumentException("Indica 'nombre_simbolo' o 'id_simbolo' (list_style_symbols da los dos).");
            string categoria = Texto(parameters["categoria_estilo"]);
            string etiqueta = Texto(parameters["etiqueta"]);

            IStyleGallery galeria = doc.StyleGallery;
            ClaseGaleria clase = ClasesPedidas(galeria, Texto(parameters["clase"]), shp)[0];

            ISymbol simbolo;
            JObject origen;
            using (var uso = new UsoDeEstilo(galeria, estilo))
            {
                var exactos = new List<IStyleGalleryItem>();
                var parecidos = new List<string>();
                foreach (IStyleGalleryItem it in Items(galeria, clase.Nombre, uso.Ruta))
                {
                    string n = it.Name ?? "";
                    bool casaNombre = nombre == null || string.Equals(n, nombre, StringComparison.OrdinalIgnoreCase);
                    bool casaCategoria = categoria == null
                        || string.Equals(it.Category ?? "", categoria, StringComparison.OrdinalIgnoreCase);
                    bool casaId = id == null || it.ID == id.Value;
                    if (casaNombre && casaCategoria && casaId)
                        exactos.Add(it);
                    else if (nombre != null && !casaNombre && parecidos.Count < 10
                             && n.IndexOf(nombre, StringComparison.OrdinalIgnoreCase) >= 0)
                        parecidos.Add(n);
                }
                string pedido = (nombre != null ? "'" + nombre + "'" : "") + (id != null ? " con id " + id : "");
                if (exactos.Count == 0)
                    throw new ArgumentException("No hay ningún símbolo " + pedido
                        + (categoria != null ? " en la categoría '" + categoria + "'" : "")
                        + " en la clase '" + clase.Nombre + "' de " + uso.RutaCompleta + "."
                        + (parecidos.Count > 0 ? " Parecidos: " + string.Join(" | ", parecidos) + "." : "")
                        + " Busca con list_style_symbols(estilo, patron=...).");
                if (exactos.Count > 1)
                    throw new ArgumentException("Hay " + exactos.Count + " símbolos " + pedido + " en " + uso.RutaCompleta
                        + ": " + string.Join(" | ", exactos.ConvertAll(i => "id " + i.ID + " (categoría '" + (i.Category ?? "") + "')"))
                        + ". Indica 'id_simbolo' (o 'categoria_estilo' si difieren en categoría).");

                IStyleGalleryItem elegido = exactos[0];
                nombre = elegido.Name; // con solo id_simbolo, para los mensajes de abajo
                ISymbol delEstilo = elegido.Item as ISymbol;
                if (delEstilo == null)
                    throw new ArgumentException("'" + nombre + "' no es un símbolo.");
                // Copia independiente: el objeto de la galería deja de ser válido
                // cuando el .style se quita, y no se comparte con la capa.
                simbolo = (ISymbol)((IClone)delEstilo).Clone();
                origen = new JObject
                {
                    ["estilo"] = uso.RutaCompleta,
                    ["estaba_cargado"] = uso.EstabaCargado,
                    ["nombre"] = elegido.Name,
                    ["categoria"] = elegido.Category,
                    ["id"] = elegido.ID,
                    ["clase"] = clase.Nombre
                };
            }

            ComprobarGeometria(simbolo, shp, nombre);
            ISimpleRenderer render = new SimpleRendererClass { Symbol = simbolo };
            if (etiqueta != null)
                render.Label = etiqueta;
            gfl.Renderer = (IFeatureRenderer)render;
            MapHandlers.NotificarCambioContenido(map, doc);

            JObject r = SymbolHandlers.Estado(gfl, null);
            r["simbolo_de_estilo"] = origen;
            return Protocol.Result(r);
        }

        // ------------------------------------------------------------------ //

        /// <summary>Resuelve el .style de la llamada y, si no estaba en la galería, lo
        /// añade y lo quita al salir (también si la llamada falla).</summary>
        private sealed class UsoDeEstilo : IDisposable
        {
            private readonly IStyleGalleryStorage _almacen;
            /// <summary>La ruta tal como la guarda la galería: es la clave de get_Items
            /// y RemoveFile. Los estilos de la instalación van RELATIVOS
            /// ("ESRI.style", a DefaultStylePath).</summary>
            public readonly string Ruta;
            /// <summary>La misma, absoluta: la que se devuelve.</summary>
            public readonly string RutaCompleta;
            public readonly bool EstabaCargado;

            public UsoDeEstilo(IStyleGallery galeria, string estilo)
            {
                _almacen = (IStyleGalleryStorage)galeria;
                List<string> cargados = Ficheros(_almacen);
                bool esRuta = Path.IsPathRooted(estilo)
                    || estilo.EndsWith(".style", StringComparison.OrdinalIgnoreCase);

                if (!esRuta)
                {
                    string hallado = cargados.Find(f => string.Equals(Path.GetFileNameWithoutExtension(f), estilo,
                        StringComparison.OrdinalIgnoreCase));
                    if (hallado == null)
                        throw new ArgumentException("No hay ningún estilo cargado llamado '" + estilo + "'. Cargados: "
                            + string.Join(" | ", cargados.ConvertAll(Path.GetFileNameWithoutExtension))
                            + ". Para uno que no esté cargado, pasa la ruta completa del .style.");
                    Ruta = hallado;
                    RutaCompleta = Absoluta(_almacen, hallado);
                    EstabaCargado = true;
                    return;
                }

                if (!Path.IsPathRooted(estilo))
                    throw new ArgumentException("'estilo' debe ser una ruta ABSOLUTA al .style, o el nombre de un"
                        + " estilo cargado. Recibido: " + estilo);
                // GetFullPath cambia también / por \: AddFile con barras normales falla
                // con «Error no especificado» (medido el 2026-09-25).
                string completa = Path.GetFullPath(estilo);
                if (!File.Exists(completa))
                    throw new ArgumentException("No existe el estilo: " + completa);
                string ya = cargados.Find(f => string.Equals(Absoluta(_almacen, f), completa, StringComparison.OrdinalIgnoreCase));
                if (ya != null)
                {
                    Ruta = ya;
                    RutaCompleta = completa;
                    EstabaCargado = true;
                    return;
                }
                _almacen.AddFile(completa);
                Ruta = completa;
                RutaCompleta = completa;
                EstabaCargado = false;
            }

            public void Dispose()
            {
                if (!EstabaCargado)
                {
                    try { _almacen.RemoveFile(Ruta); }
                    catch (Exception ex) { Log.Warn("StyleHandlers: no se pudo quitar " + Ruta + " de la galería: " + ex.Message); }
                }
            }
        }

        private static JObject EstilosCargados(IStyleGallery galeria)
        {
            IStyleGalleryStorage almacen = (IStyleGalleryStorage)galeria;
            var estilos = new JArray();
            foreach (string f in Ficheros(almacen))
                estilos.Add(new JObject { ["nombre"] = Path.GetFileNameWithoutExtension(f), ["ruta"] = Absoluta(almacen, f) });
            var clases = new JArray();
            foreach (ClaseGaleria k in ClasesDeSimbolo(galeria))
                clases.Add(new JObject { ["nombre"] = k.Nombre, ["tipo"] = k.Tipo });
            return new JObject
            {
                ["estilos_cargados"] = estilos,
                ["estilo_por_defecto"] = SinFallo(() => almacen.DefaultStylePath),
                ["clases_de_simbolo"] = clases,
                ["nota"] = "Pasa 'estilo' (nombre de uno cargado o ruta a un .style) para ver sus símbolos."
            };
        }

        private static List<string> Ficheros(IStyleGalleryStorage almacen)
        {
            var salida = new List<string>();
            for (int i = 0; i < almacen.FileCount; i++)
                salida.Add(almacen.get_File(i));
            return salida;
        }

        private static string Absoluta(IStyleGalleryStorage almacen, string ruta)
        {
            try
            {
                if (!Path.IsPathRooted(ruta))
                    ruta = Path.Combine(almacen.DefaultStylePath, ruta);
                return Path.GetFullPath(ruta);
            }
            catch (Exception) { return ruta; }
        }

        // ------------------------------------------------------------------ //

        private sealed class ClaseGaleria
        {
            public string Nombre;
            public string Tipo; // relleno | linea | marcador
        }

        private static readonly Dictionary<string, string> Alias = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            { "relleno", "relleno" }, { "rellenos", "relleno" }, { "fill", "relleno" }, { "fill symbols", "relleno" },
            { "símbolos de relleno", "relleno" }, { "simbolos de relleno", "relleno" }, { "poligono", "relleno" }, { "polígono", "relleno" },
            { "linea", "linea" }, { "línea", "linea" }, { "lineas", "linea" }, { "líneas", "linea" }, { "line", "linea" },
            { "line symbols", "linea" }, { "símbolos de línea", "linea" }, { "simbolos de linea", "linea" },
            { "marcador", "marcador" }, { "marcadores", "marcador" }, { "punto", "marcador" }, { "puntos", "marcador" },
            { "marker", "marcador" }, { "marker symbols", "marcador" }, { "símbolos de marcador", "marcador" },
            { "simbolos de marcador", "marcador" }
        };

        /// <summary>Las clases de símbolo de la galería (relleno, línea, marcador), por
        /// lo que crean. Si una no se reconoce se cae a su nombre en inglés.</summary>
        private static List<ClaseGaleria> ClasesDeSimbolo(IStyleGallery galeria)
        {
            var salida = new List<ClaseGaleria>();
            for (int i = 0; i < galeria.ClassCount; i++)
            {
                IStyleGalleryClass k = galeria.get_Class(i);
                string tipo = TipoDeClase(k);
                if (tipo != null && !salida.Exists(c => c.Tipo == tipo))
                    salida.Add(new ClaseGaleria { Nombre = k.Name, Tipo = tipo });
            }
            return salida;
        }

        private static string TipoDeClase(IStyleGalleryClass k)
        {
            string nombre = k.Name ?? "";
            if (string.Equals(nombre, "Fill Symbols", StringComparison.OrdinalIgnoreCase)) return "relleno";
            if (string.Equals(nombre, "Line Symbols", StringComparison.OrdinalIgnoreCase)) return "linea";
            if (string.Equals(nombre, "Marker Symbols", StringComparison.OrdinalIgnoreCase)) return "marcador";
            try
            {
                IEnumBSTR tipos = k.NewObjectTypes;
                tipos.Reset();
                string t = tipos.Next();
                if (string.IsNullOrEmpty(t))
                    return null;
                object o = k.get_NewObject(t);
                // Orden importante: ningún relleno es línea, pero se mira primero por claridad.
                if (o is IFillSymbol) return "relleno";
                if (o is ILineSymbol) return "linea";
                if (o is IMarkerSymbol) return "marcador";
            }
            catch (Exception) { }
            return null;
        }

        /// <summary>
        /// Clases que pide la llamada. `clase` null: en la lista, las tres de símbolo;
        /// en apply (geometría dada), la que toca a la capa. Si no, alias (relleno,
        /// linea, marcador, «Fill Symbols»...) o el nombre exacto de una clase.
        /// </summary>
        private static List<ClaseGaleria> ClasesPedidas(IStyleGallery galeria, string clase, esriGeometryType? shp)
        {
            List<ClaseGaleria> simbolo = ClasesDeSimbolo(galeria);
            string tipo = null;
            if (clase == null)
            {
                if (shp == null)
                {
                    if (simbolo.Count == 0)
                        throw new InvalidOperationException("La galería de estilos no tiene clases de símbolo reconocibles.");
                    return simbolo;
                }
                tipo = shp == esriGeometryType.esriGeometryPolygon ? "relleno"
                     : shp == esriGeometryType.esriGeometryPolyline ? "linea" : "marcador";
            }
            else
            {
                for (int i = 0; i < galeria.ClassCount; i++)
                {
                    IStyleGalleryClass k = galeria.get_Class(i);
                    if (string.Equals(k.Name, clase, StringComparison.OrdinalIgnoreCase))
                        return new List<ClaseGaleria> { new ClaseGaleria { Nombre = k.Name, Tipo = TipoDeClase(k) } };
                }
                if (!Alias.TryGetValue(clase.Trim(), out tipo))
                    throw new ArgumentException("Clase '" + clase + "' desconocida. Usa relleno, linea o marcador (o el"
                        + " nombre de una clase de la galería: " + string.Join(" | ", simbolo.ConvertAll(c => c.Nombre)) + ").");
            }
            ClaseGaleria hallada = simbolo.Find(c => c.Tipo == tipo);
            if (hallada == null)
                throw new InvalidOperationException("La galería no tiene clase de símbolos de tipo '" + tipo + "'.");
            return new List<ClaseGaleria> { hallada };
        }

        private static IEnumerable<IStyleGalleryItem> Items(IStyleGallery galeria, string clase, string ruta)
        {
            IEnumStyleGalleryItem en = galeria.get_Items(clase, ruta, "");
            en.Reset();
            IStyleGalleryItem it;
            while ((it = en.Next()) != null)
                yield return it;
        }

        /// <summary>Con * o ? es comodín sobre el texto entero; sin ellos, «contiene».
        /// Sin distinguir mayúsculas.</summary>
        private static Regex Filtro(string patron)
        {
            if (patron == null)
                return null;
            string re = patron.IndexOf('*') >= 0 || patron.IndexOf('?') >= 0
                ? "^" + Regex.Escape(patron).Replace(@"\*", ".*").Replace(@"\?", ".") + "$"
                : Regex.Escape(patron);
            return new Regex(re, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
        }

        private static void ComprobarGeometria(ISymbol sym, esriGeometryType shp, string nombre)
        {
            bool vale = shp == esriGeometryType.esriGeometryPolygon ? sym is IFillSymbol
                      : shp == esriGeometryType.esriGeometryPolyline ? sym is ILineSymbol
                      : sym is IMarkerSymbol;
            if (!vale)
                throw new ArgumentException("'" + nombre + "' es un símbolo de " + TipoSimbolo(sym)
                    + " y la capa es de " + (shp == esriGeometryType.esriGeometryPolygon ? "polígonos"
                        : shp == esriGeometryType.esriGeometryPolyline ? "líneas" : "puntos") + ".");
        }

        private static string TipoSimbolo(ISymbol sym)
        {
            if (sym is IFillSymbol) return "relleno";
            if (sym is ILineSymbol) return "linea";
            if (sym is IMarkerSymbol) return "marcador";
            return sym == null ? null : "otro";
        }

        private static string Texto(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            string s = ((string)t).Trim();
            return s.Length == 0 ? null : s;
        }

        private static string SinFallo(Func<string> f)
        {
            try { return f(); }
            catch (Exception) { return null; }
        }
    }
}
