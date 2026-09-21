using System;
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
            app.SaveDocument(ruta);
            return Protocol.Result(new JObject { ["guardado"] = true, ["ruta"] = ruta });
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
            app.SaveAsDocument(salida, true); // true = copia: el doc activo no cambia
            return Protocol.Result(new JObject
            {
                ["guardado"] = true,
                ["salida"] = salida,
                ["origen"] = origen,
                ["sobrescrito"] = sobrescrito
            });
        }
    }
}
