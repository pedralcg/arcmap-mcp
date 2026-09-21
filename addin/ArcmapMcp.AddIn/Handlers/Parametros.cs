using System;
using System.Globalization;
using System.IO;
using ESRI.ArcGIS.Display;
using Newtonsoft.Json.Linq;

namespace ArcmapMcp.AddIn.Handlers
{
    /// <summary>
    /// Lectura y validación de los parámetros del contrato JSON, compartida por
    /// todos los handlers.
    ///
    /// Existe por dos defectos que se repetían handler a handler:
    ///
    /// 1. Los COLORES los leía cada uno a su manera —uno recortaba al rango, dos
    ///    lanzaban— y los tres IGNORABAN EN SILENCIO un color mal formado
    ///    ("#FF0000", [255,0]): se caía a la rampa por defecto y quien llamaba se
    ///    quedaba creyendo que su color se había aplicado. Aquí lo mal formado es
    ///    siempre un ERROR, y se acepta además la notación hexadecimal, que es la
    ///    que se escribe a mano.
    /// 2. Los ENTEROS se leían con `(int)parameters["x"]`, que revienta con un
    ///    JSON null (InvalidCastException cruda), y sin techo: 1200 dpi sobre un
    ///    A1 en un proceso de 32 bits es un OutOfMemory, no un export.
    /// </summary>
    internal static class Parametros
    {
        /// <summary>Color desde [R,G,B] (0-255) o "#RRGGBB". Ausente o null → el
        /// color por defecto que pase quien llama; mal formado → error.</summary>
        public static IColor LeerColor(JToken t, string nombre, int rDef, int gDef, int bDef)
        {
            if (t == null || t.Type == JTokenType.Null)
                return Rgb(rDef, gDef, bDef);

            JArray arr = t as JArray;
            if (arr != null)
            {
                if (arr.Count != 3)
                    throw new ArgumentException("'" + nombre + "' debe ser [R, G, B] con TRES valores 0-255"
                        + " (o \"#RRGGBB\"). Recibido: " + Texto(t));
                var canal = new int[3];
                for (int i = 0; i < 3; i++)
                {
                    if (arr[i].Type != JTokenType.Integer && arr[i].Type != JTokenType.Float)
                        throw new ArgumentException("'" + nombre + "[" + i + "]' no es un número: " + Texto(arr[i]));
                    double v = (double)arr[i];
                    if (v < 0 || v > 255)
                        throw new ArgumentException("'" + nombre + "' va como [R, G, B] con valores 0-255."
                            + " Recibido: " + Texto(t));
                    canal[i] = (int)Math.Round(v);
                }
                return Rgb(canal[0], canal[1], canal[2]);
            }

            if (t.Type == JTokenType.String)
            {
                string s = ((string)t ?? "").Trim();
                if (s.StartsWith("#", StringComparison.Ordinal))
                    s = s.Substring(1);
                int rgb;
                if (s.Length == 6 && int.TryParse(s, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out rgb))
                    return Rgb((rgb >> 16) & 0xFF, (rgb >> 8) & 0xFF, rgb & 0xFF);
                throw new ArgumentException("'" + nombre + "' no es un color válido: " + Texto(t)
                    + ". Usa [R, G, B] con valores 0-255 o \"#RRGGBB\".");
            }

            throw new ArgumentException("'" + nombre + "' debe ser [R, G, B] con valores 0-255"
                + " o \"#RRGGBB\". Recibido: " + Texto(t));
        }

        private static IColor Rgb(int r, int g, int b)
        {
            return new RgbColorClass { Red = r, Green = g, Blue = b };
        }

        /// <summary>Entero del contrato JSON con null-check y rango. Un JSON null
        /// vale como "no lo mando" (el relay manda null en los opcionales), no como
        /// error de tipo.</summary>
        public static int LeerEntero(JToken t, string nombre, int porDefecto, int min, int max)
        {
            if (t == null || t.Type == JTokenType.Null)
                return ComprobarRango(porDefecto, nombre, min, max);
            if (t.Type != JTokenType.Integer && t.Type != JTokenType.Float)
                throw new ArgumentException("'" + nombre + "' debe ser un número entero. Recibido: " + Texto(t));
            double v = (double)t;
            // El rango se comprueba en double a propósito: un valor absurdamente
            // grande desbordaría el cast a int antes de poder rechazarlo.
            if (v < min || v > max)
                throw new ArgumentException("'" + nombre + "' debe estar entre " + min + " y " + max
                    + ". Recibido: " + Texto(t));
            return (int)Math.Round(v);
        }

        private static int ComprobarRango(int v, string nombre, int min, int max)
        {
            if (v < min || v > max)
                throw new ArgumentException("'" + nombre + "' debe estar entre " + min + " y " + max
                    + ". Recibido: " + v);
            return v;
        }

        public static bool LeerBool(JToken t, string nombre, bool porDefecto)
        {
            if (t == null || t.Type == JTokenType.Null)
                return porDefecto;
            if (t.Type != JTokenType.Boolean)
                throw new ArgumentException("'" + nombre + "' debe ser true o false. Recibido: " + Texto(t));
            return (bool)t;
        }

        /// <summary>
        /// Ruta de un fichero de salida: absoluta, con carpeta existente y con una
        /// de las extensiones válidas del formato. Si la extensión no es ninguna de
        /// ellas se añade la canónica (la primera de la lista) — pero "plano.jpeg"
        /// NO acaba en "plano.jpeg.jpg", porque .jpeg es válida para ese formato.
        ///
        /// La ruta relativa se rechaza en vez de resolverse: se resolvería contra el
        /// directorio de trabajo de ArcMap, que nadie controla ni ve.
        /// </summary>
        public static string RutaDeSalida(JToken t, string nombre, string[] extensiones)
        {
            string salida = t != null && t.Type != JTokenType.Null ? (string)t : null;
            if (string.IsNullOrEmpty(salida))
                throw new ArgumentException("Indica '" + nombre + "' (ruta del archivo de destino).");
            salida = salida.Trim();
            if (!Path.IsPathRooted(salida))
                throw new ArgumentException("'" + nombre + "' debe ser una ruta ABSOLUTA (ej. "
                    + @"D:\salidas\plano" + extensiones[0] + "). Recibido: " + salida);

            string ext = Path.GetExtension(salida);
            bool valida = false;
            foreach (string e in extensiones)
                if (string.Equals(ext, e, StringComparison.OrdinalIgnoreCase))
                    valida = true;
            if (!valida)
                salida += extensiones[0];

            string carpeta = Path.GetDirectoryName(salida);
            if (!string.IsNullOrEmpty(carpeta) && !Directory.Exists(carpeta))
                throw new ArgumentException("La carpeta de salida no existe: " + carpeta);
            return salida;
        }

        /// <summary>Devuelve si el destino YA existía (para decirlo en la respuesta)
        /// y corta si existía y no se autorizó sobrescribir.</summary>
        public static bool ComprobarSobrescritura(string ruta, bool sobrescribir, string nombreParam)
        {
            bool existe = File.Exists(ruta);
            if (existe && !sobrescribir)
                throw new ArgumentException("Ya existe un archivo en " + ruta + ". Cambia la ruta o pasa '"
                    + nombreParam + "': true para reemplazarlo.");
            return existe;
        }

        private static string Texto(JToken t)
        {
            return t == null ? "null" : t.ToString(Newtonsoft.Json.Formatting.None);
        }
    }
}
