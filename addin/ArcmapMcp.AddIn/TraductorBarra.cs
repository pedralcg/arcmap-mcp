using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;

namespace ArcmapMcp.AddIn
{
    /// <summary>
    /// Base de los desplegables de la barra que saben cambiar de idioma en caliente.
    /// </summary>
    /// <remarks>
    /// <para>
    /// <b>Por qué todo son desplegables.</b> En las clases de add-in,
    /// <c>Button.set_Caption</c> solo escribe un campo privado (su IL es un <c>stfld</c> y
    /// un <c>ret</c>): ArcMap lee ese texto una única vez, al construir el control, y luego
    /// dibuja con lo que guardó. Se comprobó asignando <c>Caption</c> en cada
    /// <c>OnUpdate</c>, sin comparar antes: la pantalla seguía en el idioma de arranque.
    /// Un <c>ComboBox</c>, en cambio, se rehace con <c>Clear</c> + <c>Add</c> y ArcMap
    /// redibuja el ítem seleccionado. De ahí que las acciones del puente sean también un
    /// desplegable.
    /// </para>
    /// <para>
    /// <b>Sincronizar es idempotente y sin estado compartido.</b> Cada control compara los
    /// textos que muestra con los que le tocan en el idioma activo y solo repuebla si
    /// difieren. No hay bandera de "último idioma aplicado": ArcMap instancia los controles
    /// más de una vez por arranque y los reconstruye al personalizar la barra, así que una
    /// instancia nueva heredaba la bandera de otra y se saltaba su primera traducción.
    /// </para>
    /// <para>
    /// <b>Por qué no basta una bandera <c>Repoblando</c>:</b> al rellenar el control,
    /// ArcMap encola un cambio de selección que puede llegar <i>después</i> de que la
    /// bandera ya se haya desactivado. Ese evento fantasma llegaba a la lógica del
    /// autoarranque y escribía la preferencia con la cookie equivocada —de ahí el
    /// "Autoarranque: Sí" que aparecía solo al cambiar de idioma—. Por eso, además de la
    /// bandera, cada evento se valida contra las cookies vigentes.
    /// </para>
    /// <para>
    /// <b>Hechos de la API (verificados por reflexión contra Desktop 10.8):</b>
    /// <c>ComboBox</c> expone <c>Add</c>/<c>Clear</c>/<c>Select(cookie)</c>/<c>Remove</c>,
    /// la colección de solo lectura <c>items</c> (<c>ComboBox.Item</c> con <c>Caption</c>,
    /// <c>Cookie</c>, <c>Tag</c>) y <c>Selected</c>, que es la cookie seleccionada. No
    /// existen <c>Count</c>, <c>SelectedIndex</c> ni <c>Text(i)</c>.
    /// </para>
    /// </remarks>
    public abstract class DesplegableTraducible : ESRI.ArcGIS.Desktop.AddIns.ComboBox
    {
        private bool _repoblando;

        // Cookies de los ítems vigentes, en el orden en que se crearon. Es la única
        // fuente de verdad de OnSelChange: una cookie que no esté aquí es de una lista
        // anterior (Clear + Add las regenera) y no puede representar una elección.
        private int[] _cookies = new int[0];
        private List<int> _pendientes;
        private bool _construido;

        // Contador y no bandera: la supresión puede anidarse (una selección dentro de un
        // EvitarEvento) y una bandera se desactivaría antes de tiempo.
        private int _supresion;

        /// <summary>Clave base en el diccionario, p. ej. "combo.auto".</summary>
        protected abstract string ClaveIdioma { get; }

        /// <summary>
        /// Textos que deben verse ahora, en orden, con el idioma activo. Incluye el
        /// rótulo ("Idioma: Español"), porque un desplegable muestra el ítem
        /// seleccionado, no una etiqueta aparte.
        /// </summary>
        protected abstract string[] TextosEsperados();

        /// <summary>
        /// ¿Está el control en construcción (o se está repoblando ahora mismo)? Mientras
        /// dure, los eventos de selección no son del usuario y deben ignorarse.
        /// </summary>
        protected bool Construyendo
        {
            get { return _repoblando || !_construido; }
        }

        /// <summary>Repuebla los ítems en el idioma activo. Lo implementa cada subclase.</summary>
        protected abstract void Repoblar();

        /// <summary>Número de ítems de la lista vigente.</summary>
        protected int Total
        {
            get { return _cookies.Length; }
        }

