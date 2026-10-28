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
            AnotarMarcosOle(doc, r);
            app.SaveDocument(ruta);
            return Protocol.Result(r);
        }

        /// <summary>
        /// open_mxd — abre otro .mxd en ESTA sesión, sin cerrar ArcMap.
        ///
        /// El 2026-10-01 se abrieron 15 planos cerrando ArcMap, lanzándolo con el .mxd y
        /// arrancando el puente por script: ~45 s cada uno y una caída.
        ///
        /// Lo que no puede pasar es un DIÁLOGO: corre en el hilo de ArcMap, y uno modal
        /// («¿guardar los cambios?», «documento de una versión posterior») lo deja
        /// esperando a un clic que nadie va a dar, con el puente colgado. Por eso todo lo
        /// que lo sacaría se resuelve ANTES: los cambios pendientes se guardan o se
        /// descartan (elección obligatoria), y un .mxd de versión posterior no se abre.
        /// </summary>
        public static JObject OpenMxd(JObject parameters)
        {
            string ruta = (string)parameters["ruta"];
            if (string.IsNullOrEmpty(ruta) || !System.IO.Path.IsPathRooted(ruta)
                || !ruta.EndsWith(".mxd", StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("'ruta' debe ser la ruta ABSOLUTA de un .mxd. Recibido: " + ruta);
            if (!System.IO.File.Exists(ruta))
                throw new ArgumentException("No existe el fichero: " + ruta);
            // Sin valor por defecto que actúe: ArcMap no deja leer si el documento tiene
            // cambios (IDocumentDirty2 solo escribe, IPersistStream no está; probado en vivo
            // el 2026-10-01), y tanto guardar como descartar por defecto pueden costar trabajo.
            string cambios = ((string)parameters["cambios_sin_guardar"] ?? "").ToLowerInvariant();
            if (cambios != "guardar" && cambios != "descartar")
                throw new ArgumentException("Indica 'cambios_sin_guardar': 'guardar' (guarda el documento abierto"
                    + " en su ruta antes de cambiar) o 'descartar' (tira lo que no esté guardado). Es obligatorio:"
                    + " ArcMap no deja consultar si hay cambios pendientes, y si los hay los pregunta con un"
                    + " diálogo que deja el puente colgado.");

            string errVersion;
            string versionMxd = MxdVersion.Leer(ruta, out errVersion);
            string versionSesion = MxdVersion.VersionArcMapSesion();
            if (versionMxd != null && versionSesion != null
                && MxdVersion.Comparar(MxdVersion.ATupla(versionMxd), MxdVersion.ATupla(versionSesion)) > 0)
                throw new InvalidOperationException("El .mxd es de ArcMap " + versionMxd + " y esta sesión es "
                    + versionSesion + ": ArcMap no lo abre y lo diría con un diálogo que dejaría el puente"
                    + " esperando. Ábrelo con una versión " + versionMxd + " o superior.");

            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            string anterior = ArcSession.MxdPath(app);

            // El mismo documento: OpenDocument lo reabriría, pero desde fuera no hay forma de
            // saber cuándo ha terminado (el documento activo ya es ese desde antes), y
            // «descartar y volver a lo guardado» quedaría sin garantizar.
            if (anterior != null && string.Equals(System.IO.Path.GetFullPath(anterior),
                    System.IO.Path.GetFullPath(ruta), StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Ese .mxd ya es el documento abierto. Para guardarlo, save_mxd; para"
                    + " volver a lo guardado, abre otro documento con cambios_sin_guardar='descartar' y luego"
                    + " vuelve a abrir este.");

            // Una sesión del Editor con ediciones pendientes: SetClean y SaveDocument no las
            // resuelven y ArcMap las pregunta con un diálogo al cambiar de documento. Pararla
            // aquí escribiría (o tiraría) DATOS, que no es lo que decide cambios_sin_guardar.
            string edicion = SesionDeEdicion(app);
            if (edicion != null)
                throw new InvalidOperationException("Hay una sesión de edición abierta en ArcMap (" + edicion + "):"
                    + " al cambiar de documento preguntaría con un diálogo si guardar las ediciones, y el puente"
                    + " se quedaría esperando. Guarda o descarta las ediciones y para la edición en ArcMap"
                    + " (Editor > Detener edición), y repite.");
            string queSeHizo;
            if (cambios == "guardar")
            {
                if (string.IsNullOrEmpty(anterior) || !anterior.EndsWith(".mxd", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("El documento abierto nunca se ha guardado: no hay"
                        + " dónde guardarlo. Usa save_mxd_as, o cambios_sin_guardar='descartar'.");
                app.SaveDocument(anterior);
                queSeHizo = "guardado en " + anterior;
            }
            else
            {
                IDocumentDirty2 sucio = doc as IDocumentDirty2;
                if (sucio == null)
                    throw new InvalidOperationException("No se pueden descartar los cambios del documento"
                        + " (no expone IDocumentDirty2): guárdalo o ciérralo a mano.");
                sucio.SetClean();
                queSeHizo = "cambios sin guardar descartados (si los había)";
            }

            // OpenDocument NO abre aquí: deja la apertura en cola y ArcMap la hace cuando
            // este handler devuelve (medido en vivo: justo después, el documento activo
            // seguía siendo el anterior). Por eso no se comprueba nada del documento nuevo
            // aquí: lo hace el servidor MCP preguntando con get_arcmap_info hasta verlo.
            app.OpenDocument(ruta);

            return Protocol.Result(new JObject
            {
                ["solicitado"] = ruta,
                ["anterior"] = anterior,
                ["documento_anterior"] = queSeHizo,
                ["version_mxd"] = versionMxd
            });
        }

        /// <summary>null si no se está editando; si sí, el estado («editando», «con ediciones
        /// pendientes»). Sin extensión de edición cargada no hay sesión posible.</summary>
        private static string SesionDeEdicion(IApplication app)
        {
            ESRI.ArcGIS.Editor.IEditor editor = null;
            try { editor = app.FindExtensionByName("ESRI Object Editor") as ESRI.ArcGIS.Editor.IEditor; }
            catch { }
            if (editor == null || editor.EditState == ESRI.ArcGIS.Editor.esriEditState.esriStateNotEditing)
                return null;
            bool pendientes = false;
            try
            {
                ESRI.ArcGIS.Geodatabase.IWorkspaceEdit ws = editor.EditWorkspace as ESRI.ArcGIS.Geodatabase.IWorkspaceEdit;
                bool hay = false;
                if (ws != null) ws.HasEdits(ref hay);
                pendientes = hay;
            }
            catch { }
            return pendientes ? "con ediciones sin guardar" : "sin ediciones pendientes, pero abierta";
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
            AnotarMarcosOle(doc, r);
            app.SaveAsDocument(salida, true); // true = copia: el doc activo no cambia
            return Protocol.Result(r);
        }

        /// <summary>La verificación del guardado (la hace el servidor) reabre el .mxd con el
        /// arcpy de fuera, y con marcos OLE eso se cuelga mientras ArcMap está abierto (ver
        /// MarcosOle). Se avisa aquí para que el servidor no la lance. Best-effort: nunca
        /// impide guardar.</summary>
        private static void AnotarMarcosOle(IMxDocument doc, JObject r)
        {
            try
            {
                JArray m = MarcosOle.Buscar(doc);
                if (m.Count > 0)
                    r["marcos_ole"] = m;
            }
            catch (Exception ex)
            {
                Log.Info("No se pudieron contar los marcos OLE antes de guardar: " + ex.Message);
            }
        }
    }
}
