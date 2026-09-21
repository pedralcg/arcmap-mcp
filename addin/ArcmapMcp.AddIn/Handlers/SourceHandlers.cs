using System;
using System.Collections.Generic;
using System.Text.RegularExpressions;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.esriSystem;
using ESRI.ArcGIS.Framework;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// list_broken_data_sources / repair_data_source — contrato JSON que esperan
    /// los schemas del servidor MCP. Equivalen al ListBrokenDataSources/
    /// findAndReplaceWorkspacePath de arcpy; aquí: ILayer2.Valid +
    /// IStandaloneTable.Valid para detectar, y un clon del IDatasetName
    /// reapuntado + IDataLayer.Connect para reparar.
    /// </summary>
    internal static class SourceHandlers
    {
        public static JObject ListBrokenDataSources(JObject parameters)
        {
            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            IMaps maps = doc.Maps;

            var rotos = new JArray();
            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);

                foreach (MapHandlers.CapaEnMapa c in MapHandlers.CapasConRuta(m))
                {
                    ILayer lyr = c.Capa;
                    if (lyr is IGroupLayer)
                        continue; // los grupos no tienen fuente propia
                    bool valido = true;
                    try
                    {
                        ILayer2 l2 = lyr as ILayer2;
                        valido = l2 == null || l2.Valid;
                    }
                    catch { valido = false; }
                    if (valido)
                        continue;

                    string workspace;
                    string fuente = DataAccess.RutaFuente(lyr, out workspace);
                    rotos.Add(new JObject
                    {
                        ["nombre"] = NombreSeguro(lyr),
                        // El listado recorre TODOS los data frames, así que sin decir
                        // en cuál está cada capa, repair_data_source no sabe dónde
                        // buscarla. 'ruta' la desambigua si el nombre se repite.
                        ["data_frame"] = NombreDeMapa(m),
                        ["ruta"] = c.Ruta,
                        ["fuente_rota"] = fuente,
                        ["workspace"] = workspace
                    });
                }

                // Tablas independientes del data frame, como ListBrokenDataSources.
                IStandaloneTableCollection tablas = m as IStandaloneTableCollection;
                if (tablas == null)
                    continue;
                for (int j = 0; j < tablas.StandaloneTableCount; j++)
                {
                    IStandaloneTable st = tablas.get_StandaloneTable(j);
                    bool valido = true;
                    try { valido = st.Valid; } catch { valido = false; }
                    if (valido)
                        continue;
                    var item = new JObject
                    {
                        ["nombre"] = "(tabla sin nombre)",
                        ["data_frame"] = NombreDeMapa(m),
                        ["ruta"] = null,
                        ["fuente_rota"] = null,
                        ["workspace"] = null
                    };
                    try { item["nombre"] = st.Name; } catch { }
                    try
                    {
                        IDatasetName dsn = (st as IDataLayer) != null
                            ? ((IDataLayer)st).DataSourceName as IDatasetName : null;
                        if (dsn != null)
                        {
                            string ws = dsn.WorkspaceName != null ? dsn.WorkspaceName.PathName : null;
                            item["workspace"] = ws;
                            item["fuente_rota"] = ws != null
                                ? System.IO.Path.Combine(ws, dsn.Name) : dsn.Name;
                        }
                    }
                    catch { }
                    rotos.Add(item);
                }
            }

            return Protocol.Result(new JObject
            {
                ["num"] = rotos.Count,
                ["rotos"] = rotos
            });
        }

        private static string NombreSeguro(ILayer lyr)
        {
            try { return lyr.Name; } catch { return "(sin nombre)"; }
        }

        private static string NombreDeMapa(IMap m)
        {
            try { return m.Name; } catch { return "(data frame sin nombre)"; }
        }

        /// <summary>
        /// Busca la capa en TODOS los data frames, o solo en el que diga
        /// 'data_frame'. list_broken_data_sources recorre el documento entero, así
        /// que limitar la reparación al data frame activo dejaba sin arreglo
        /// precisamente la mitad de lo que el listado enseñaba.
        /// </summary>
        private static ILayer BuscarCapa(IMxDocument doc, string capa, string dataFrame, out IMap mapa)
        {
            mapa = null;
            IMaps maps = doc.Maps;
            var candidatas = new List<string>();
            var todas = new List<string>();
            ILayer encontrada = null;
            bool dfVisto = false;
            var nombresDf = new List<string>();

            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);
                string nombreDf = NombreDeMapa(m);
                nombresDf.Add(nombreDf);
                if (!string.IsNullOrEmpty(dataFrame)
                    && !string.Equals(nombreDf, dataFrame, StringComparison.OrdinalIgnoreCase))
                    continue;
                dfVisto = true;

                List<MapHandlers.CapaEnMapa> capas = MapHandlers.CapasConRuta(m);
                foreach (MapHandlers.CapaEnMapa c in capas)
                    todas.Add(nombreDf + "/" + c.Ruta);
                foreach (MapHandlers.CapaEnMapa c in MapHandlers.Coincidencias(capas, capa))
                {
                    candidatas.Add(nombreDf + "/" + c.Ruta);
                    if (encontrada == null)
                    {
                        encontrada = c.Capa;
                        mapa = m;
                    }
                }
            }

            if (!string.IsNullOrEmpty(dataFrame) && !dfVisto)
                throw new ArgumentException("Data frame no encontrado: " + dataFrame
                    + ". Disponibles: " + string.Join(", ", nombresDf));
            if (candidatas.Count == 0)
                throw new ArgumentException("Capa no encontrada: " + capa
                    + ". Disponibles: " + string.Join(", ", todas));
            if (candidatas.Count > 1)
            {
                mapa = null;
                throw new ArgumentException("Hay " + candidatas.Count + " capas que casan con '" + capa
                    + "'. Indica 'data_frame' o la ruta de grupo completa: " + string.Join(", ", candidatas));
            }
            return encontrada;
        }

        public static JObject RepairDataSource(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string rutaAntigua = (string)parameters["ruta_antigua"];
            string rutaNueva = (string)parameters["ruta_nueva"];
            string dataFrame = (string)parameters["data_frame"];
            bool validar = Parametros.LeerBool(parameters["validar"], "validar", true);
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(rutaAntigua) || string.IsNullOrEmpty(rutaNueva))
                throw new ArgumentException("Indica 'capa', 'ruta_antigua' y 'ruta_nueva'.");

            IApplication appRep = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(appRep);
            IMap map;
            ILayer lyr = BuscarCapa(doc, capa, dataFrame, out map);
            IDataLayer dl = lyr as IDataLayer;
            if (dl == null)
                throw new ArgumentException("La capa no tiene fuente de datos reapuntable: " + capa);

            bool rotoAntes = EstaRota(lyr);

            IDatasetName dsn = dl.DataSourceName as IDatasetName;
            if (dsn == null || dsn.WorkspaceName == null)
                throw new InvalidOperationException("No se puede resolver la fuente actual de la capa: " + capa);
            string wsActual = dsn.WorkspaceName.PathName ?? "";
            // Sustitución del fragmento de ruta, insensible a mayúsculas — misma
            // semántica que el findAndReplaceWorkspacePath de arcpy.
            string wsNuevo = Regex.Replace(wsActual, Regex.Escape(rutaAntigua),
                rutaNueva.Replace("$", "$$"), RegexOptions.IgnoreCase);

            bool aplicado = true;
            string aviso = null;
            if (string.Equals(wsNuevo, wsActual, StringComparison.OrdinalIgnoreCase))
            {
                aplicado = false;
                aviso = "La 'ruta_antigua' no aparece en el workspace actual de la capa ("
                        + wsActual + "); no hay nada que sustituir.";
            }
            else
            {
                // Clonar el dataset name y reapuntarlo, sin tocar la capa hasta validar.
                IObjectCopy copia = new ObjectCopyClass();
                IDatasetName nuevoDsn = (IDatasetName)copia.Copy(dsn);
                nuevoDsn.WorkspaceName.PathName = wsNuevo;

                if (validar)
                {
                    try
                    {
                        ((IName)nuevoDsn).Open();
                    }
                    catch (Exception e)
                    {
                        // Respuesta limpia cuando la ruta nueva no contiene el
                        // dataset: aplicado:false + aviso, no excepción.
                        aplicado = false;
                        aviso = "Ruta nueva no valida (el dataset no existe alli); con validar=True "
                                + "no se aplico el cambio. Detalle: " + e.Message;
                    }
                }
                if (aplicado)
                {
                    // Connect(repairName) IGNORA el repair name mientras el
                    // DataSourceName actual siga siendo abrible (incluso tras
                    // Disconnect): para un reapunte incondicional como el del
                    // findAndReplaceWorkspacePath de arcpy hay que ESCRIBIR
                    // DataSourceName y reconectar.
                    IDataLayer2 dl2 = lyr as IDataLayer2;
                    bool conectado;
                    try
                    {
                        // Disconnect lanza E_FAIL si la capa ya está desconectada
                        // (rota); es indiferente: solo se busca soltar la conexión.
                        try { if (dl2 != null) dl2.Disconnect(); } catch { }
                        dl.DataSourceName = (IName)nuevoDsn;
                        conectado = dl.Connect((IName)nuevoDsn);
                    }
                    catch (Exception e)
                    {
                        if (validar)
                            throw;
                        conectado = false;
                        aviso = "Connect falló: " + e.Message;
                    }
                    // No fiarse del bool de Connect: verificar el reapunte
                    // re-leyendo la fuente (así se cazó este bug en la prueba).
                    try
                    {
                        IDatasetName dsnFinal = dl.DataSourceName as IDatasetName;
                        string wsFinal = dsnFinal != null && dsnFinal.WorkspaceName != null
                            ? dsnFinal.WorkspaceName.PathName : null;
                        if (conectado && !string.Equals(wsFinal, wsNuevo, StringComparison.OrdinalIgnoreCase))
                        {
                            conectado = false;
                            aviso = "El reapunte no se aplicó (workspace final: " + wsFinal + ").";
                        }
                    }
                    catch { }
                    if (!conectado && aviso == null)
                        aviso = "ArcObjects no pudo reapuntar la capa a " + wsNuevo + " (Connect devolvió false).";
                    aplicado = conectado;
                }
            }

            bool rotoDespues = EstaRota(lyr);
            // Como el resto de mutadoras: ContentsChanged avisa a los map surrounds.
            // Sin él, una leyenda se quedaba con el estado anterior a la reparación y
            // el export salía con la capa todavía rota.
            MapHandlers.NotificarCambioContenido(map, doc);

            var salida = new JObject
            {
                ["capa"] = capa,
                ["data_frame"] = NombreDeMapa(map),
                ["roto_antes"] = rotoAntes,
                ["roto_despues"] = rotoDespues,
                ["ruta_antigua"] = rutaAntigua,
                ["ruta_nueva"] = rutaNueva,
                ["validado"] = validar,
                ["aplicado"] = aplicado
            };
            if (aviso != null)
                salida["aviso"] = aviso;
            return Protocol.Result(salida);
        }

        private static bool EstaRota(ILayer lyr)
        {
            try
            {
                ILayer2 l2 = lyr as ILayer2;
                return l2 != null && !l2.Valid;
            }
            catch
            {
                return true;
            }
        }
    }
}
