using System;
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

                IEnumLayer enumLayer = null;
                try { enumLayer = m.get_Layers(null, true); } catch { /* df vacío */ }
                if (enumLayer != null)
                {
                    enumLayer.Reset();
                    ILayer lyr;
                    while ((lyr = enumLayer.Next()) != null)
                    {
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
                            ["fuente_rota"] = fuente,
                            ["workspace"] = workspace
                        });
                    }
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

        public static JObject RepairDataSource(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string rutaAntigua = (string)parameters["ruta_antigua"];
            string rutaNueva = (string)parameters["ruta_nueva"];
            bool validar = parameters["validar"] == null || parameters["validar"].Type == JTokenType.Null
                ? true : (bool)parameters["validar"];
            if (string.IsNullOrEmpty(capa) || string.IsNullOrEmpty(rutaAntigua) || string.IsNullOrEmpty(rutaNueva))
                throw new ArgumentException("Indica 'capa', 'ruta_antigua' y 'ruta_nueva'.");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
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
            doc.UpdateContents();
            doc.ActiveView.Refresh();

            var salida = new JObject
            {
                ["capa"] = capa,
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