        /// <summary>¿Los ítems que se ven son exactamente los del idioma activo?</summary>
        private bool EstaTraducido()
        {
            if (!_construido) return false;

            try
            {
                string[] esperados = TextosEsperados();
                ReadOnlyCollection<ESRI.ArcGIS.Desktop.AddIns.ComboBox.Item> actuales = items;
                if (esperados == null || actuales == null || actuales.Count != esperados.Length)
                    return false;

                for (int i = 0; i < esperados.Length; i++)
                {
                    if (actuales[i] == null || actuales[i].Caption != esperados[i]) return false;
                }
                return true;
            }
            catch (Exception ex)
            {
                Log.Warn("No se pudo comprobar la traducción de '" + ClaveIdioma + "': " + ex.Message);
                return false;
            }
        }

        /// <summary>Retraduce si hace falta. Lo llaman las subclases desde OnUpdate.</summary>
        protected void SincronizarIdioma()
        {
            if (EstaTraducido()) return;
            try { Repoblar(); }
            catch (Exception ex)
            {
                Log.Warn("No se pudo retraducir '" + ClaveIdioma + "': " + ex.Message);
            }
        }

        /// <summary>
        /// Vacía la lista, la llena con <paramref name="llenar"/> (que debe usar
        /// <see cref="AgregarItem"/>) y deja seleccionado el ítem de índice
        /// <paramref name="indiceSeleccionado"/>.
        /// </summary>
        /// <remarks>
        /// Todo el repoblado ocurre con la lista marcada como "en construcción", y la
        /// selección final se reafirma sin que cuente como elección del usuario. Lo que
        /// ArcMap encole después de volver de aquí lo filtra la validación de cookie: una
        /// bandera booleana no puede cubrir ese caso, porque el mensaje llega al message
        /// pump cuando la bandera ya se apagó.
        /// </remarks>
        protected void RepoblarCon(Action llenar, int indiceSeleccionado)
        {
            bool antes = _repoblando;
            _repoblando = true;
            try
            {
                _pendientes = new List<int>(8);
                Clear();
                llenar();
            }
            catch (Exception ex)
            {
                Log.Warn("No se pudo repoblar '" + ClaveIdioma + "': " + ex.Message);
            }
            finally
            {
                TerminarRepoblado(indiceSeleccionado);
                _repoblando = antes;
                _construido = true;
            }
        }

        /// <summary>
        /// Añade un ítem y recuerda su cookie. Usar solo dentro de
        /// <see cref="RepoblarCon"/>.
        /// </summary>
        protected int AgregarItem(string texto)
        {
            int cookie = Add(texto);
            if (_pendientes != null) _pendientes.Add(cookie);
            return cookie;
        }

        private void TerminarRepoblado(int indiceSeleccionado)
        {
            List<int> acumuladas = _pendientes;
            _pendientes = null;

            if (acumuladas != null && acumuladas.Count > 0)
                _cookies = acumuladas.ToArray();

            if (_cookies.Length == 0) return;

            int i = indiceSeleccionado;
            if (i < 0) i = 0;
            if (i >= _cookies.Length) i = _cookies.Length - 1;
            SeleccionarIndice(i);
        }

        /// <summary>
        /// Deja seleccionado el ítem de índice <paramref name="indice"/> sin que el evento
        /// resultante cuente como una elección del usuario.
        /// </summary>
        protected void SeleccionarIndice(int indice)
        {
            if (indice < 0 || indice >= _cookies.Length) return;
            EvitarEvento(delegate
            {
                try { Select(_cookies[indice]); }
                catch (Exception ex)
                {
                    Log.Warn("No se pudo seleccionar en '" + ClaveIdioma + "': " + ex.Message);
                }
            });
        }

        /// <summary>
        /// Desactiva la reacción a OnSelChange durante <paramref name="accion"/>. El
        /// message pump sigue sin estar del todo a salvo (ArcMap puede encolar el cambio),
        /// de ahí que además se valide la cookie; esto solo cubre el caso inmediato.
        /// </summary>
        protected void EvitarEvento(Action accion)
        {
            int antes = _supresion;
            _supresion++;
            try { accion(); }
            finally { _supresion = antes; }
        }

        /// <summary>
        /// ¿La cookie que llega en OnSelChange es una elección del usuario y no un eco del
        /// propio control?
        /// </summary>
        protected bool EsEventoDeUsuario(int cookie)
        {
            if (_supresion > 0) return false;
            if (Construyendo) return false;
            if (cookie < 0) return false;

            // Una cookie de una lista anterior (Clear + Add las regenera) o el 0 que
            // ArcMap emite al vaciar el control no son elecciones del usuario.
            return IndiceDeCookie(cookie) >= 0;
        }

        /// <summary>Índice de la cookie dentro de la lista vigente, o -1.</summary>
        protected int IndiceDeCookie(int cookie)
        {
            for (int i = 0; i < _cookies.Length; i++)
            {
                if (_cookies[i] == cookie) return i;
            }
            return -1;
        }

        /// <summary>Índice del ítem seleccionado actualmente, o -1.</summary>
        protected int IndiceSeleccionado()
        {
            return IndiceDeCookie(Selected);
        }
    }
}
