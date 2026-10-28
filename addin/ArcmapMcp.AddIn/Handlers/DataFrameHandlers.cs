using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Framework;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// list_data_frames / set_active_df — contrato JSON que esperan los schemas
    /// del servidor MCP. Como el mxd.activeView de arcpy, activar un data frame
    /// conmuta a su vista de datos (IMxDocument.ActiveView tiene setter).
    /// </summary>
    internal static class DataFrameHandlers
    {
        public static JObject ListDataFrames(JObject parameters)
        {
            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            IMaps maps = doc.Maps;
            IMap mapaActivo = doc.FocusMap;
            string activo = mapaActivo != null ? mapaActivo.Name : null;

            var dataFrames = new JArray();
            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);
                double? escala = null;
                try { escala = m.MapScale; } catch { /* escala no definida */ }
                dataFrames.Add(new JObject
                {
                    ["nombre"] = m.Name,
                    ["escala"] = escala,
                    // Por REFERENCIA, no por nombre: nada impide dos data frames
                    // llamados "Layers" (es el nombre por defecto, así que pasa en
                    // cuanto alguien añade uno), y entonces salían los dos marcados
                    // como activo.
                    ["activo"] = ReferenceEquals(m, mapaActivo)
                });
            }
            return Protocol.Result(new JObject
            {
                ["num"] = maps.Count,
                ["activo"] = activo,
                ["data_frames"] = dataFrames
            });
        }

        public static JObject SetActiveDf(JObject parameters)
        {
            string nombre = (string)parameters["nombre"];
            if (string.IsNullOrEmpty(nombre))
                throw new ArgumentException("Indica 'nombre' (data frame a activar).");

            IApplication app = ArcSession.App();
            IMxDocument doc = ArcSession.Doc(app);
            IMaps maps = doc.Maps;

            IMap objetivo = null;
            var disponibles = new List<string>();
            for (int i = 0; i < maps.Count; i++)
            {
                IMap m = maps.get_Item(i);
                disponibles.Add(m.Name);
                if (objetivo == null && string.Equals(m.Name, nombre, StringComparison.OrdinalIgnoreCase))
                    objetivo = m;
            }
            if (objetivo == null)
                throw new ArgumentException("Data frame no encontrado: " + nombre
                    + ". Disponibles: " + string.Join(", ", disponibles));

            doc.ActiveView = (IActiveView)objetivo;
            doc.ActiveView.Refresh();
            return Protocol.Result(new JObject { ["activo"] = objetivo.Name });
        }

        /// <summary>
        /// set_frame_clip — recorta un data frame a la forma de una capa (el perímetro del
        /// monte en los planos de una serie de montes), con capas que quedan FUERA del recorte y se ven
        /// enteras (la base, el propio límite), o quita el recorte. Sin parámetros de cambio,
        /// devuelve el estado. Es IMapClipOptions, lo de «Clip to shape» de ArcMap: arcpy no
        /// lo expone, y en una serie de montes se hizo con ArcObjects a mano (Cowork, 2026-10-06).
        ///
        /// La forma es la unión de las entidades de la capa que pasan su definition query
        /// (IFeatureLayer.Search la respeta) y el `where`, proyectada al data frame.
        /// </summary>
        public static JObject SetFrameClip(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            string where = (string)parameters["where"];
            bool quitar = Parametros.LeerBool(parameters["quitar"], "quitar", false);
            JArray excluir = parameters["excluir"] as JArray;
            if (Parametros.Dado(parameters["excluir"]) && excluir == null)
                throw new ArgumentException("'excluir' es una lista de capas.");
            if (quitar && (capa != null || where != null || excluir != null))
                throw new ArgumentException("'quitar' va solo: deja el data frame sin recorte.");
            if (where != null && capa == null)
                throw new ArgumentException("'where' filtra las entidades de 'capa': indica la capa.");

            Documento doc = Documento.Actual();
            IMap map = MapaDelLayout(doc, (string)parameters["data_frame"]);
            IMapClipOptions clip = (IMapClipOptions)map;
            JObject antes = EstadoRecorte(map, clip);

            if (!quitar && capa == null && excluir == null)
                return Protocol.Result(antes);

            if (quitar)
            {
                clip.ClipType = esriMapClipType.esriMapClipNone;
                clip.ClipFilter = new ESRI.ArcGIS.esriSystem.SetClass();
            }
            else
            {
                if (capa != null)
                {
                    IFeatureLayer fl = MapHandlers.FindLayer(map, capa) as IFeatureLayer;
                    if (fl == null || fl.FeatureClass == null)
                        throw new ArgumentException("'" + capa + "' no es una capa de entidades con datos accesibles.");
                    if (fl.FeatureClass.ShapeType != ESRI.ArcGIS.Geometry.esriGeometryType.esriGeometryPolygon)
                        throw new ArgumentException("La forma de recorte tiene que salir de una capa de POLÍGONOS; '"
                            + capa + "' es " + DataAccess.NombreTipoGeometria(fl.FeatureClass.ShapeType) + ".");
                    int n;
                    ESRI.ArcGIS.Geometry.IGeometry forma = Union(fl, where, map.SpatialReference, out n);
                    clip.ClipGeometry = forma;
                    clip.ClipType = esriMapClipType.esriMapClipShape;
                }
                else if (clip.ClipType != esriMapClipType.esriMapClipShape)
                    throw new ArgumentException("El data frame no está recortado a una forma: indica 'capa' para"
                        + " recortarlo; 'excluir' solo dice qué capas se libran del recorte.");
                if (excluir != null)
                {
                    var set = new ESRI.ArcGIS.esriSystem.SetClass();
                    foreach (JToken t in excluir)
                        set.Add(MapHandlers.FindLayer(map, (string)t));
                    clip.ClipFilter = set;
                }
            }

            ((IActiveView)map).ContentsChanged();
            doc.Refrescar();
            doc.RefrescarGraficosLayout();
            JObject r = EstadoRecorte(map, clip);
            r["antes"] = antes;
            return Protocol.Result(r);
        }

        private static ESRI.ArcGIS.Geometry.IGeometry Union(IFeatureLayer fl, string where,
            ESRI.ArcGIS.Geometry.ISpatialReference destino, out int n)
        {
            var filtro = new ESRI.ArcGIS.Geodatabase.QueryFilterClass();
            if (!string.IsNullOrEmpty(where))
                filtro.WhereClause = where;
            var bolsa = new ESRI.ArcGIS.Geometry.GeometryBagClass();
            ((ESRI.ArcGIS.Geometry.IGeometry)bolsa).SpatialReference = fl.FeatureClass.Fields
                .get_Field(fl.FeatureClass.FindField(fl.FeatureClass.ShapeFieldName)).GeometryDef.SpatialReference;
            n = 0;
            ESRI.ArcGIS.Geodatabase.IFeatureCursor cur = fl.Search(filtro, false);
            try
            {
                ESRI.ArcGIS.Geodatabase.IFeature f;
                object falta = Type.Missing;
                while ((f = cur.NextFeature()) != null)
                {
                    if (f.Shape == null || f.Shape.IsEmpty)
                        continue;
                    ((ESRI.ArcGIS.Geometry.IGeometryCollection)bolsa).AddGeometry(f.ShapeCopy, ref falta, ref falta);
                    n++;
                }
            }
            finally
            {
                System.Runtime.InteropServices.Marshal.ReleaseComObject(cur);
            }
            if (n == 0)
                throw new ArgumentException("Ninguna entidad de '" + fl.Name + "' pasa "
                    + (string.IsNullOrEmpty(where) ? "su definition query" : "el filtro '" + where + "' (ni su definition query)")
                    + ": no hay forma con la que recortar.");
            var poligono = new ESRI.ArcGIS.Geometry.PolygonClass();
            ((ESRI.ArcGIS.Geometry.ITopologicalOperator)poligono).ConstructUnion(
                (ESRI.ArcGIS.Geometry.IEnumGeometry)bolsa);
            ESRI.ArcGIS.Geometry.IGeometry g = poligono;
            if (destino != null)
                g.Project(destino);
            return g;
        }

        private static JObject EstadoRecorte(IMap map, IMapClipOptions clip)
        {
            var excluidas = new JArray();
            ESRI.ArcGIS.esriSystem.ISet set = clip.ClipFilter;
            if (set != null)
            {
                set.Reset();
                object o;
                while ((o = set.Next()) != null)
                {
                    ILayer l = o as ILayer;
                    excluidas.Add(l != null ? l.Name : o.ToString());
                }
            }
            var r = new JObject
            {
                ["data_frame"] = map.Name,
                ["recorte"] = clip.ClipType.ToString().Replace("esriMapClip", ""),
                ["excluidas"] = excluidas
            };
            ESRI.ArcGIS.Geometry.IArea area = clip.ClipGeometry as ESRI.ArcGIS.Geometry.IArea;
            if (clip.ClipType == esriMapClipType.esriMapClipShape && area != null)
                r["area_forma"] = Math.Round(area.Area, 2);
            return r;
        }

        /// <summary>El data frame por nombre (del layout, así vale también en disco) o, sin
        /// nombre, el activo.</summary>
        internal static IMap MapaDelLayout(Documento doc, string nombre)
        {
            if (string.IsNullOrEmpty(nombre))
                return doc.FocusMap;
            var disponibles = new List<string>();
            IMap hallado = null;
            int veces = 0;
            IGraphicsContainer gc = (IGraphicsContainer)doc.PageLayout;
            gc.Reset();
            IElement el;
            while ((el = gc.Next()) != null)
            {
                IMapFrame mf = el as IMapFrame;
                if (mf == null)
                    continue;
                disponibles.Add(mf.Map.Name);
                if (string.Equals(mf.Map.Name, nombre, StringComparison.OrdinalIgnoreCase))
                {
                    hallado = mf.Map;
                    veces++;
                }
            }
            if (veces == 0)
                throw new ArgumentException("Data frame no encontrado: " + nombre + ". Disponibles: "
                    + string.Join(", ", disponibles));
            if (veces > 1)
                throw new ArgumentException("Hay " + veces + " data frames llamados '" + nombre + "' y no se elige por"
                    + " ti: renombra uno o activa el que quieras y no pases 'data_frame'.");
            return hallado;
        }
    }
}
