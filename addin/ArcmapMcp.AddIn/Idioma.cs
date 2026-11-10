using System;
using System.Collections.Generic;
using System.Globalization;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Idiomas que entiende la interfaz del add-in.
    /// </summary>
    internal enum Idioma
    {
        Espanol = 0,
        Chino = 1,
    }

    /// <summary>
    /// Traducciones de los textos de la interfaz (barra, diálogos y mensajes).
    /// </summary>
    /// <remarks>
    /// <para>
    /// El idioma por defecto sigue siendo el español: quien no toque nada ve exactamente
    /// lo de siempre. La preferencia se guarda por usuario con <see cref="Ajustes.Idioma"/>
    /// y se cambia desde el desplegable de la propia barra, sin reiniciar ArcMap.
    /// </para>
    /// <para>
    /// Qué NO se traduce, a propósito:
    /// </para>
    /// <list type="bullet">
    ///   <item>Los identificadores del protocolo MCP y los nombres de herramientas
    ///   (<c>ping</c>, <c>list_layers</c>, <c>execute_arcpy</c>…): son contrato con el
    ///   servidor Python y el agente, no interfaz.</item>
    ///   <item>Los nombres de variables de entorno (<c>ARCMAP_BRIDGE_PORT</c>…), rutas,
    ///   nombres de fichero y parámetros de los handlers.</item>
    ///   <item>El contenido del log: es diagnóstico para el mantenedor, y tenerlo en un
    ///   solo idioma hace que dos usuarios puedan comparar trazas.</item>
    /// </list>
    /// </remarks>
    internal static class IdiomaCfg
    {
        /// <summary>Idioma que se usa si no hay preferencia guardada.</summary>
        public const string PorDefecto = "es";

        private static Idioma _actual = Idioma.Espanol;

        /// <summary>Idioma activo.</summary>
        public static Idioma Actual
        {
            get { return _actual; }
        }

        /// <summary>
        /// Cambia el idioma activo. No persiste nada: de eso se encarga el llamador
        /// (el desplegable de la barra) para que este módulo pueda usarse también en
        /// contextos donde no se quiere tocar el registro.
        /// </summary>
        public static void Establecer(Idioma idioma)
        {
            _actual = idioma;
        }

        /// <summary>Carga el idioma guardado en la preferencia del usuario.</summary>
        public static Idioma CargarPreferencia()
        {
            _actual = DesdeCodigo(Ajustes.Idioma);
            return _actual;
        }

        /// <summary>
        /// Convierte el identificador guardado ("es" / "zh") al enum, tolerando
        /// variantes habituales ("zh-CN", "es-ES", mayúsculas) y cayendo al idioma por
        /// defecto ante cualquier valor desconocido o vacío.
        /// </summary>
        public static Idioma DesdeCodigo(string codigo)
        {
            string norm = Normalizar(codigo);
            return norm == "zh" ? Idioma.Chino : Idioma.Espanol;
        }

        /// <summary>Devuelve el identificador canónico ("es" / "zh") de un valor libre.</summary>
        public static string Normalizar(string codigo)
        {
            if (string.IsNullOrEmpty(codigo)) return PorDefecto;
            string c = codigo.Trim().ToLowerInvariant();
            if (c.StartsWith("zh", StringComparison.Ordinal)) return "zh";
            if (c.StartsWith("es", StringComparison.Ordinal)) return "es";
            return PorDefecto;
        }

        /// <summary>¿Está la interfaz en español (el idioma original del add-in)?</summary>
        public static bool EsEspanol
        {
            get { return _actual == Idioma.Espanol; }
        }

        /// <summary>Identificador canónico de un idioma.</summary>
        public static string Codigo(Idioma idioma)
        {
            return idioma == Idioma.Chino ? "zh" : "es";
        }

        /// <summary>
        /// Idioma deducido de la configuración regional de Windows. Se usa solo para
        /// decidir el valor inicial la primera vez, cuando aún no hay preferencia.
        /// </summary>
        public static Idioma DetectarDelSistema()
        {
            try
            {
                CultureInfo ci = CultureInfo.CurrentUICulture
                                 ?? CultureInfo.CurrentCulture
                                 ?? CultureInfo.InstalledUICulture;
                if (ci != null && ci.TwoLetterISOLanguageName == "zh")
                    return Idioma.Chino;
            }
            catch (Exception ex)
            {
                Log.Warn("No se pudo detectar el idioma del sistema: " + ex.Message);
            }
            return Idioma.Espanol;
        }

        // ------------------------------------------------------------------
        // Textos
        // ------------------------------------------------------------------

        private static readonly Dictionary<string, string[]> Textos =
            new Dictionary<string, string[]>(StringComparer.Ordinal)
        {
            // --- Barra de herramientas: botones ---
            { "btn.start.caption",     new[] { "Iniciar MCP",     "启动 MCP" } },
            { "btn.start.tip",         new[] { "Levantar el puente MCP (socket 127.0.0.1:27179)", "启动 MCP 桥（socket 127.0.0.1:27179）" } },
            { "btn.start.msg",         new[] { "Arranca el puente MCP (socket 127.0.0.1:27179).", "启动 MCP 桥（socket 127.0.0.1:27179）。" } },

            { "btn.stop.caption",      new[] { "Detener MCP",     "停止 MCP" } },
            { "btn.stop.tip",          new[] { "Detener el puente MCP", "停止 MCP 桥" } },
            { "btn.stop.msg",          new[] { "Detiene el puente MCP.", "停止 MCP 桥。" } },

            { "btn.status.caption",    new[] { "Estado MCP",      "MCP 状态" } },
            { "btn.status.tip",        new[] { "Ver estado del puente (ACTIVO / PARADO) y host:puerto", "查看桥状态（运行中 / 已停止）与 host:port" } },
            { "btn.status.msg",        new[] { "Muestra el estado del puente MCP.", "显示 MCP 桥状态。" } },

            { "btn.report.caption",    new[] { "Reportar",        "反馈问题" } },
            { "btn.report.tip",        new[] { "Reportar un problema: abre un issue de GitHub o un email con el diagnostico (sin datos de proyecto)", "反馈问题：打开 GitHub issue 或邮件，附带诊断信息（不含项目数据）" } },
            { "btn.report.msg",        new[] { "Reporta un problema (issue de GitHub o email) con el diagnostico.", "反馈问题（GitHub issue 或邮件），附带诊断信息。" } },

            { "btn.about.caption",     new[] { "Acerca de",       "关于" } },
            { "btn.about.tip",         new[] { "Ficha del autor: Pedro Alcoba Gómez - pedralcg.dev", "作者信息：Pedro Alcoba Gómez - pedralcg.dev" } },
            { "btn.about.msg",         new[] { "Informacion del autor de arcmap-mcp.", "arcmap-mcp 作者信息。" } },

            // --- Barra de herramientas: desplegables ---
            // El desplegable de acciones sustituye a los cinco botones: es el único
            // control cuyo texto ArcMap vuelve a leer tras Clear+Add, y por tanto el
            // único que puede cambiar de idioma sin reiniciar.
            { "combo.accion.caption",  new[] { "Acciones",        "操作" } },
            // Rótulo original, tal cual lo trae config.xml. Se guarda aparte porque en
            // español el desplegable debe mostrar EXACTAMENTE eso, no una copia.
            { "combo.accion.rotulo",   new[] { "Acciones",        "操作" } },
            { "combo.accion.tip",      new[] { "Acciones del puente MCP: iniciar, detener, estado, reportar, acerca de", "MCP 桥操作：启动、停止、状态、反馈、关于" } },
            { "combo.accion.msg",      new[] { "Elige la acción que quieres ejecutar sobre el puente MCP.", "选择要对 MCP 桥执行的操作。" } },
            { "combo.accion.report",   new[] { "Reportar",        "反馈问题" } },
            { "combo.accion.about",    new[] { "Acerca de",       "关于" } },

            { "combo.auto.caption",    new[] { "Autoarranque",    "自动启动" } },
            { "combo.auto.tip",        new[] { "Iniciar el puente automaticamente al abrir ArcMap (preferencia por usuario)", "打开 ArcMap 时自动启动桥（按用户保存的偏好）" } },
            { "combo.auto.msg",        new[] { "Elige si el puente MCP debe iniciarse solo al abrir ArcMap.", "选择是否在打开 ArcMap 时自动启动 MCP 桥。" } },
            { "combo.auto.no",         new[] { "Autoarranque: No", "自动启动：关" } },
            { "combo.auto.si",         new[] { "Autoarranque: Sí", "自动启动：开" } },
            { "combo.auto.hint",       new[] { "Autoarranque",    "自动启动" } },

            { "combo.lang.caption",    new[] { "Idioma",          "语言" } },
            { "combo.lang.tip",        new[] { "Idioma de la interfaz del add-in (preferencia por usuario)", "add-in 界面语言（按用户保存的偏好）" } },
            { "combo.lang.msg",        new[] { "Elige el idioma de la interfaz del add-in.", "选择 add-in 界面语言。" } },
            // Los nombres de idioma NO se traducen entre sí: cada uno va en su propia
            // lengua, para que quien no entiende el idioma actual reconozca el suyo.
            { "combo.lang.es",         new[] { "Idioma: Español",  "语言：Español" } },
            { "combo.lang.zh",         new[] { "Idioma: 中文",     "语言：中文" } },
            { "combo.lang.hint",       new[] { "Idioma",          "语言" } },

            // --- Diálogos de los botones ---
            { "msg.ext.noCargada",     new[] { "La extensión MCP no está cargada.", "MCP 扩展未加载。" } },
            { "msg.error.titulo",      new[] { "arcmap-mcp · Error", "arcmap-mcp · 错误" } },

            { "msg.start.ok", new[] {
                "El puente MCP está activo y escuchando en 127.0.0.1:{0}.\n"
                + "Mantén ArcMap abierto mientras lo utilizas.\n\n"
                + "pedralcg.dev",
                "MCP 桥已启动，正在监听 127.0.0.1:{0}。\n"
                + "使用期间请保持 ArcMap 打开。\n\n"
                + "pedralcg.dev" } },
            { "msg.start.titulo",      new[] { "arcmap-mcp · Puente iniciado", "arcmap-mcp · 桥已启动" } },
            { "msg.start.fallo",       new[] { "No se ha podido iniciar el puente:\n{0}", "无法启动桥：\n{0}" } },

            { "msg.stop.sinPuente",    new[] { "El puente MCP no está cargado; no hay nada que detener.", "MCP 桥未加载，没有可停止的对象。" } },
            { "msg.stop.tituloEstado", new[] { "arcmap-mcp · Estado del puente", "arcmap-mcp · 桥状态" } },
            { "msg.stop.ok",           new[] { "El puente MCP se ha detenido correctamente.", "MCP 桥已正常停止。" } },
            { "msg.stop.titulo",       new[] { "arcmap-mcp · Puente detenido", "arcmap-mcp · 桥已停止" } },

            // --- Ficha de estado ---
            { "estado.titulo",         new[] { "arcmap-mcp · Estado del puente", "arcmap-mcp · 桥状态" } },
            { "estado.puente",         new[] { "PUENTE", "桥" } },
            { "estado.estado",         new[] { "Estado", "状态" } },
            { "estado.activo",         new[] { "ACTIVO, escuchando", "运行中，正在监听" } },
            { "estado.parado",         new[] { "PARADO", "已停止" } },
            { "estado.direccion",      new[] { "Dirección", "地址" } },
            { "estado.addin",          new[] { "Add-in", "Add-in" } },
            { "estado.actualizacion",  new[] { "Actualización", "更新" } },
            { "estado.autoarranque",   new[] { "Autoarranque", "自动启动" } },
            { "estado.auto.si",        new[] { "SI (se inicia al abrir ArcMap)", "开（打开 ArcMap 时启动）" } },
            { "estado.auto.no",        new[] { "NO (hay que pulsar Iniciar)", "关（需手动点「启动 MCP」）" } },
            { "estado.sesion",         new[] { "SESIÓN", "会话" } },
            { "estado.documento",      new[] { "Documento", "文档" } },
            { "estado.desde",          new[] { "Desde", "起始时间" } },
            { "estado.peticiones",     new[] { "Peticiones", "请求数" } },
            { "estado.ultimo",         new[] { "Último", "最近命令" } },
            { "estado.errores",        new[] { "Errores", "错误数" } },
            { "estado.entorno",        new[] { "ENTORNO", "环境" } },
            { "estado.python",         new[] { "Python arcpy", "Python arcpy" } },
            { "estado.log",            new[] { "Log", "日志" } },
            { "estado.preguntaLog",    new[] { "¿Abrir el log ahora?", "现在打开日志？" } },

            { "estado.sinDocumento",   new[] { "(sin documento)", "（无文档）" } },
            { "estado.noAccesible",    new[] { "(no accesible: {0})", "（无法访问：{0}）" } },
            { "estado.docInfo",        new[] { "{0}  ·  mapa {1}, {2} capas", "{0}  ·  地图 {1}，{2} 个图层" } },
            { "estado.sinLog",         new[] { "Todavía no hay fichero de log en {0}", "日志文件尚未生成：{0}" } },
            { "estado.log.fallo",      new[] { "No se ha podido abrir el log:\n{0}", "无法打开日志：\n{0}" } },
            { "estado.sinComando",     new[] { "(ninguno todavía)", "（尚未收到）" } },

            // --- Estado de actualización (Diagnostico.DescribirActualizacion) ---
            { "actualizacion.nueva",       new[] { "hay v{0} disponible", "有新版 v{0} 可用" } },
            { "actualizacion.aldia",       new[] { "al día (última comprobada v{0})", "已是最新（最近检查到 v{0}）" } },
            { "actualizacion.sinComprobar", new[] { "sin comprobar todavía", "尚未检查" } },

            // --- Diálogo de autoarranque ---
            { "auto.titulo",           new[] { "arcmap-mcp · Autoarranque", "arcmap-mcp · 自动启动" } },
            { "auto.activado", new[] {
                "Autoarranque ACTIVADO.\n\n"
                + "El puente MCP se levantará solo cada vez que abras ArcMap.\n"
                + "{0}",
                "自动启动已开启。\n\n"
                + "此后每次打开 ArcMap 都会自动启动 MCP 桥。\n"
                + "{0}" } },
            { "auto.activado.ahora",   new[] { "En esta sesión ya está escuchando en 127.0.0.1:{0}.", "本次会话已在 127.0.0.1:{0} 监听。" } },
            { "auto.activado.aviso",   new[] { "Aviso: no se ha podido iniciar ahora mismo:\n{0}", "提示：当前无法立即启动：\n{0}" } },
            { "auto.desactivado", new[] {
                "Autoarranque DESACTIVADO.\n\n"
                + "A partir de la próxima sesión tendrás que pulsar \"{0}\".\n"
                + "El puente actual sigue activo hasta que lo detengas.",
                "自动启动已关闭。\n\n"
                + "下次启动时需要手动点击「{0}」。\n"
                + "当前桥仍然运行，直到你手动停止。" } },

            // --- Desplegable de idioma ---
            { "lang.titulo",           new[] { "arcmap-mcp · Idioma", "arcmap-mcp · 语言" } },
            { "lang.cambiado.es",      new[] { "La interfaz del add-in ahora está en español.", "add-in 界面已切换为西班牙语。" } },
            { "lang.cambiado.zh",      new[] { "La interfaz del add-in ahora está en chino.", "add-in 界面已切换为中文。" } },
        };

        /// <summary>
        /// Aviso de cambio de idioma, escrito en <b>el idioma de destino</b>: el mensaje
        /// tiene que confirmarle al usuario que el cambio funcionó, y para eso lo lee en
        /// el idioma que acaba de elegir.
        /// </summary>
        public static string AvisoCambioIdioma(Idioma destino)
        {
            return destino == Idioma.Chino ? T("lang.cambiado.zh") : T("lang.cambiado.es");
        }

        /// <summary>
        /// Texto ORIGINAL del add-in, siempre en español, independientemente del idioma
        /// activo.
        /// </summary>
        /// <remarks>
        /// Se usa para guardar los textos de fábrica antes de traducirlos y poder
        /// restaurarlos al volver al español. Es la red de seguridad que hace que una
        /// errata en el diccionario nunca pueda alterar la versión original.
        /// </remarks>
        public static string TOriginal(string clave)
        {
            string[] par;
            if (clave == null || !Textos.TryGetValue(clave, out par) || par == null || par.Length == 0)
                return clave;
            return par[0] ?? clave;
        }

        /// <summary>
        /// Texto traducido al idioma activo. Si la clave no existe devuelve la propia
        /// clave, para que un olvido se note en pantalla en vez de fallar en silencio.
        /// </summary>
        public static string T(string clave)
        {
            return T(clave, null);
        }

        /// <summary>
        /// Texto de una clave en un idioma CONCRETO, sin tocar el idioma activo.
        /// </summary>
        /// <remarks>
        /// Un desplegable que se repobla necesita saber "qué dice esta clave en el idioma
        /// que voy a mostrar" sin cambiar el idioma global: cambiarlo y restaurarlo con
        /// <see cref="Establecer"/> sería un efecto lateral que cualquier callback del
        /// message pump podría observar a mitad.
        /// </remarks>
        public static string TEn(Idioma idioma, string clave)
        {
            string[] par;
            if (clave == null || !Textos.TryGetValue(clave, out par) || par == null || par.Length == 0)
                return clave;

            int i = idioma == Idioma.Chino ? 1 : 0;
            if (i >= par.Length || par[i] == null) i = 0;
            return par[i] ?? clave;
        }

        /// <summary>
        /// Texto traducido con sustitución de argumentos al estilo string.Format
        /// (<c>{0}</c>, <c>{1}</c>…).
        /// </summary>
        public static string T(string clave, params object[] args)
        {
            string texto = Buscar(clave, args != null && args.Length > 0);
            if (texto == null) return clave;

            if (args != null && args.Length > 0)
            {
                try
                {
                    return string.Format(CultureInfo.InvariantCulture, texto, args);
                }
                catch (FormatException ex)
                {
                    // Plantilla mal escrita: devolver el texto sin formatear es mejor
                    // que lanzar desde un manejador de la barra de ArcMap.
                    Log.Warn("Plantilla de texto inválida para '" + clave + "': " + ex.Message);
                    return texto;
                }
            }
            return texto;
        }

        private static string Buscar(string clave, bool requiereFormato)
        {
            string[] par;
            if (clave == null || !Textos.TryGetValue(clave, out par) || par == null)
                return null;

            int i = _actual == Idioma.Chino ? 1 : 0;
            if (i >= par.Length || par[i] == null)
            {
                // Falta una traducción: se cae al español en vez de mostrar la clave
                // en inglés, que aquí no existe.
                i = 0;
                if (par.Length == 0 || par[0] == null) return null;
            }
            return par[i];
        }
    }
}
