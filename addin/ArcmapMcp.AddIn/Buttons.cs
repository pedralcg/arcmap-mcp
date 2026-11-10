using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;
using AddInComboBox = ESRI.ArcGIS.Desktop.AddIns.ComboBox;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Desplegable único con las cinco acciones del puente MCP.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <b>Por qué un desplegable y no cinco botones.</b> Los botones de add-in no se
    /// pueden retraducir en caliente: <c>Button.set_Caption</c> solo escribe un campo
    /// privado (<c>stfld</c>), ArcMap lee ese texto una vez al construir el control y
    /// después dibuja con lo que guardó. Se comprobó asignando <c>Caption</c> en cada
    /// <c>OnUpdate</c> sin comparar antes: la pantalla seguía en el idioma de arranque.
    /// Un desplegable, en cambio, se rehace con <c>Clear</c> + <c>Add</c> y ArcMap sí
    /// redibuja el ítem seleccionado, así que es la única forma de cumplir "cambiar de
    /// idioma sin reiniciar ArcMap".
    /// </para>
    /// <para>
    /// El primer ítem es siempre el rótulo ("Acciones" / "操作"): lo que se ve cerrado.
    /// Al elegir una acción se ejecuta y la selección vuelve al rótulo, de modo que el
    /// desplegable se comporta como un menú y nunca queda marcado en una acción.
    /// </para>
    /// </remarks>
    public class AccionComboBox : DesplegableTraducible
    {
        // Textos del add-in original para el rótulo, tomados de config.xml.
        private readonly string _rotuloOriginal;

        // Índices dentro de la lista: 0 = rótulo, 1..5 = las cinco acciones.
        private const int IndiceRotulo = 0;

        protected override string ClaveIdioma { get { return "combo.accion"; } }

        public AccionComboBox()
        {
            _rotuloOriginal = IdiomaCfg.TOriginal("combo.accion.rotulo");
            Repoblar();
        }

        /// <summary>
        /// Textos de la lista en el idioma pedido: rótulo + las cinco acciones.
        /// </summary>
        private string[] Textos(Idioma idioma)
        {
            List<string> textos = new List<string>(6);

            if (idioma == Idioma.Espanol)
                textos.Add(_rotuloOriginal);
            else
                textos.Add(IdiomaCfg.TEn(idioma, "combo.accion.caption"));

            // Iniciar, detener y estado comparten el diccionario con los antiguos
            // botones; reportar y acerca de tienen su propia clave porque aquí su
            // rótulo es más corto (no caben en el ancho del desplegable).
            textos.Add(IdiomaCfg.TEn(idioma, "btn.start.caption"));
            textos.Add(IdiomaCfg.TEn(idioma, "btn.stop.caption"));
            textos.Add(IdiomaCfg.TEn(idioma, "btn.status.caption"));
            textos.Add(IdiomaCfg.TEn(idioma, "combo.accion.report"));
            textos.Add(IdiomaCfg.TEn(idioma, "combo.accion.about"));

            return textos.ToArray();
        }

        protected override string[] TextosEsperados()
        {
            return Textos(IdiomaCfg.Actual);
        }

        protected override void Repoblar()
        {
            string[] textos = Textos(IdiomaCfg.Actual);

            // Se conserva el índice seleccionado si era válido; el rótulo si no.
            int indice = IndiceSeleccionado();
            if (indice < 0 || indice >= textos.Length) indice = IndiceRotulo;

            RepoblarCon(delegate
            {
                for (int i = 0; i < textos.Length; i++) AgregarItem(textos[i]);
            }, indice);
        }

        protected override void OnSelChange(int cookie)
        {
            if (!EsEventoDeUsuario(cookie)) return;

            int indice = IndiceDeCookie(cookie);
            if (indice <= IndiceRotulo)
            {
                // Rótulo elegido a propósito (o eco): no hay nada que ejecutar.
                return;
            }

            AccionesMcp.Accion accion = (AccionesMcp.Accion)(indice - 1);

            // Volver al rótulo ANTES de ejecutar: las acciones abren diálogos modales y,
            // si la selección siguiera en la acción, al cerrar el diálogo ArcMap
            // re-dispararía OnSelChange y la ejecutaría otra vez.
            SeleccionarIndice(IndiceRotulo);

            AccionesMcp.Ejecutar(accion);
        }

        protected override void OnUpdate()
        {
            SincronizarIdioma();
            Enabled = true;
        }
    }

    /// <summary>
    /// Autoarranque del puente, como DESPLEGABLE y no como botón: el icono de un
    /// botón no se puede repintar en caliente (la clase Button de los add-ins solo
    /// expone Caption, Message, Tooltip, Enabled y Checked) y el "pulsado" de
    /// Checked se lee mal en la barra. Un desplegable enseña el valor en texto,
    /// siempre visible y sin configurar nada.
    /// </summary>
    public class AutoStartComboBox : DesplegableTraducible
    {
        // Textos del add-in original en español. Se guardan una vez y se usan siempre
        // que el idioma activo sea español, para no depender del diccionario.
        private readonly string _textoNoOriginal;
        private readonly string _textoSiOriginal;

        protected override string ClaveIdioma { get { return "combo.auto"; } }

        public AutoStartComboBox()
        {
            // La lista es cerrada (editable="false" en Config.xml): solo dos valores.
            _textoNoOriginal = IdiomaCfg.TOriginal("combo.auto.no");
            _textoSiOriginal = IdiomaCfg.TOriginal("combo.auto.si");
            Repoblar();
        }

        /// <summary>
        /// Los dos textos del idioma activo, en orden No / Sí. En español salen de
        /// Config.xml (guardados al construir); en cualquier otro, del diccionario.
        /// </summary>
        private string[] Textos(Idioma idioma)
        {
            if (idioma == Idioma.Espanol)
                return new[] { _textoNoOriginal, _textoSiOriginal };

            return new[]
            {
                IdiomaCfg.TEn(idioma, "combo.auto.no"),
                IdiomaCfg.TEn(idioma, "combo.auto.si"),
            };
        }

        protected override string[] TextosEsperados()
        {
            return Textos(IdiomaCfg.Actual);
        }

        /// <summary>
        /// Vacía y repuebla la lista con los textos del idioma activo, dejando seleccionado
        /// el que dice la preferencia.
        /// </summary>
        /// <remarks>
        /// ComboBox de los add-ins no tiene <c>Update</c>: la única forma de cambiar el
        /// texto de un ítem es <c>Clear</c> + <c>Add</c>, y por eso las cookies se
        /// recalculan aquí. El texto visible del control es el del ítem seleccionado.
        /// </remarks>
        protected override void Repoblar()
        {
            string[] textos = Textos(IdiomaCfg.Actual);
            int indice = Ajustes.Autoarranque ? 1 : 0;
            RepoblarCon(delegate
            {
                AgregarItem(textos[0]);
                AgregarItem(textos[1]);
            }, indice);
        }

        /// <summary>Refleja en el desplegable lo que dice el registro, sin que el cambio
        /// de selección se confunda con una elección del usuario.</summary>
        private void SincronizarConPreferencia()
        {
            SeleccionarIndice(Ajustes.Autoarranque ? 1 : 0);
        }

        protected override void OnSelChange(int cookie)
        {
            if (!EsEventoDeUsuario(cookie)) return;

            bool activar = IndiceDeCookie(cookie) == 1;
            Ajustes.Autoarranque = activar;
            Log.Info("Autoarranque cambiado por el usuario a " + (activar ? "SI" : "NO"));

            string aviso;
            if (activar)
            {
                // Coherencia inmediata: si se pide autoarranque, el puente debe estar
                // ya escuchando en esta sesión, no solo en la siguiente.
                string error = McpExtension.Instance != null ? McpExtension.Instance.StartServer() : null;
                aviso = IdiomaCfg.T("auto.activado",
                    error == null
                        ? IdiomaCfg.T("auto.activado.ahora", McpServer.Port)
                        : IdiomaCfg.T("auto.activado.aviso", error));
            }
            else
            {
                aviso = IdiomaCfg.T("auto.desactivado", IdiomaCfg.T(ClaveIdioma + ".caption"));
            }

            // El MessageBox NO puede mostrarse aquí dentro: al cerrarse un diálogo
            // modal, el ComboBox de ArcMap revierte su selección al primer ítem y
            // vuelve a disparar OnSelChange (con "No"), pisando la preferencia recién
            // guardada. Se pospone al message pump, cuando el combo ya ha consolidado
            // su selección, y la reafirmación posterior vuelve a poner el texto que
            // dice el registro.
            StaDispatcher.Post(delegate
            {
                try { MessageBox.Show(aviso, IdiomaCfg.T("auto.titulo")); }
                finally { SincronizarConPreferencia(); }
            });
        }

        protected override void OnUpdate()
        {
            SincronizarIdioma();
            Enabled = true;
        }
    }

    /// <summary>
    /// Selector de idioma de la interfaz. Igual que el autoarranque, es un desplegable
    /// y no un botón: el valor actual se ve siempre en texto, sin abrir nada.
    /// </summary>
    /// <remarks>
    /// Al cambiar de idioma se persiste la preferencia y se retraduce toda la barra en
    /// caliente, sin reiniciar ArcMap. El aviso se pospone al message pump por el mismo
    /// motivo que en el autoarranque: un diálogo modal abierto desde OnSelChange hace
    /// que ArcMap revierta la selección del combo y vuelva a disparar el evento.
    /// </remarks>
    public class CambioIdiomaComboBox : DesplegableTraducible
    {
        protected override string ClaveIdioma { get { return "combo.lang"; } }

        public CambioIdiomaComboBox()
        {
            Repoblar();
        }

        /// <summary>
        /// Texto visible de cada opción: el rótulo sigue al idioma activo, pero el nombre
        /// del idioma NO se traduce nunca. Un usuario que no entiende el idioma actual
        /// tiene que poder reconocer el suyo en la lista, y "西班牙语" no le dice nada a
        /// nadie que hable español: cada nombre va escrito en su propia lengua.
        /// </summary>
        /// <remarks>
        /// Orden fijo: español primero, porque es el idioma original del add-in y el que
        /// ve quien no ha elegido nada. El índice 1 es el chino, y de ahí que
        /// <see cref="OnSelChange"/> traduzca índice a idioma en lugar de comparar cookies.
        /// </remarks>
        private static string[] Textos(Idioma idioma)
        {
            return new[]
            {
                IdiomaCfg.TEn(idioma, "combo.lang.es"),
                IdiomaCfg.TEn(idioma, "combo.lang.zh"),
            };
        }

        protected override string[] TextosEsperados()
        {
            return Textos(IdiomaCfg.Actual);
        }

        /// <summary>
        /// Vacía y repuebla la lista con el rótulo del idioma activo. Los nombres de los
        /// idiomas se muestran en su propia lengua, que es lo que busca quien no entiende
        /// la actual; solo cambia el rótulo.
        /// </summary>
        protected override void Repoblar()
        {
            string[] textos = Textos(IdiomaCfg.Actual);
            int indice = IdiomaCfg.Actual == Idioma.Chino ? 1 : 0;
            RepoblarCon(delegate
            {
                AgregarItem(textos[0]);
                AgregarItem(textos[1]);
            }, indice);
        }

        protected override void OnSelChange(int cookie)
        {
            if (!EsEventoDeUsuario(cookie)) return;

            Idioma nuevo = IndiceDeCookie(cookie) == 1 ? Idioma.Chino : Idioma.Espanol;
            if (nuevo == IdiomaCfg.Actual)
            {
                // Nada que hacer, pero el texto visible debe seguir al idioma activo.
                SeleccionarIndice(IdiomaCfg.Actual == Idioma.Chino ? 1 : 0);
                return;
            }

            // 1. Persistir y activar. La barra se retraduce sola: cada control compara
            //    en su OnUpdate lo que muestra con lo que debería mostrar y se actualiza.
            //    Así no hace falta ningún registro de instancias que pueda quedar muerto.
            Ajustes.Idioma = IdiomaCfg.Codigo(nuevo);
            IdiomaCfg.Establecer(nuevo);
            Log.Info("Idioma cambiado por el usuario a '" + IdiomaCfg.Codigo(nuevo) + "'");

            // Fuerza un repintado inmediato para que el cambio se vea al instante:
            // la API de add-ins no expone Refresh, pero IApplication sí, y es la vía
            // documentada para que ArcMap vuelva a pedir OnUpdate a cada control.
            BarraUi.Refrescar();

            // El aviso va en el idioma de DESTINO, no en el que había: es lo que confirma
            // al usuario que el cambio funcionó.
            string aviso = IdiomaCfg.AvisoCambioIdioma(nuevo);

            // Mismo aplazamiento que en el autoarranque: ver el comentario de allí.
            StaDispatcher.Post(delegate
            {
                try { MessageBox.Show(aviso, IdiomaCfg.T("lang.titulo")); }
                finally
                {
                    // Reafirma el rótulo en el idioma nuevo; sin esto, si ArcMap revierte
                    // la selección tras el diálogo, el control mostraría el rótulo viejo
                    // hasta el siguiente repintado.
                    SeleccionarIndice(IdiomaCfg.Actual == Idioma.Chino ? 1 : 0);
                }
            });
        }

        protected override void OnUpdate()
        {
            SincronizarIdioma();
            Enabled = true;
        }
    }

    /// <summary>
    /// Pide a ArcMap que repinte la interfaz, con lo que vuelve a llamar a OnUpdate de
    /// cada control y los textos se actualizan sin esperar a que el usuario interactúe.
    /// </summary>
    internal static class BarraUi
    {
        public static void Refrescar()
        {
            try
            {
                var app = Handlers.ArcSession.App();
                if (app != null) app.RefreshWindow();
            }
            catch (Exception ex)
            {
                // Sin ArcMap vivo no hay nada que repintar: no es un error.
                Log.Warn("No se pudo refrescar la barra: " + ex.Message);
            }
        }
    }
}
