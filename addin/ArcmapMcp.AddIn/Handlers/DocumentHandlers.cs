using System;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Framework;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// save_mxd / save_mxd_as — contrato JSON que esperan los schemas del servidor MCP.
    /// IApplication.SaveDocument(ruta) guarda en sitio; SaveAsDocument(ruta, true)
    /// equivale al mxd.saveACopy de arcpy (NO cambia el documento activo ni su ruta).
    /// </summary>
    internal static class DocumentHandlers
    {
        public static JObject SaveMxd(JObject parameters)
        {
            IApplication app = ArcSession.App();
            string ruta = ArcSession.MxdPath(app);
            if (string.IsNullOrEmpty(ruta) || !ruta.ToLowerInvariant().EndsWith(".mxd"))
                throw new InvalidOperationException(
                    "El documento no tiene ruta .mxd (¿nunca guardado?). Usa save_mxd_as.");
            bool verificar = Parametros.LeerBool(parameters["verificar"], "verificar", false);
            IMxDocument doc = ArcSession.Doc(app);
            JObject r = new JObject { ["guardado"] = true, ["ruta"] = ruta };
            // Antes de guardar: lo que se va a perder y lo que ya estaba roto (para
            // que quien reabra sepa qué rotas son NUEVAS).
            int enRiesgo = Prediccion(doc, ruta, r);
            if (verificar || enRiesgo > 0)
                r["rotas_en_memoria"] = RotasEnMemoria(doc);
            app.SaveDocument(ruta);
            return Protocol.Result(r);
        }

        /// <summary>Añade a la respuesta las capas que se romperán al guardar en
        /// `destino` (rutas relativas + MAX_PATH, ver RutasRelativas). Devuelve cuántas.</summary>
        private static int Prediccion(IMxDocument doc, string destino, JObject r)
        {
            try
            {
                bool relativas = doc.RelativePaths;
                r["rutas_relativas"] = relativas;
                if (!relativas)
                    return 0;
                JArray capas = RutasRelativas.CapasEnRiesgo(doc, destino);
                if (capas.Count == 0)
                    return 0;
                r["capas_perderan_ruta"] = capas;
                r["aviso_rutas_relativas"] = RutasRelativas.Aviso(capas.Count, destino);
                return capas.Count;
            }
            catch (Exception ex)
            {
                Log.Warn("save: no se pudo predecir las rutas relativas: " + ex.Message);
                return 0;
            }
        }

        private static JArray RotasEnMemoria(IMxDocument doc)
        {
            var salida = new JArray();
            IMaps maps = doc.Maps;
            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);
                foreach (MapHandlers.CapaEnMapa c in MapHandlers.CapasConRuta(m))
                {
                    if (c.Capa is IGroupLayer)
                        continue;
                    bool rota;
                    try
                    {
                        ILayer2 l2 = c.Capa as ILayer2;
                        rota = l2 != null && !l2.Valid;
                    }
                    catch { rota = true; }
                    if (rota)
                        salida.Add(new JObject { ["data_frame"] = m.Name, ["ruta"] = c.Ruta });
                }
            }
            return salida;
        }

        /// <summary>
        /// save_mxd_as: copia del documento en otra ruta.
        ///
        /// `sobrescribir` va a FALSE por defecto, al revés que en los export: aquí no
        /// hay ninguna serie que reexportar encima, y pisar un .mxd ajeno sin avisar
        /// se lleva por delante un montaje de horas. Se valida además que la carpeta
        /// exista y que la ruta sea absoluta, como ya hacía el hermano RutaSalida de
        /// los export: sin eso, SaveAsDocument escribía donde le pillara el
        /// directorio de trabajo de ArcMap.
        /// </summary>
        public static JObject SaveMxdAs(JObject parameters)
        {
            string salida = Parametros.RutaDeSalida(parameters["salida"], "salida", new[] { ".mxd" });
            bool sobrescribir = Parametros.LeerBool(parameters["sobrescribir"], "sobrescribir", false);
            bool sobrescrito = Parametros.ComprobarSobrescritura(salida, sobrescribir, "sobrescribir");

            IApplication app = ArcSession.App();
            string origen = ArcSession.MxdPath(app);
            var r = new JObject
            {
                ["guardado"] = true,
                ["salida"] = salida,
                ["origen"] = origen,
                ["sobrescrito"] = sobrescrito
            };
            // La relativa se calcula contra el DESTINO: una copia más profunda puede
            // romper capas que en el original guardan bien.
            IMxDocument doc = ArcSession.Doc(app);
            bool verificar = Parametros.LeerBool(parameters["verificar"], "verificar", false);
            if (Prediccion(doc, salida, r) > 0 || verificar)
                r["rotas_en_memoria"] = RotasEnMemoria(doc);
            app.SaveAsDocument(salida, true); // true = copia: el doc activo no cambia
            return Protocol.Result(r);
        }
    }
}
