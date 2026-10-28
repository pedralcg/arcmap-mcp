using System;
using System.Collections.Generic;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.Geodatabase;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Etiquetas de una capa de entidades: expresión, tamaño, color y halo.
    ///
    /// arcpy llega a la expresión y poco más; el halo exige ArcObjects. Y la vía
    /// obvia en ArcObjects —vaciar las clases y crear unas LabelEngineLayerProperties
    /// nuevas— tiene un fallo silencioso: en un mapa con MAPLEX la capa se queda sin
    /// etiquetas y sin error (bloque 03 de una serie de montes, 2026-09-23). Con el motor
    /// estándar la misma vía sí pinta (MXD real, 2026-09-25). Así que aquí:
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
            double? tamano = Parametros.LeerDouble(parameters["tamano"], "tamano", 1, 500);
            double? halo = Parametros.LeerDouble(parameters["halo"], "halo", 0, 50);
            bool hayColor = Parametros.Dado(parameters["color"]);
            bool hayColorHalo = Parametros.Dado(parameters["color_halo"]);
            IColor color = hayColor ? Parametros.LeerColor(parameters["color"], "color", 0, 0, 0) : null;
            IColor colorHalo = Parametros.LeerColor(parameters["color_halo"], "color_halo", 255, 255, 255);
            JToken tActivar = parameters["activar"];
            bool? activar = Parametros.Dado(tActivar) ? Parametros.LeerBool(tActivar, "activar", true) : (bool?)null;

            if (halo == null && hayColorHalo)
                throw new ArgumentException("'color_halo' sin 'halo' no hace nada: indica también el grosor"
                    + " del halo en puntos (por ejemplo 1).");

            // Colocación Maplex (serie real, 2026-10-06: «Zona de Uso Agrario» rotulada 30 veces
            // en una hoja). 0 apaga la opción.
            JToken tParteMayor = parameters["parte_mayor"];
            bool? parteMayor = Parametros.Dado(tParteMayor) ? Parametros.LeerBool(tParteMayor, "parte_mayor", true) : (bool?)null;
            double? sinDuplicadosM = Parametros.LeerDouble(parameters["sin_duplicados_m"], "sin_duplicados_m", 0, 1e7);
            double? tamanoMinimoM = Parametros.LeerDouble(parameters["tamano_minimo_m"], "tamano_minimo_m", 0, 1e7);
            double? repetirCadaMm = Parametros.LeerDouble(parameters["repetir_cada_mm"], "repetir_cada_mm", 0, 10000);
            string copiarDe = Texto(parameters["copiar_de"]);
            JObject porValor = parameters["por_valor"] as JObject;
            if (Parametros.Dado(parameters["por_valor"]) && porValor == null)
                throw new ArgumentException("'por_valor' es un objeto: {\"campo\": \"ZONA\"} y, opcional, \"colores\": {valor: color}.");
            if (porValor != null && filtroClase != null)
                throw new ArgumentException("'por_valor' rehace todas las clases: no se combina con 'clase'.");
            // Con 'color' cada clase perdía el de su valor y todas salían iguales (revisión del
            // 2026-10-25): el color de cada valor va en 'por_valor.colores'.
            if (porValor != null && hayColor)
                throw new ArgumentException("'por_valor' da a cada clase el color de su valor: no se combina con"
                    + " 'color'. Para fijarlos, 'por_valor': {\"campo\": ..., \"colores\": {valor: color}}.");
            // Filtro SQL de la clase (op `label_expr` con SQL de una serie de montes: rotular solo las fajas
            // del monte), fuente y estilo (negrita en una serie real), y más opciones de Maplex
            // (etiqueta21_ao.py de una serie de montes). "" en 'where' lo quita.
            string where = parameters["where"] != null && parameters["where"].Type != JTokenType.Null
                ? (string)parameters["where"] : null;
            string fuente = Texto(parameters["fuente"]);
            bool? negrita = Parametros.Dado(parameters["negrita"]) ? Parametros.LeerBool(parameters["negrita"], "negrita", false) : (bool?)null;
            bool? cursiva = Parametros.Dado(parameters["cursiva"]) ? Parametros.LeerBool(parameters["cursiva"], "cursiva", false) : (bool?)null;
            bool? nuncaQuitar = Parametros.Dado(parameters["nunca_quitar"]) ? Parametros.LeerBool(parameters["nunca_quitar"], "nunca_quitar", false) : (bool?)null;
            bool? sobrepasar = Parametros.Dado(parameters["sobrepasar_entidad"]) ? Parametros.LeerBool(parameters["sobrepasar_entidad"], "sobrepasar_entidad", false) : (bool?)null;
            int? buffer = Parametros.Dado(parameters["buffer"]) ? Parametros.LeerEntero(parameters["buffer"], "buffer", 0, 0, 1000) : (int?)null;
            esriMaplexLinePlacementMethod? colocacion = LeerColocacionLinea(parameters["colocacion_linea"]);
            bool hayMaplex = parteMayor != null || sinDuplicadosM != null || tamanoMinimoM != null || repetirCadaMm != null
                || nuncaQuitar != null || sobrepasar != null || buffer != null || colocacion != null;

            Documento doc;
            IMap map = MapHandlers.FocusMap(out doc);
            ILayer lyr = MapHandlers.FindLayer(map, capa);
            IGeoFeatureLayer gfl = lyr as IGeoFeatureLayer;
            if (gfl == null)
                throw new ArgumentException("'" + capa + "' no es una capa de entidades: solo esas llevan etiquetas.");

            if (expresion != null)
                ComprobarCampos(gfl, expresion);

            bool maplex;
            string motor = Motor(map, out maplex);
            if (hayMaplex && !maplex)
                throw new ArgumentException("'parte_mayor', 'sin_duplicados_m', 'tamano_minimo_m', 'repetir_cada_mm',"
                    + " 'nunca_quitar', 'sobrepasar_entidad', 'buffer' y 'colocacion_linea' son opciones de Maplex, y este"
                    + " data frame etiqueta con " + motor + ".");
            if (!string.IsNullOrEmpty(where))
                ComprobarWhere(gfl, where);
            if ((sinDuplicadosM > 0 || tamanoMinimoM > 0) && map.MapUnits != ESRI.ArcGIS.esriSystem.esriUnits.esriMeters)
                throw new ArgumentException("'sin_duplicados_m' y 'tamano_minimo_m' van en metros, en las unidades del mapa,"
                    + " y este mapa está en " + map.MapUnits.ToString().Replace("esri", "") + ".");
            IAnnotateLayerPropertiesCollection col = gfl.AnnotationProperties;

            bool cambiaAlgo = expresion != null || tamano != null || halo != null || hayColor
                || hayMaplex || copiarDe != null || porValor != null || where != null || fuente != null
                || negrita != null || cursiva != null;
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

            // Foto de las clases y del encendido: varias cosas pueden lanzar con la capa ya tocada
            // (las expresiones copiadas con copiar_de, una fuente que no está instalada a mitad del
            // bucle). Se devuelven tal cual estaban; en vivo, un error no debe dejar la capa cambiada
            // (revisión del 2026-10-25). Se restaura un CLON, como copiar_de: es la vía segura con Maplex.
            var fotoClases = (IAnnotateLayerPropertiesCollection)((ESRI.ArcGIS.esriSystem.IClone)col).Clone();
            bool fotoEncendidas = gfl.DisplayAnnotation;
            bool creada = false;
            JArray clasesPorValor = null;
            try
            {
                // copiar_de: las clases de otra capa, CLONADAS. Es la vía segura con Maplex: las
                // clases creadas desde cero dejan la capa sin etiquetas y sin error.
                if (copiarDe != null)
                {
                    IGeoFeatureLayer origen = MapHandlers.FindLayer(map, copiarDe) as IGeoFeatureLayer;
                    if (origen == null)
                        throw new ArgumentException("'copiar_de': '" + copiarDe + "' no es una capa de entidades.");
                    if (origen.AnnotationProperties == null || origen.AnnotationProperties.Count == 0)
                        throw new ArgumentException("'copiar_de': '" + copiarDe + "' no tiene clases de etiquetas que copiar.");
                    gfl.AnnotationProperties = (IAnnotateLayerPropertiesCollection)
                        ((ESRI.ArcGIS.esriSystem.IClone)origen.AnnotationProperties).Clone();
                    col = gfl.AnnotationProperties;
                    // La expresión copiada tiene que valer en esta capa (si no se pide otra).
                    if (expresion == null)
                        for (int k = 0; k < col.Count; k++)
                        {
                            ILabelEngineLayerProperties lepC = Clase(col, k) as ILabelEngineLayerProperties;
                            if (lepC != null && !string.IsNullOrEmpty(lepC.Expression))
                                ComprobarCampos(gfl, lepC.Expression);
                        }
                }

                if (col.Count == 0)
                {
                    if (expresion == null)
                        throw new ArgumentException("La capa no tiene ninguna clase de etiquetas y no se puede"
                            + " inventar qué etiquetar: indica 'expresion' (por ejemplo \"[NOMBRE]\").");
                    col.Add(ClaseNueva(maplex));
                    creada = true;
                }

                if (porValor != null)
                    clasesPorValor = ClasesPorValor(gfl, col, porValor, where);

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
                    // El tamaño se pone al final, tras reasignar la fuente: el IFontDisp lo redondea a
                    // píxeles de pantalla (8,5 → 8,25) y una negrita sola movía el tamaño (2026-10-10).
                    double tam = tamano ?? ts.Size;
                    if (hayColor)
                        ts.Color = color;
                    if (halo != null)
                        AplicarHalo(ts, halo.Value, colorHalo, hayColorHalo);
                    if (fuente != null || negrita != null || cursiva != null)
                    {
                        // La fuente se modifica y se REASIGNA: el getter puede dar una copia.
                        stdole.IFontDisp f = ts.Font;
                        if (fuente != null) f.Name = fuente;
                        if (negrita != null) f.Bold = negrita.Value;
                        if (cursiva != null) f.Italic = cursiva.Value;
                        ts.Font = f;
                        if (fuente != null && !string.Equals(ts.Font.Name, fuente, StringComparison.OrdinalIgnoreCase))
                            throw new ArgumentException("La fuente '" + fuente + "' no está instalada (queda '" + ts.Font.Name + "').");
                    }
                    ts.Size = tam;
                    lep.Symbol = ts;
                    // Con por_valor el 'where' ya va compuesto en el filtro de cada clase: ponerlo
                    // aquí tal cual pisaba el «campo = valor» y dejaba N etiquetas por entidad.
                    if (where != null && porValor == null)
                        ((IAnnotateLayerProperties)lep).WhereClause = where;
                    if (hayMaplex)
                        AplicarMaplex(lep, parteMayor, sinDuplicadosM, tamanoMinimoM, repetirCadaMm,
                                      nuncaQuitar, sobrepasar, buffer, colocacion);
                }

                // Poner etiquetas y dejarlas apagadas no es lo que nadie pide: si cambia
                // algo y no se dice nada de 'activar', se encienden.
                gfl.DisplayAnnotation = activar ?? true;
            }
            catch
            {
                gfl.AnnotationProperties = fotoClases;
                gfl.DisplayAnnotation = fotoEncendidas;
                throw;
            }

            MapHandlers.NotificarCambioContenido(map, doc);
            JObject r = Estado(gfl, col, motor, maplex, creada, antes);
            if (copiarDe != null)
                r["copiado_de"] = copiarDe;
            if (clasesPorValor != null)
                r["por_valor"] = clasesPorValor;
            return Protocol.Result(r);
        }

        /// <summary>
        /// Opciones de colocación de Maplex en una clase. Las propiedades viven repartidas
        /// en IMaplexOverposterLayerProperties, ...2 (unidades) y ...4 (LabelLargestPolygon,
        /// que NO está en la base: un try/except que lo buscaba allí escondió durante horas
        /// que no se aplicaba). Se trabaja sobre las propiedades de la clase y se vuelven a
        /// asignar; lo aplicado se devuelve LEÍDO en el estado.
        /// </summary>
        private static esriMaplexLinePlacementMethod? LeerColocacionLinea(JToken t)
        {
            if (!Parametros.Dado(t))
                return null;
            switch (((string)t ?? "").Trim().ToLowerInvariant())
            {
                case "horizontal_centrada": return esriMaplexLinePlacementMethod.esriMaplexCenteredHorizontalOnLine;
                case "recta_centrada": return esriMaplexLinePlacementMethod.esriMaplexCenteredStraightOnLine;
                case "curva_centrada": return esriMaplexLinePlacementMethod.esriMaplexCenteredCurvedOnLine;
                case "perpendicular_centrada": return esriMaplexLinePlacementMethod.esriMaplexCenteredPerpendicularOnLine;
                case "horizontal": return esriMaplexLinePlacementMethod.esriMaplexOffsetHorizontalFromLine;
                case "recta": return esriMaplexLinePlacementMethod.esriMaplexOffsetStraightFromLine;
                case "curva": return esriMaplexLinePlacementMethod.esriMaplexOffsetCurvedFromLine;
                case "perpendicular": return esriMaplexLinePlacementMethod.esriMaplexOffsetPerpendicularFromLine;
            }
            throw new ArgumentException("'colocacion_linea': horizontal | recta | curva | perpendicular (desplazadas de la"
                + " línea) o horizontal_centrada | recta_centrada | curva_centrada | perpendicular_centrada (sobre ella).");
        }

        private static string NombreColocacion(esriMaplexLinePlacementMethod m)
        {
            switch (m)
            {
                case esriMaplexLinePlacementMethod.esriMaplexCenteredHorizontalOnLine: return "horizontal_centrada";
                case esriMaplexLinePlacementMethod.esriMaplexCenteredStraightOnLine: return "recta_centrada";
                case esriMaplexLinePlacementMethod.esriMaplexCenteredCurvedOnLine: return "curva_centrada";
                case esriMaplexLinePlacementMethod.esriMaplexCenteredPerpendicularOnLine: return "perpendicular_centrada";
                case esriMaplexLinePlacementMethod.esriMaplexOffsetHorizontalFromLine: return "horizontal";
                case esriMaplexLinePlacementMethod.esriMaplexOffsetStraightFromLine: return "recta";
                case esriMaplexLinePlacementMethod.esriMaplexOffsetCurvedFromLine: return "curva";
                default: return "perpendicular";
            }
        }

        /// <summary>El filtro de la clase, probado contra la capa: un campo mal escrito deja la
        /// clase sin rotular y sin error, como la expresión.</summary>
        private static void ComprobarWhere(IGeoFeatureLayer gfl, string where)
        {
            if (gfl.FeatureClass == null)
                return;
            try
            {
                // Contra la tabla de VISUALIZACIÓN, con sus uniones, como ComprobarCampos: contra la
                // clase de entidades se rechazaba un `tabla.CAMPO` válido (revisión del 2026-10-25,
                // hipótesis 7, confirmada en vivo el 2026-10-28).
                IQueryFilter qf = new QueryFilterClass { WhereClause = where };
                IDisplayTable dt = gfl as IDisplayTable;
                if (dt != null)
                {
                    ICursor cur = dt.SearchDisplayTable(qf, true);
                    try { cur.NextRow(); }
                    finally { System.Runtime.InteropServices.Marshal.ReleaseComObject(cur); }
                }
                else
                    gfl.FeatureClass.FeatureCount(qf);
            }
            catch (Exception ex)
            {
                throw new ArgumentException("El filtro 'where' no es válido en esta capa: " + where + " (" + ex.Message + ").");
            }
        }

        private static void AplicarMaplex(ILabelEngineLayerProperties lep, bool? parteMayor,
            double? sinDuplicadosM, double? tamanoMinimoM, double? repetirCadaMm,
            bool? nuncaQuitar = null, bool? sobrepasar = null, int? buffer = null,
            esriMaplexLinePlacementMethod? colocacion = null)
        {
            ILabelEngineLayerProperties2 lep2 = lep as ILabelEngineLayerProperties2;
            IMaplexOverposterLayerProperties mop = lep2 != null
                ? lep2.OverposterLayerProperties as IMaplexOverposterLayerProperties : null;
            if (mop == null)
                throw new InvalidOperationException("La clase '" + NombreClase((IAnnotateLayerProperties)lep)
                    + "' no tiene propiedades de Maplex aunque el mapa use Maplex.");
            IMaplexOverposterLayerProperties2 mop2 = mop as IMaplexOverposterLayerProperties2;
            if (parteMayor != null)
            {
                IMaplexOverposterLayerProperties4 mop4 = mop as IMaplexOverposterLayerProperties4;
                if (mop4 == null)
                    throw new InvalidOperationException("Esta versión de Maplex no tiene 'etiquetar solo la parte mayor'.");
                mop4.LabelLargestPolygon = parteMayor.Value;
            }
            if (sinDuplicadosM != null)
            {
                mop.ThinDuplicateLabels = sinDuplicadosM.Value > 0;
                if (sinDuplicadosM.Value > 0)
                {
                    mop.ThinningDistance = sinDuplicadosM.Value;
                    if (mop2 != null) mop2.ThinningDistanceUnit = esriMaplexUnit.esriMaplexUnitMap;
                }
            }
            if (tamanoMinimoM != null)
            {
                mop.MinimumSizeForLabeling = tamanoMinimoM.Value;
                if (mop2 != null) mop2.MinimumFeatureSizeUnit = esriMaplexUnit.esriMaplexUnitMap;
            }
            if (repetirCadaMm != null)
            {
                mop.RepeatLabel = repetirCadaMm.Value > 0;
                if (repetirCadaMm.Value > 0)
                {
                    mop.MinimumRepetitionInterval = repetirCadaMm.Value;
                    if (mop2 != null) mop2.RepetitionIntervalUnit = esriMaplexUnit.esriMaplexUnitMM;
                }
            }
            if (nuncaQuitar != null) mop.NeverRemoveLabel = nuncaQuitar.Value;
            if (sobrepasar != null) mop.CanOverrunFeature = sobrepasar.Value;
            if (buffer != null) mop.LabelBuffer = buffer.Value;
            if (colocacion != null) mop.LinePlacementMethod = colocacion.Value;
            lep2.OverposterLayerProperties = (IOverposterLayerProperties)mop;
        }

        /// <summary>
        /// por_valor: una clase de etiquetas por cada valor de 'campo', cada una con el
        /// color de su categoría (o el que se dé en 'colores'). Las clases se CLONAN de la
        /// que la capa ya tiene (la primera): crearlas desde cero deja Maplex sin
        /// etiquetas. Sustituye todas las clases de la capa. Un 'where' se compone con AND
        /// en el filtro de cada clase ("" o null: solo el del valor).
        /// </summary>
        private static JArray ClasesPorValor(IGeoFeatureLayer gfl, IAnnotateLayerPropertiesCollection col, JObject porValor,
                                             string where)
        {
            string campo = (string)porValor["campo"];
            if (string.IsNullOrEmpty(campo))
                throw new ArgumentException("'por_valor' necesita 'campo'.");
            if (gfl.FeatureClass == null)
                throw new ArgumentException("'por_valor' necesita leer los valores de la capa, y la capa está rota:"
                    + " repara su fuente antes (repair_data_source o replace_data_source).");
            IFields campos = gfl.FeatureClass.Fields;
            int iCampo = campos.FindField(campo);
            if (iCampo < 0)
                throw new ArgumentException("'por_valor': la capa no tiene el campo '" + campo + "'.");
            IField f = campos.get_Field(iCampo);
            bool esTexto = f.Type == esriFieldType.esriFieldTypeString;

            // Valor -> color: los de 'colores' mandan; si no, los de la simbología por valores
            // únicos de ese mismo campo.
            var valores = new List<KeyValuePair<string, IColor>>();
            JObject colores = porValor["colores"] as JObject;
            if (colores != null && colores.Count > 0)
            {
                foreach (var p in colores)
                    valores.Add(new KeyValuePair<string, IColor>(p.Key, Parametros.LeerColor(p.Value, "colores." + p.Key, 0, 0, 0)));
            }
            else
            {
                IUniqueValueRenderer uvr = gfl.Renderer as IUniqueValueRenderer;
                if (uvr == null || uvr.FieldCount != 1
                    || !string.Equals(uvr.get_Field(0), campo, StringComparison.OrdinalIgnoreCase))
                    throw new ArgumentException("'por_valor' sin 'colores' toma los colores de la simbología, y la capa no"
                        + " está simbolizada por valores únicos de '" + campo + "'. Da 'colores': {valor: color}.");
                for (int j = 0; j < uvr.ValueCount; j++)
                {
                    string v = uvr.get_Value(j);
                    valores.Add(new KeyValuePair<string, IColor>(v, ColorDeSimbolo(uvr.get_Symbol(v))));
                }
            }
            if (valores.Count == 0)
                throw new ArgumentException("'por_valor': no hay valores con los que hacer clases.");
            if (valores.Count > 100)
                throw new ArgumentException("'por_valor': más de 100 valores; una clase de etiquetas por cada uno no tiene sentido.");

            // 🔴 La clase ORIGINAL se queda en la colección (pasa a ser la del primer valor) y
            // los demás valores son CLONES añadidos detrás. Vaciar la colección (Clear) y meter
            // solo clones deja la capa SIN ETIQUETAS en Maplex y sin error: la primera versión
            // lo hacía y lo cazó la prueba de imagen (2026-10-06). Es el patrón que funcionó
            // en los planos de una serie real.
            IAnnotateLayerProperties plantilla = Clase(col, 0);
            var clones = new List<IAnnotateLayerProperties>();
            for (int i = 1; i < valores.Count; i++)
                clones.Add((IAnnotateLayerProperties)((ESRI.ArcGIS.esriSystem.IClone)plantilla).Clone());
            var salida = new JArray();
            for (int i = 0; i < valores.Count; i++)
            {
                var kv = valores[i];
                IAnnotateLayerProperties alp = i == 0 ? plantilla : clones[i - 1];
                alp.Class = kv.Key;
                string delValor = campo + " = " + (esTexto ? "'" + kv.Key.Replace("'", "''") + "'" : kv.Key);
                alp.WhereClause = string.IsNullOrEmpty(where) ? delValor : "(" + delValor + ") AND (" + where + ")";
                ILabelEngineLayerProperties lep = alp as ILabelEngineLayerProperties;
                if (lep != null && kv.Value != null)
                {
                    ITextSymbol ts = lep.Symbol;
                    ts.Color = kv.Value;
                    lep.Symbol = ts;
                }
                salida.Add(new JObject { ["valor"] = kv.Key, ["filtro"] = alp.WhereClause });
            }
            // Fuera las demás clases que tuviera la capa, de atrás adelante, sin tocar la 0.
            while (col.Count > 1)
                col.Remove(Clase(col, col.Count - 1));
            foreach (IAnnotateLayerProperties alp in clones)
                col.Add(alp);
            return salida;
        }

        /// <summary>El color que «es» la categoría: el relleno, o el borde si el relleno es
        /// hueco; el de la línea; el del marcador.
        ///
        /// 🔴 Del RELLENO se devuelve un tono OSCURO (55 %): una etiqueta del mismo color que
        /// el polígono sobre el que se dibuja existe pero no se ve. La primera versión lo
        /// hacía y el JPG con etiquetas salía idéntico byte a byte al JPG sin ellas
        /// (2026-10-06). Es lo que hizo a mano el script de una serie real.</summary>
        private static IColor ColorDeSimbolo(ISymbol sym)
        {
            IFillSymbol fs = sym as IFillSymbol;
            if (fs != null)
            {
                ISimpleFillSymbol sfs = sym as ISimpleFillSymbol;
                bool hueco = (sfs != null && sfs.Style == esriSimpleFillStyle.esriSFSHollow)
                    || fs.Color == null || fs.Color.NullColor;
                if (hueco && fs.Outline != null)
                    return fs.Outline.Color;
                return Oscurecer(fs.Color, 0.55);
            }
            ILineSymbol ls = sym as ILineSymbol;
            if (ls != null)
                return ls.Color;
            IMarkerSymbol ms = sym as IMarkerSymbol;
            if (ms != null)
                return ms.Color;
            return null;
        }

        private static IColor Oscurecer(IColor c, double factor)
        {
            IRgbColor rgb = new RgbColorClass { RGB = c.RGB };
            return new RgbColorClass
            {
                Red = (int)Math.Round(rgb.Red * factor),
                Green = (int)Math.Round(rgb.Green * factor),
                Blue = (int)Math.Round(rgb.Blue * factor)
            };
        }

        /// <summary>
        /// Los campos [entre corchetes] de la expresión tienen que existir en la capa.
        ///
        /// Si no existen, ArcMap no dibuja NINGUNA etiqueta y no da error: set_labels
        /// respondía «activas» y no salía nada. Pasó el 2026-10-01 en un expediente real al
        /// reañadir capas desde .shp con la expresión de la capa anterior, y se atribuyó
        /// a Maplex (hipótesis que la prueba en vivo desmintió). Se mira la tabla de
        /// presentación, que incluye los campos de las uniones ("tabla.campo").
        /// </summary>
        internal static void ComprobarCampos(IGeoFeatureLayer gfl, string expresion)
        {
            IFields campos = null;
            try
            {
                IDisplayTable dt = gfl as IDisplayTable;
                campos = dt != null ? dt.DisplayTable.Fields : gfl.FeatureClass.Fields;
            }
            catch { return; } // sin campos legibles (capa rota): no se puede comprobar
            if (campos == null)
                return;
            var nombres = new List<string>();
            for (int i = 0; i < campos.FieldCount; i++)
                nombres.Add(campos.get_Field(i).Name);

            // Lo que va entre comillas es texto, no campos: `[SUP] & " [ha]"` es válida y ArcMap la
            // dibuja, pero leída a ciegas pedía un campo `ha` (revisión del 2026-10-01). En
            // VBScript una comilla dentro del texto se escribe doble ("").
            string sinLiterales = System.Text.RegularExpressions.Regex.Replace(expresion, "\"(?:[^\"]|\"\")*\"", "\"\"");
            // Solo vale el nombre EXACTO. Con una tabla unida los campos se llaman `tabla.CAMPO`, y
            // ArcMap no dibuja nada con el nombre corto, aunque solo exista en una de las tablas
            // (medido en vivo el 2026-10-07 con un .dbf y con un .csv unidos). Aceptar la cola
            // `.CAMPO`, como se hacía, dejaba pasar justo la expresión que no dibuja.
            var faltan = new List<string>();
            var sugerencias = new List<string>();
            foreach (System.Text.RegularExpressions.Match m in
                     System.Text.RegularExpressions.Regex.Matches(sinLiterales, @"\[([^\[\]]+)\]"))
            {
                string pedido = m.Groups[1].Value.Trim();
                if (nombres.Exists(n => string.Equals(n, pedido, StringComparison.OrdinalIgnoreCase))
                    || faltan.Contains(pedido))
                    continue;
                faltan.Add(pedido);
                foreach (string n in nombres)
                    if (n.EndsWith("." + pedido, StringComparison.OrdinalIgnoreCase))
                        sugerencias.Add("[" + n + "]");
            }
            if (faltan.Count > 0)
                throw new ArgumentException("La expresión usa " + (faltan.Count == 1 ? "un campo que" : "campos que")
                    + " la capa no tiene: [" + string.Join("], [", faltan) + "]. ArcMap no dibujaría ninguna"
                    + " etiqueta y no avisaría. "
                    + (sugerencias.Count > 0
                        ? "La capa tiene una tabla unida, y entonces el campo va con su nombre COMPLETO: "
                          + string.Join(" o ", sugerencias) + "."
                        : "Campos de la capa: " + string.Join(", ", nombres) + "."));
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

        /// <summary>Clase k (POSICIÓN 0..Count-1) de la colección, por QueryItem. No por
        /// IAnnotateLayerPropertiesCollection2.get_Properties: ese pide el ID de la clase, que
        /// solo coincide con la posición mientras nadie quite clases. Tras un por_valor (quita
        /// las sobrantes y añade clones) repetir por_valor daba E_FAIL (regresión del 2026-10-28).</summary>
        internal static IAnnotateLayerProperties Clase(IAnnotateLayerPropertiesCollection col, int k)
        {
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
                        c["color"] = Parametros.Hex(ts.Color);
                        IMask m = ts as IMask;
                        bool conHalo = m != null && m.MaskStyle == esriMaskStyle.esriMSHalo;
                        c["halo"] = conHalo ? m.MaskSize : 0.0;
                        c["color_halo"] = conHalo && m.MaskSymbol != null ? Parametros.Hex(m.MaskSymbol.Color) : null;
                        try
                        {
                            c["fuente"] = ts.Font.Name;
                            c["negrita"] = ts.Font.Bold;
                            c["cursiva"] = ts.Font.Italic;
                        }
                        catch { }
                    }
                }
                if (maplex)
                {
                    ILabelEngineLayerProperties2 lep2 = alp as ILabelEngineLayerProperties2;
                    IMaplexOverposterLayerProperties mop = lep2 != null
                        ? lep2.OverposterLayerProperties as IMaplexOverposterLayerProperties : null;
                    if (mop != null)
                    {
                        IMaplexOverposterLayerProperties2 mop2 = mop as IMaplexOverposterLayerProperties2;
                        IMaplexOverposterLayerProperties4 mop4 = mop as IMaplexOverposterLayerProperties4;
                        var mx = new JObject
                        {
                            ["parte_mayor"] = mop4 != null ? (JToken)mop4.LabelLargestPolygon : null,
                            ["sin_duplicados"] = mop.ThinDuplicateLabels,
                            ["distancia_duplicados"] = mop.ThinningDistance,
                            ["tamano_minimo"] = mop.MinimumSizeForLabeling,
                            ["repetir"] = mop.RepeatLabel,
                            ["intervalo_repeticion"] = mop.MinimumRepetitionInterval,
                            ["nunca_quitar"] = mop.NeverRemoveLabel,
                            ["sobrepasar_entidad"] = mop.CanOverrunFeature,
                            ["buffer"] = mop.LabelBuffer,
                            ["colocacion_linea"] = NombreColocacion(mop.LinePlacementMethod)
                        };
                        if (mop2 != null)
                        {
                            mx["unidad_duplicados"] = mop2.ThinningDistanceUnit.ToString().Replace("esriMaplexUnit", "");
                            mx["unidad_tamano_minimo"] = mop2.MinimumFeatureSizeUnit.ToString().Replace("esriMaplexUnit", "");
                            mx["unidad_repeticion"] = mop2.RepetitionIntervalUnit.ToString().Replace("esriMaplexUnit", "");
                        }
                        c["maplex"] = mx;
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

        private static string Texto(JToken t)
        {
            return Parametros.Dado(t) ? (string)t : null;
        }

    }
}
