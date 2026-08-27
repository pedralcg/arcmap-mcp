using System;
using System.Globalization;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.DataSourcesRaster;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Simbología de RÁSTER: clasificada y estirada.
    ///
    /// Por qué existe este fichero aparte: `set_graduated_symbology` exige un
    /// IGeoFeatureLayer (LayerHandlers.cs), así que hasta 2026-08-27 NO había
    /// forma de simbolizar un ráster desde el MCP. `execute_arcpy` tampoco valía:
    /// opera sobre una copia del documento y los cambios al renderer se descartan
    /// por diseño (ADR-004). Era el hueco más pegado a los flujos reales de IDEN,
    /// donde casi todo el producto es ráster: NDVI, FCC, P95, pendientes.
    ///
    /// La familia de interfaces es OTRA que en vectorial: aquí manda
    /// IRasterRenderer (+ IRasterClassifyColorRampRenderer o
    /// IRasterStretchColorRampRenderer), no IFeatureRenderer, y el orden de las
    /// llamadas importa — ver el comentario de Update() más abajo.
    /// </summary>
    internal static class RasterSymbologyHandlers
    {
        /// <summary>
        /// set_raster_symbology: aplica simbología a una capa ráster de la TOC.
        ///
        /// modo="clasificado" (por defecto): N clases con rampa de color.
        /// modo="estirado": rampa continua entre el mínimo y el máximo.
        /// </summary>
        public static JObject SetRasterSymbology(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre del ráster en la TOC).");

            string modo = ((string)parameters["modo"] ?? "clasificado").ToLowerInvariant();
            if (modo == "classified") modo = "clasificado";
            if (modo == "stretched") modo = "estirado";
            if (modo != "clasificado" && modo != "estirado")
                throw new ArgumentException("'modo' debe ser 'clasificado' o 'estirado' "
                    + "(alias: classified / stretched). Recibido: " + modo);

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer capaEncontrada = MapHandlers.FindLayer(map, capa);
            IRasterLayer rl = capaEncontrada as IRasterLayer;
            if (rl == null)
            {
                // Mensaje que REDIRIGE en vez de solo negar: el error mudo es lo que
                // costó una sesión entera de trabajo real.
                string queEs = capaEncontrada == null
                    ? "no existe ninguna capa con ese nombre en la TOC"
                    : (capaEncontrada is IGeoFeatureLayer
                        ? "es una capa de ENTIDADES; para eso usa set_graduated_symbology "
                          + "(rangos) o set_unique_values_symbology (categorías)"
                        : "no es una capa ráster");
                throw new ArgumentException("'" + capa + "': " + queEs + ".");
            }

            IRaster raster = rl.Raster;
            if (raster == null)
                throw new ArgumentException("La capa ráster '" + capa
                    + "' no tiene datos accesibles (¿fuente rota?). Revisa con list_broken_data_sources.");

            IColor desde = LeerColor(parameters["color_desde"], 255, 255, 178);
            IColor hasta = LeerColor(parameters["color_hasta"], 189, 0, 38);

            JObject detalle = modo == "clasificado"
                ? AplicarClasificado(rl, raster, parameters, desde, hasta)
                : AplicarEstirado(rl, raster, desde, hasta);

            MapHandlers.NotificarCambioContenido(map, doc);

            detalle["capa"] = rl.Name;
            detalle["modo"] = modo;
            return Protocol.Result(detalle);
        }

        private static JObject AplicarClasificado(IRasterLayer rl, IRaster raster,
            JObject parameters, IColor desde, IColor hasta)
        {
            int numClases = LeerInt(parameters["num_clases"], 5);
            if (numClases < 2 || numClases > 32)
                throw new ArgumentException("'num_clases' debe estar entre 2 y 32.");

            // Sin estadísticas no hay clasificación posible: el renderer necesita
            // saber mínimo, máximo e histograma. Muchos .tif llegan sin ellas (los
            // que salen de un geoproceso, típicamente), y el síntoma es un E_FAIL
            // mudo desde COM. Se calculan aquí en vez de mandar al usuario a
            // hacerlo a mano, que es lo que hacía la primera versión de esto.
            AsegurarEstadisticas(raster);

            IRasterClassifyColorRampRenderer clasificado = new RasterClassifyColorRampRendererClass();
            IRasterRenderer render = (IRasterRenderer)clasificado;

            // ORDEN OBLIGATORIO, y es la trampa de esta API: Raster → Update() →
            // ClassCount → Update(). El PRIMER Update() es el que hace que el
            // renderer lea el ráster y construya una clasificación por defecto; si
            // se pone ClassCount antes de eso, no hay nada sobre lo que reclasificar
            // y Update() revienta con E_FAIL.
            render.Raster = raster;
            try
            {
                render.Update();
                clasificado.ClassCount = numClases;
                render.Update();
            }
            catch (Exception ex)
            {
                throw new ArgumentException("No se pudo clasificar el ráster en " + numClases
                    + " clases: " + ex.Message
                    + ". Comprueba que la banda tiene valores variados y estadísticas válidas; "
                    + "un ráster constante o todo NoData no se puede clasificar.");
            }

            int nReal = clasificado.ClassCount;
            IAlgorithmicColorRamp rampa = new AlgorithmicColorRampClass
            {
                Algorithm = esriColorRampAlgorithm.esriHSVAlgorithm,
                FromColor = desde,
                ToColor = hasta,
                Size = nReal
            };
            bool rampaOk;
            rampa.CreateRamp(out rampaOk);
            if (!rampaOk)
                throw new ArgumentException("No se pudo crear la rampa de color con los colores dados.");

            IEnumColors colores = rampa.Colors;
            colores.Reset();

            var cortes = new JArray();
            for (int i = 0; i < nReal; i++)
            {
                IColor color = colores.Next() ?? hasta;
                ISimpleFillSymbol relleno = new SimpleFillSymbolClass
                {
                    Color = color,
                    Style = esriSimpleFillStyle.esriSFSSolid
                };
                // Sin borde: en un ráster el contorno de cada clase es ruido visual.
                ISimpleLineSymbol sinBorde = new SimpleLineSymbolClass
                {
                    Style = esriSimpleLineStyle.esriSLSNull
                };
                relleno.Outline = sinBorde;

                clasificado.set_Symbol(i, (ISymbol)relleno);
                clasificado.set_Label(i, Etiqueta(clasificado.get_Break(i)));
                cortes.Add(clasificado.get_Break(i));
            }

            // Segundo Update(): consolida símbolos y etiquetas en la leyenda.
            render.Update();
            rl.Renderer = render;

            return new JObject
            {
                ["num_clases"] = nReal,
                ["cortes"] = cortes,
            };
        }

        private static JObject AplicarEstirado(IRasterLayer rl, IRaster raster,
            IColor desde, IColor hasta)
        {
            IRasterStretchColorRampRenderer estirado = new RasterStretchColorRampRendererClass();
            IRasterRenderer render = (IRasterRenderer)estirado;

            render.Raster = raster;

            IAlgorithmicColorRamp rampa = new AlgorithmicColorRampClass
            {
                Algorithm = esriColorRampAlgorithm.esriHSVAlgorithm,
                FromColor = desde,
                ToColor = hasta,
                Size = 255
            };
            bool rampaOk;
            rampa.CreateRamp(out rampaOk);
            if (!rampaOk)
                throw new ArgumentException("No se pudo crear la rampa de color con los colores dados.");

            estirado.BandIndex = 0;
            estirado.ColorRamp = rampa;
            try
            {
                render.Update();
            }
            catch (Exception ex)
            {
                throw new ArgumentException("No se pudo aplicar el estirado: " + ex.Message
                    + ". Suele ser que el ráster no tiene estadísticas calculadas.");
            }
            rl.Renderer = render;

            return new JObject
            {
                ["banda"] = 0,
            };
        }

        /// <summary>
        /// Calcula estadísticas de las bandas si faltan. Sin ellas, el renderer
        /// clasificado falla con un E_FAIL de COM que no explica nada.
        /// </summary>
        private static void AsegurarEstadisticas(IRaster raster)
        {
            IRasterBandCollection bandas = raster as IRasterBandCollection;
            if (bandas == null)
                return;
            for (int i = 0; i < bandas.Count; i++)
            {
                IRasterBand banda = bandas.Item(i);
                bool tiene;
                banda.HasStatistics(out tiene);
                if (tiene)
                    continue;
                try
                {
                    banda.ComputeStatsAndHist();
                    Log.Info("Estadísticas calculadas para la banda " + i + " del ráster.");
                }
                catch (Exception ex)
                {
                    // No es fatal: puede ser una fuente de solo lectura. Se deja que
                    // falle después con su mensaje, pero queda anotado el motivo.
                    Log.Error("No se pudieron calcular estadísticas de la banda " + i, ex);
                }
            }
        }

        private static string Etiqueta(double valor)
        {
            return valor.ToString(Math.Abs(valor) >= 100 ? "0.#" : "0.###",
                CultureInfo.InvariantCulture);
        }

        private static IColor LeerColor(JToken t, int rDef, int gDef, int bDef)
        {
            int r = rDef, g = gDef, b = bDef;
            JArray arr = t as JArray;
            if (arr != null && arr.Count >= 3)
            {
                r = (int)arr[0];
                g = (int)arr[1];
                b = (int)arr[2];
            }
            if (r < 0 || r > 255 || g < 0 || g > 255 || b < 0 || b > 255)
                throw new ArgumentException("Los colores van como [R, G, B] con valores 0-255.");
            return new RgbColorClass { Red = r, Green = g, Blue = b };
        }

        private static int LeerInt(JToken t, int porDefecto)
        {
            return t == null || t.Type == JTokenType.Null ? porDefecto : (int)t;
        }
    }
}
