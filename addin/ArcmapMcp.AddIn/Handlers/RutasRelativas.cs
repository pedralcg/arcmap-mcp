using System;
using System.Collections.Generic;
using System.IO;
using ESRI.ArcGIS.ArcMapUI;
using ESRI.ArcGIS.Carto;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Capas que ArcMap 10.5 va a PERDER al guardar con rutas relativas.
    ///
    /// Medido el 2026-09-25 con arcpy (cinco series, guardando y REABRIENDO): con
    /// «Store relative pathnames», ArcMap concatena sin normalizar la carpeta del
    /// .mxd y la ruta relativa del WORKSPACE de la capa (C:\planos\..\..\datos). Si
    /// esa cadena llega a 260 caracteres (MAX_PATH), el workspace no se escribe y
    /// al reabrir la fuente es "\fichero.shp": capa rota. En memoria se sigue
    /// viendo bien, así que nadie se entera hasta reabrir.
    ///
    /// No depende de la ruta absoluta del dato (aguantó 252 caracteres con el
    /// .mxd en una carpeta corta), ni del nombre del fichero: el caso de ID2018 que
    /// lo destapó, con un dato de 224, rompía por lo profundo de la carpeta del .mxd.
    ///
    /// Esto PREDICE. La única comprobación que no miente es reabrir el documento
    /// guardado, que hace el servidor (save_mxd con verificar).
    /// </summary>
    internal static class RutasRelativas
    {
        /// <summary>MAX_PATH: con 259 guarda bien, con 260 pierde el workspace.</summary>
        public const int Tope = 260;

        /// <summary>
        /// Longitud de carpeta_mxd + "\" + relativa(workspace), o null si ArcMap no
        /// puede guardar esa ruta como relativa (otra unidad: la guarda absoluta y
        /// este fallo no existe).
        /// </summary>
        public static int? LongitudConcatenada(string carpetaMxd, string workspace)
        {
            if (string.IsNullOrEmpty(carpetaMxd) || string.IsNullOrEmpty(workspace))
                return null;
            string a, b;
            try
            {
                a = Path.GetFullPath(carpetaMxd).TrimEnd('\\');
                b = Path.GetFullPath(workspace).TrimEnd('\\');
            }
            catch (Exception)
            {
                return null;
            }
            if (!string.Equals(Path.GetPathRoot(a), Path.GetPathRoot(b), StringComparison.OrdinalIgnoreCase))
                return null;

            string[] pa = a.Split('\\');
            string[] pb = b.Split('\\');
            int comun = 0;
            while (comun < pa.Length && comun < pb.Length
                   && string.Equals(pa[comun], pb[comun], StringComparison.OrdinalIgnoreCase))
                comun++;
            var partes = new List<string>();
            for (int i = comun; i < pa.Length; i++)
                partes.Add("..");
            for (int i = comun; i < pb.Length; i++)
                partes.Add(pb[i]);
            string relativa = string.Join("\\", partes.ToArray());
            return a.Length + 1 + relativa.Length;
        }

        /// <summary>Riesgo de un workspace concreto frente al .mxd dado, o null.</summary>
        public static JObject Riesgo(string rutaMxd, string workspace)
        {
            string carpeta = string.IsNullOrEmpty(rutaMxd) ? null : Path.GetDirectoryName(rutaMxd);
            int? n = LongitudConcatenada(carpeta, workspace);
            if (n == null || n.Value < Tope)
                return null;
            return new JObject
            {
                ["workspace"] = workspace,
                ["longitud"] = n.Value,
                ["tope"] = Tope
            };
        }

        /// <summary>Todas las capas del documento que se perderían al guardar en
        /// `rutaMxd` con rutas relativas. Recorre todos los data frames.</summary>
        public static JArray CapasEnRiesgo(IMxDocument doc, string rutaMxd)
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
                    string workspace;
                    try { DataAccess.RutaFuente(c.Capa, out workspace); }
                    catch (Exception) { continue; }
                    JObject r = Riesgo(rutaMxd, workspace);
                    if (r == null)
                        continue;
                    r["data_frame"] = m.Name;
                    r["ruta"] = c.Ruta;
                    salida.Add(r);
                }
            }
            return salida;
        }

        public static string Aviso(int n, string rutaMxd)
        {
            return n + (n == 1 ? " capa va" : " capas van") + " a quedar ROTAS al guardar en " + rutaMxd
                + ": con rutas relativas, ArcMap 10.5 no escribe el workspace si la carpeta del .mxd"
                + " más la ruta relativa llega a " + Tope + " caracteres (queda '\\fichero.shp'). En"
                + " memoria se ven bien. Arreglo: acortar la carpeta de los datos o la del .mxd (lo"
                + " preferido), o guardar ese .mxd con rutas absolutas.";
        }
    }
}
