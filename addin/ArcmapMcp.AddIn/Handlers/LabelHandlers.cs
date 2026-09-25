using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Etiquetas de una capa de entidades: expresión, tamaño, color y halo.
    ///
    /// arcpy llega a la expresión y poco más; el halo exige ArcObjects. Y la vía
    /// obvia en ArcObjects —vaciar las clases y crear unas LabelEngineLayerProperties
    /// nuevas— tiene un fallo silencioso: en un mapa con MAPLEX la capa se queda sin
    /// etiquetas y sin error (bloque 03 de ID2018, 2026-09-23). Con el motor
    /// estándar la misma vía sí pinta (Majal Blanco, 2026-09-25). Así que aquí:
    ///
    /// 1. Se MODIFICAN las clases que ya tiene la capa, sin crear ni borrar ninguna.
    ///    Sirve con los dos motores.
    /// 2. Solo si la capa no tiene ninguna clase se crea una, y con las propiedades
    ///    de colocación del motor del mapa: una clase nueva lleva por defecto las del
    ///    estándar, que Maplex no sabe colocar.
    /// 3. Lo que se devuelve se LEE de vuelta de la capa, no se copia de lo pedido.
    /// </summary>
    internal static class LabelHandlers
    {
        public static JObject SetLabels(JObject parameters)
        {
            string capa = (string)parameters["capa"];
            if (string.IsNullOrEmpty(capa))
                throw new ArgumentException("Indica 'capa' (nombre en la TOC, o ruta 'Grupo/Capa').");

            string expresion = Texto(parameters["expresion"]);
            string filtroClase = Texto(parameters["clase"]);
            double? tamano = LeerDouble(parameters["tamano"], "tamano", 1, 500);
            double? halo = LeerDouble(parameters["halo"], "halo", 0, 50);
            bool hayColor = Dado(parameters["color"]);
            bool hayColorHalo = Dado(parameters["color_halo"]);
            IColor color = hayColor ? Parametros.LeerColor(parameters["color"], "color", 0, 0, 0) : null;
            IColor colorHalo = Parametros.LeerColor(parameters["color_halo"], "color_halo", 255, 255, 255);
            JToken tActivar = parameters["activar"];
            bool? activar = Dado(tActivar) ? Parametros.LeerBool(tActivar, "activar", true) : (bool?)null;

            if (halo == null && hayColorHalo)
                throw new ArgumentException("'color_halo' sin 'halo' no hace nada: indica también el grosor"
                    + " del halo en puntos (por ejemplo 1).");

            IMxDocument doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer gfl = lyr as IGeoFeatureLayer;
            if (gfl == null)
                throw new ArgumentException("'" + capa + "' no es una capa de entidades: solo esas llevan etiquetas.");

            bool maplex;
            string motor = Motor(map, out maplex);
            IAnnotateLayerPropertiesCollection col = gfl.AnnotationProperties;

            bool cambiaAlgo = expresion != null || tamano != null || halo != null || hayColor;
            if (!cambiaAlgo && activar == null)
                return Protocol.Result(Estado(gfl, col, motor, maplex, false, null));

            JObject antes = Estado(gfl, col, motor, maplex, false, null);

            if (!cambiaAlgo)
            {
                // Solo encender o apagar: no se toca ninguna clase.
                gfl.DisplayAnnotation = activar.Value;
                MapHandlers.NotificarCambioContenido(map, doc);
                return Protocol.Result(Estado(gfl, col, motor, maplex, false, antes));
            }

            bool creada = false;
            if (col.Count == 0)
            {
                if (expresion == null)
                    throw new ArgumentException("La capa no tiene ninguna clase de etiquetas y no se puede"
                        + " inventar qué etiquetar: indica 'expresion' (por ejemplo \"[NOMBRE]\").");
                col.Add(ClaseNueva(maplex));
                creada = true;
            }

            var objetivo = new List<ILabelEngineLayerProperties>();
            var nombres = new List<string>();
            for (int k = 0; k < col.Count; k++)
            {
                IAnnotateLayerProperties alp = Clase(col, k);
                string nombre = NombreClase(alp);
                nombres.Add(nombre);
                if (filtroClase != null && !string.Equals(nombre, filtroClase, StringComparison.OrdinalIgnoreCase))
                    continue;
                ILabelEngineLayerProperties lep = alp as ILabelEngineLayerProperties;
                if (lep != null)
                    objetivo.Add(lep);
            }
            if (objetivo.Count == 0)
                throw new ArgumentException(filtroClase != null
                    ? "La capa no tiene ninguna clase de etiquetas llamada '" + filtroClase + "'. Clases: "
                      + string.Join(", ", nombres)
                    : "Ninguna clase de etiquetas de la capa se puede editar (no son de motor de etiquetado).");

            foreach (ILabelEngineLayerProperties lep in objetivo)
            {
                if (expresion != null)
                {
                    lep.Expression = expresion;
                    lep.IsExpressionSimple = true;
                }
                // Se trabaja sobre el símbolo que ya tiene la clase y se vuelve a
                // asignar al final: la fuente, el estilo y lo que no se pida se
                // quedan como estaban.
                ITextSymbol ts = lep.Symbol;
                if (tamano != null)
                    ts.Size = tamano.Value;
                if (hayColor)
                    ts.Color = color;
                if (halo != null)
                    AplicarHalo(ts, halo.Value, colorHalo, hayColorHalo);
                lep.Symbol = ts;
            }

            // Poner etiquetas y dejarlas apagadas no es lo que nadie pide: si cambia
            // algo y no se dice nada de 'activar', se encienden.
            gfl.DisplayAnnotation = activar ?? true;

            MapHandlers.NotificarCambioContenido(map, doc);
            return Protocol.Result(Estado(gfl, col, motor, maplex, creada, antes));
        }

        /// <summary>El motor de etiquetado del data frame. IAnnotateMap.Name es
        /// "ESRI Maplex Label Engine" o "ESRI Standard Label Engine"; se busca la
        /// palabra por si otra versión cambia el resto del texto.</summary>
        private static string Motor(IMap map, out bool maplex)
        {
            string nombre = null;
            try
            {
                IAnnotateMap am = map.AnnotationEngine;
                nombre = am != null ? am.Name : null;
            }
            catch { /* sin motor legible: se trata como estándar */ }
            maplex = nombre != null && nombre.IndexOf("Maplex", StringComparison.OrdinalIgnoreCase) >= 0;
            return nombre ?? "(desconocido)";
        }

        private static IAnnotateLayerProperties ClaseNueva(bool maplex)
        {
            ILabelEngineLayerProperties2 le = new LabelEngineLayerPropertiesClass();
            if (maplex)
                le.OverposterLayerProperties = (IOverposterLayerProperties)new MaplexOverposterLayerPropertiesClass();
            IAnnotateLayerProperties alp = (IAnnotateLayerProperties)le;
            alp.Class = "Default";
            return alp;
        }

        private static void AplicarHalo(ITextSymbol ts, double grosor, IColor colorHalo, bool hayColorHalo)
        {
            IMask m = ts as IMask;
            if (m == null)
                throw new InvalidOperationException("El símbolo de texto de la capa no admite halo (no es IMask).");
            if (grosor <= 0)
            {
                m.MaskStyle = esriMaskStyle.esriMSNone;
                return;
            }
            m.MaskStyle = esriMaskStyle.esriMSHalo;
            m.MaskSize = grosor;
            // El color del halo solo se toca si se pide o si no había símbolo: un halo
            // que ya era gris claro no debe volverse blanco por cambiar el grosor.
            if (hayColorHalo || m.MaskSymbol == null)
            {
                ISimpleFillSymbol relleno = new SimpleFillSymbolClass
                {
                    Color = colorHalo,
                    Style = esriSimpleFillStyle.esriSFSSolid,
                    Outline = new SimpleLineSymbolClass { Style = esriSimpleLineStyle.esriSLSNull }
                };
                m.MaskSymbol = (IFillSymbol)relleno;
            }
        }

        /// <summary>Clase k de la colección. IAnnotateLayerPropertiesCollection2 da
        /// acceso directo; QueryItem (la vía de la interfaz 1) tiene parámetros de
        /// salida que desde comtypes no se dejaban llamar, y aquí no hace falta.</summary>
        private static IAnnotateLayerProperties Clase(IAnnotateLayerPropertiesCollection col, int k)
        {
            IAnnotateLayerPropertiesCollection2 col2 = col as IAnnotateLayerPropertiesCollection2;
            if (col2 != null)
                return col2.get_Properties(k);
            IAnnotateLayerProperties alp;
            IElementCollection colocados, sinColocar;
            col.QueryItem(k, out alp, out colocados, out sinColocar);
            return alp;
        }

        private static string NombreClase(IAnnotateLayerProperties alp)
        {
            try { return alp.Class ?? ""; }
            catch { return ""; }
        }

        private static JObject Estado(IGeoFeatureLayer gfl, IAnnotateLayerPropertiesCollection col,
            string motor, bool maplex, bool creada, JObject antes)
        {
            var clases = new JArray();
            for (int k = 0; k < col.Count; k++)
            {
                IAnnotateLayerProperties alp = Clase(col, k);
                var c = new JObject { ["clase"] = NombreClase(alp) };
                ILabelEngineLayerProperties lep = alp as ILabelEngineLayerProperties;
                if (lep != null)
                {
                    c["expresion"] = lep.Expression;
                    ITextSymbol ts = lep.Symbol;
                    if (ts != null)
                    {
                        c["tamano"] = ts.Size;
                        c["color"] = Hex(ts.Color);
                        IMask m = ts as IMask;
                        bool conHalo = m != null && m.MaskStyle == esriMaskStyle.esriMSHalo;
                        c["halo"] = conHalo ? m.MaskSize : 0.0;
                        c["color_halo"] = conHalo && m.MaskSymbol != null ? Hex(m.MaskSymbol.Color) : null;
                    }
                }
                string filtro = null;
                try { filtro = alp.WhereClause; } catch { }
                if (!string.IsNullOrEmpty(filtro))
                    c["filtro"] = filtro;
                clases.Add(c);
            }
            var r = new JObject
            {
                ["capa"] = ((ILayer)gfl).Name,
                ["motor"] = motor,
                ["maplex"] = maplex,
                ["etiquetas_activas"] = gfl.DisplayAnnotation,
                ["clase_creada"] = creada,
                ["clases"] = clases
            };
            if (antes != null)
                r["antes"] = antes;
            return r;
        }

        private static string Hex(IColor c)
        {
            if (c == null)
                return null;
            IRgbColor rgb = c as IRgbColor;
            if (rgb != null)
                return string.Format("#{0:X2}{1:X2}{2:X2}", rgb.Red, rgb.Green, rgb.Blue);
            int v = c.RGB; // 0x00BBGGRR
            return string.Format("#{0:X2}{1:X2}{2:X2}", v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF);
        }

        private static bool Dado(JToken t)
        {
            return t != null && t.Type != JTokenType.Null;
        }

        private static string Texto(JToken t)
        {
            return Dado(t) ? (string)t : null;
        }

        private static double? LeerDouble(JToken t, string nombre, double min, double max)
        {
            if (!Dado(t))
                return null;
            if (t.Type != JTokenType.Integer && t.Type != JTokenType.Float)
                throw new ArgumentException("'" + nombre + "' debe ser un número. Recibido: " + t);
            double v = (double)t;
            if (v < min || v > max)
                throw new ArgumentException("'" + nombre + "' debe estar entre " + min + " y " + max + ". Recibido: " + v);
            return v;
        }
    }
}
