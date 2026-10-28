# Catálogo de herramientas — arcmap-mcp

> **Filosofía híbrida.** `execute_arcpy` (código arbitrario) es la base universal:
> cualquier análisis de ArcMap 10.x se puede expresar con él. Los **wrappers** de esta
> lista existen solo para lo **repetitivo y de alto valor** —sobre todo las series de
> planos (Data Driven Pages)—, no para replicar toda la API.

**78 herramientas** sobre ArcMap 10.5. **56 están cubiertas por la regresión en sesión
viva** (`tests/regresion_sesion_viva.py`, 480 comprobaciones); el resto se ha ejercitado a
mano en trabajo real, pero **no automáticamente**. El README, en «Herramientas MCP», explica
por qué la distinción importa.

## Cómo se ejecuta cada herramienta (importa para lo que puedes esperar)

Hay **cuatro modos de ejecución**, y cada herramienta usa el que le corresponde. Los
tres primeros son del add-in .NET; el cuarto no pasa por el puente siquiera:

| Modo | Qué herramientas | Consecuencias |
|---|---|---|
| **Nativo en sesión viva** (ArcObjects, hilo STA) | capas, selección, layout, exports, navegación, `run_geoprocessing`, `calculate_geometry` | Opera sobre el documento **vivo**: los cambios se ven al instante. Ocupa la interfaz mientras dura (exports cancelables con **ESC**). |
| **Out-of-process sobre snapshot** (arcpy en Python 2.7 aparte) | `execute_arcpy`, `list_ddp`, `export_ddp`, `goto_ddp_page`, `raster_index`, `hydrology`, `contours`, `topographic_profile`, `least_cost_path` | Trabaja sobre una **copia temporal del .mxd** con el estado actual: lee el documento real, pero **sus cambios al documento no afectan a la sesión viva**. Las salidas a disco sí son reales, y los resultados de análisis se añaden al mapa al terminar. **No congela la interfaz.** Coste fijo de unos segundos por llamada (snapshot + arranque de Python). |
| **Solo lectura de datos** (cursores/Describe) | consultas, listados de workspace | Sin efectos secundarios. |
| **Sin ArcMap, leyendo del disco** | `describe_mxd`, `get_ole_tables`, `audit_folder`, `export_mxd_lote`, `edit_mxd_lote`, `compare_mxd` | **No pasan por el puente**: funcionan con ArcMap cerrado (y `export_mxd_lote` también con ArcMap abierto) y sobre documentos que no son el abierto. |

---

## Sin ArcMap abierto (inspección de ficheros)

Estas no hablan con el puente: trabajan sobre el disco (`edit_mxd_lote` además escribe).
Sirven precisamente cuando el camino normal falla, que es cuando más falta hace saber algo.

| Tool | Qué hace |
|---|---|
| `describe_mxd` | Versión declarada de un `.mxd` **sin abrirlo**, más un veredicto frente a la versión de ArcMap. Milisegundos, sin arcpy y sin licencia |
| `get_ole_tables` | Tablas de los objetos OLE incrustados en un `.mxd` (Word 97-2003 y 2007+) leídas **del fichero**: sin ArcMap ni Word. Solo los objetos que usan los marcos del layout (un `.mxd` acumula copias viejas). Las filas salen de la estructura del documento, con las celdas vacías en su sitio; si un objeto trae `aviso`, se dedujeron del texto y pueden salir desalineadas. Solo lectura: editarlas desde el puente no es viable |
| `audit_folder` | Inventario de TODOS los `.mxd` de una carpeta: versión, capas, fuentes rotas y definition queries, **separando lo que sale en el plano** (capa y grupos encendidos, marco dentro de la hoja) de lo que solo está en el documento |
| `edit_mxd_lote` | **Modo disco:** la misma lista de operaciones (herramientas vivas con sus parámetros: leyenda, etiquetas, simbología, textos, TOC, recorte) aplicada a varios `.mxd` y guardada, con los mismos handlers del add-in y sin abrirlos en ArcMap. Todo o nada por documento, `.bak`, y **verificado reabriendo**. Se niega con marcos OLE y con el documento abierto en ArcMap. No exporta: después, `export_mxd_lote` |
| `compare_mxd` | Compara un `.mxd` con su **plantilla** (u otro plano de la serie) y dice en qué se separa, por ámbitos: `general` (df activo y escala), `capas` (de más, de menos, orden, visibilidad, fuente, def. query, etiquetas, rotas), `simbologia` (clase a clase), `etiquetado`, `textos` (también los agrupados del cajetín) y `leyendas`. Los textos se emparejan por nombre o, sin él, por posición (`tolerancia_posicion`); los que cambian a propósito en una serie (título, nº de plano) salen en `textos.cambiados`. Los lee `arcmap-mcp-disco.exe` con los mismos handlers que la sesión viva, sin tocar ninguno de los dos. Un `.mxd` con marcos OLE y ArcMap abierto no se lee. Devuelve `iguales`, `num_diferencias` y `diferencias` (`[plano, referencia]`) |

**`describe_mxd` descarta tanto como acusa.** `arcpy.mapping.MapDocument()` falla con un
mensaje genérico ("no puede abrir documento de mapa") que vale igual para una ruta mala, un
fichero corrupto o un documento de versión superior. Si la versión declarada es mayor que la
de tu ArcMap, ahí está la causa; si coincide, la versión queda **descartada** y hay que mirar
otra cosa. Ojo con lo que no garantiza: es lo que el documento dice de sí mismo, no
necesariamente la versión de la aplicación que lo grabó.

**`audit_folder` cuenta dos veces, y el número que importa es el del plano.**
`num_rotas_en_plano` y `num_con_query_en_plano` (por documento y sumados en `resumen`) solo
cuentan capas que se dibujan: encendidas ellas y todos sus grupos (`visible_efectivo`) y en un
marco que cae al menos en parte en la hoja (`marcos[].en_pagina`: `dentro` / `parcial` /
`fuera`). Los totales `num_rotas` y `num_con_query` siguen ahí, pero en una serie real
arrastran grupos apagados y «Nuevo marco de datos» olvidados fuera de la hoja: en una serie
real de siete planos, 335 capas rotas en total y 30 en los planos. Un `None` es «no se pudo leer», no
«apagada», y se cuenta en `num_en_plano_desconocido`. No se mira el rango de escalas de la
capa (arcpy.mapping 10.x no lo expone).

**`audit_folder` funciona con ArcMap abierto** (desde la 2.15.0). Hasta entonces se negaba,
por una medición en la que el mismo `.mxd` pasó de 0,7 s a más de 180 s bloqueado con ArcMap
abierto. No se ha vuelto a reproducir: el mismo documento abre en ~8,5 s con ArcMap cerrado,
abierto con ese mismo `.mxd` y congelado. La protección mira ahora al síntoma: abre cada
documento en un **proceso aparte con timeout**, así que uno atascado no arrastra al resto, y
tras **dos timeouts seguidos** corta la pasada de capas para lo que queda, marcándolo con
`sin_abrir` y explicándolo en `aviso_capas`. Siempre **anuncia lo que trunca**.
`forzar_con_arcmap_abierto` se acepta por compatibilidad y ya no hace nada.

---

## Esenciales

| Tool | Qué hace |
|---|---|
| `ping` | Versión del add-in y de ArcGIS; confirma que el puente está vivo |
| `get_arcmap_info` | `.mxd`, data frames, df activo, escala, vista activa |
| `list_layers` | Capas del df (nombre, visibilidad, fuente, def. query), incluidos grupos. Cada una con su `ruta` en la TOC (`Grupo/Subgrupo/Capa`) |
| `zoom_to_layer` | Encuadra a una capa y refresca el canvas (reproyecta si la capa y el data frame no comparten CRS) |
| `export_pdf` | Exporta el layout a PDF. `salida` absoluta, `dpi` 24-600, `sobrescribir` (True) y `sobrescrito` en la respuesta |
| `refresh` | Refresca vista activa + TOC |
| `execute_arcpy` | **Código arcpy arbitrario** — la base de la filosofía híbrida (ver matiz abajo) |

> **Matiz de `execute_arcpy`:** corre **fuera del proceso de ArcMap**, sobre un
> snapshot del documento (`mxd`/`df` apuntan a la copia). Perfecto para análisis y
> consultas complejas; **no sirve para mutar la sesión viva** (para eso están las tools
> nativas `set_*`, `add_layer`, …). A cambio, un script largo no congela ArcMap.
> Devuelve `RESULT`, stdout y avisos. Detalles que ahorran llamadas:
>
> - La copia del documento solo se hace si el código nombra **`mxd` o `df`** como variable
>   (`MAP`/`mapping` existen siempre y no la fuerzan). Con `usar_documento` se decide a mano.
> - Se devuelve `RESULT`; si asignas `result` en minúscula (la convención del MCP de
>   ArcGIS Pro) también, con un `aviso_result`. La cabecera `# -*- coding: utf-8 -*-` se tolera.
> - Si el código falla, la respuesta trae igualmente el **`stdout`** impreso hasta el fallo:
>   en un bucle, imprime una línea por documento hecho. Un `UnicodeEncodeError` trae una
>   `pista` (casi siempre `str(ex)` sobre un mensaje con tildes: usa `unicode(ex)`).
> - Con un documento «Sin título» (sin data frame activo), `df` vale `None`.
> - **Para exportar varios .mxd no uses un bucle aquí**: un proceso arcpy que ya ha exportado
>   un layout no vuelve a exportar otro. Usa `export_mxd_lote`.
>
> **Identificar una capa.** Toda tool que pide `capa` acepta el nombre o la **ruta de
> grupo** que da `list_layers` (`"Hidrología/Cauces"`). Si el nombre casa con más de una
> capa, la tool **no elige por ti**: falla listando las rutas candidatas. Hasta la 2.11.0
> se quedaba con la primera en silencio, también en `remove_layer`. Dos capas homónimas
> dentro del **mismo** grupo (el mismo shapefile añadido dos veces) se numeran en orden de
> TOC —`Parcelas#1`, `Parcelas#2`— para que siempre haya una ruta que las separe.
>
> **Dónde se hace la copia.** Normalmente en `%TEMP%\arcmap-mcp`, que es lo barato.
> Pero si el documento tiene marcada la casilla *Store relative pathnames*, la copia se
> hace **junto al .mxd original**, como `~arcmap-mcp-snap_*.mxd` oculto: en `%TEMP%`
> esas rutas relativas no resolverían y el snapshot abriría con todas las capas rotas.
> Si esa carpeta no deja escribir, se serializa desde ArcMap (que recalcula las rutas
> al guardar) y **nunca** se cae a `%TEMP%`, que se sabe roto para este caso. Afecta
> igual a las tres tools de Data Driven Pages, que usan el mismo snapshot; el fichero
> es efímero y se borra al terminar la llamada.
>
> La respuesta lo deja a la vista con dos campos: **`snapshot_via`** (`disco_temp`,
> `disco_junto_al_original` o `sesion_serializada`) y **`capas_rotas_en_copia`**, el nº
> de capas con la fuente rota **en la copia**. Si es mayor que cero llega además
> `aviso_capas_rotas`, que nombra el `.mxd` y hasta cinco capas: compáralo con
> `list_broken_data_sources` para saber si ya estaban rotas en la sesión o si las rompió la
> copia. Si el código no usó la copia (no nombra `mxd`/`df`, o los reasigna para abrir otros
> documentos), ninguno de los dos campos aparece: hablarían de un documento que no tocaste.
> Ojo: arcpy y ArcObjects no cuentan igual las rotas (arcpy incluye capas de servicio web
> sin `dataSource`; ArcObjects, tablas), así que el número puede diferir en uno o dos del de
> `list_broken_data_sources` sin que la copia haya roto nada.

---

## Series de planos (Data Driven Pages)

| Tool | Qué hace | Modo |
|---|---|---|
| `list_ddp` | ¿Hay Data Driven Pages?, nº páginas, campo índice, valores | snapshot |
| `export_ddp` | Exporta el atlas a PDF: todas (`modo="ALL"`) / activa (`modo="CURRENT"`) / `rango` de IDs / lista de `valores`; multipágina o 1 PDF por página | snapshot |
| `goto_ddp_page` | Encuadra la vista al extent de una página (por nº o valor de índice) | snapshot + nativo |
| `list_layout_elements` | Lista elementos del layout (texto, leyenda, imagen) con nombre/tipo, **incluidos los de dentro de un grupo** (el cajetín agrupado), con su `grupo` | nativo |
| `set_legend_item` | Qué muestra la entrada de una capa en la leyenda (nombre de capa, encabezado, etiquetas, «colocar en columna nueva») **sin cambiar las fuentes**; devuelve las fuentes leídas como prueba. También la **quita** de la leyenda dejando la capa en el mapa (`quitar`) o la **mueve** (`posicion`, `despues_de`), y devuelve el `orden` de la leyenda. Si la capa está en varias leyendas no elige: desempate con `leyenda` o `indice` | nativo |
| `set_legend` | Lo que es de la leyenda entera: `tamano` de letra de todas las entradas (apaga el ajuste automático, que encoge la letra al dibujar), `columnas` y `recuadro` (mide el tamaño natural y pone el marco a esa medida, anclado arriba a la izquierda, **sin escalar la letra**; dice si cabe y cuánto sobra o falta). Sin parámetros, devuelve el estado leído | nativo |
| `set_text_element` | Cambia el texto de un elemento (título, fecha, nº plano), también dentro de un grupo, y/o su posición (`x`, `y`, unidades de página) y tamaño de letra (`tamano`); lo que no se pasa no cambia. `patron` + `reemplazo` cambia solo la parte que casa (regex de .NET, grupos como `$1`), en uno o, con `todos=True`, en todos los que casan. Si el selector casa con varios no elige: los lista numerados y se desempata con `grupo` o, en último caso, `indice` | nativo |
| `replace_text` | Buscar y reemplazar **por diccionario** (`{"Area": "Área"}`) en todo el plano a la vez: textos del layout (también dentro de un grupo), nombres de capa, etiquetas de clase de la simbología y encabezados (`ambitos`). Sin distinguir mayúsculas al buscar; `palabra_completa` (True) y `conservar_mayusculas` (True: «AREA» → «ÁREA», sin bajar nunca las mayúsculas propias del reemplazo). No toca las etiquetas de formato (`<BOL>`…). `simular=True` dice qué cambiaría sin tocar nada. Devuelve `cambiados`, `por_ambito` y la lista `cambios` con antes y ahora. También es operación de `edit_mxd_lote` | nativo |
| `copy_layout_elements` | Copia elementos del layout de **otro .mxd** (`origen`, ruta absoluta, solo lectura) a este, en la misma posición: los logos del cajetín de un plano bueno al resto de la serie. Se eligen por `nombres` y/o por `region` de página (para el logo sin nombre), también dentro de grupos; `tipos`: `imagen` (por defecto), `texto`, `grafico`. La imagen incrustada viaja con sus bytes. `quitar` borra antes elementos sueltos de este documento (si alguno no está, error sin tocar nada). Un origen con marcos OLE se rechaza; si la página mide distinto, se pega y se avisa. Devuelve `copiados` (con la `caja` leída ya pegada) y `quitados`. También es operación de `edit_mxd_lote` | nativo |
| `set_definition_query` | Fija/limpia la def. query de una capa (planos temáticos por filtro) | nativo |
| `set_layer_visibility` | Enciende/apaga capa o grupo (la leyenda del layout se actualiza) | nativo |
| `export_view_png` | Exporta la vista activa (o el layout con `modo="layout"`) a PNG | nativo |
| `export_jpg` | Exporta el layout a JPG (dpi 230 por defecto — series de planos ligeras). Vale `.jpg` o `.jpeg` | nativo |
| `export_mxd_lote` | Exporta a JPG o PDF una **lista de .mxd del disco**, un proceso `python.exe` por documento. Por defecto, cada fichero junto a su .mxd. Devuelve bytes, `sobrescrito` y `capas_rotas` por documento. `corte_sin_cpu` (120 s) corta un documento cuyo proceso no consume CPU, sin esperar al timeout; `si_capas_rotas="fallar"` no exporta los que tienen capas rotas en el plano | sin puente |

> **Los tres export** (`export_pdf`, `export_jpg`, `export_view_png`) comparten reglas:
> `salida` **absoluta** en una carpeta que exista, `dpi` entre 24 y 600, y
> `sobrescribir=True` por defecto —una serie que se regenera necesita pisar— con
> **`sobrescrito`** en la respuesta para que nunca sea silencioso. Se exporta a un
> temporal y solo al final se mueve al destino: cancelar con ESC ya no destruye el plano
> que había. Exportan **siempre el documento abierto**, y la respuesta trae `documento`
> con su ruta.
>
> **Para una serie de .mxd, `export_mxd_lote`, no un bucle en `execute_arcpy`:** un
> proceso arcpy que ya ha exportado un layout no vuelve a exportar otro, así que el bucle
> saca el primero y falla en el resto. El lote lanza un proceso por documento y funciona
> con ArcMap abierto.

> **Matiz de `goto_ddp_page`:** el atlas vivo de la sesión **no pagina** (la API de
> Data Driven Pages solo existe en arcpy, que corre sobre el snapshot). La tool lee el
> extent de la página pedida en el snapshot y **encuadra la vista viva a ese extent**:
> el resultado es un encuadre aproximado, no un cambio de página real del atlas. Los
> elementos dinámicos del layout (título de página, flechas) no cambian — para series
> de planos usa `set_text_element` + `set_definition_query`/`set_layer_visibility`,
> o exporta directamente con `export_ddp`.
>
> **Firma de `export_ddp`:** `export_ddp(salida, modo="ALL", rango, valores,
> un_pdf_por_pagina, dpi)`. **No existe ningún parámetro `paginas`.** Las páginas se
> eligen con **una sola** de estas vías: `modo="ALL"` (todas, por defecto),
> `modo="CURRENT"` (solo la activa), `rango="1-3,5"` (IDs de página, 1-based) o
> `valores=[…]` (valores del campo índice, que se traducen a IDs solos).
> `un_pdf_por_pagina=True` saca un PDF por página en vez de uno multipágina.
>
> **Atlas grandes:** un atlas de cientos de páginas puede agotar la espera aun con el
> timeout de fondo de las DDP (`ARCMAP_FONDO_TIMEOUT`) — exporta por lotes con
> `valores` o con `rango`, que
> además da feedback lote a lote.
>
> **Localizar un texto:** `list_layout_elements(tipo="TEXT_ELEMENT")` lista todos los textos
> del layout con su `texto` y su `grupo`. (Desde la 2.17.0 `texto` es opcional, así que el
> viejo truco de llamar a `set_text_element(buscar="")` ya no devuelve la lista: responde
> «No hay nada que cambiar».)

---

## Capas y datos

Todas **nativas sobre la sesión viva** (los cambios se ven al instante; las consultas
respetan definition query y selección, igual que la tabla de atributos).

| Tool | Qué hace |
|---|---|
| `select_by_attribute` | Selección por SQL sobre una capa: `select_by_attribute(capa, where)`. Siempre selección NUEVA (`NEW_SELECTION`); no hay modos ADD/REMOVE/SUBSET. Devuelve el nº seleccionado |
| `clear_selection` | Limpia la selección |
| `get_unique_values` | Valores únicos de un campo (iterar planos por categoría). `max_valores` (1000) corta el recorrido y lo dice con `truncado: true` |
| `count_features` | Conteo (total o con `where`; honra def. query y selección) |
| `list_fields` | Campos de capa/tabla (nombre, tipo) |
| `get_layer_info` | Detalle de una capa: campos, tipo geom, extent, CRS, count |
| `get_layer_features` | Lee FILAS de atributos (respeta def. query/selección). `limite` entre 1 y 5000; admite campos de una tabla unida |
| `add_layer` | Añade capa desde shp/fgdb/ráster/`.lyr` o **URL WMS** al df. `en_leyenda=False`: no entra en la leyenda del plano (las leyendas de ArcMap añaden solas cada capa nueva). `visible=False`: entra apagada. En un WMS, `subcapas` elige qué encender (sin ella, todas) |
| `add_group` | Crea una capa de **grupo** vacía (en la raíz o dentro de otro grupo), encendida o apagada y, si se quiere, fuera de la leyenda |
| `remove_layer` | Quita capa por nombre |
| `move_layer` | Recoloca una capa o grupo **sin quitarla**: `BEFORE`/`AFTER` de una `referencia` (cambiando de grupo si hace falta) o `TOP`/`BOTTOM` de un `grupo` (`"/"` = raíz). Devuelve el orden del grupo leído de vuelta. La leyenda no sigue a la TOC: con `en_leyenda=True` sus entradas se recolocan también |
| `duplicate_layer` | Copia una capa dentro del mapa (misma fuente, simbología, etiquetas, def. query y transparencia **propias**) con `nombre_nuevo`, junto a la original o a una `referencia`. `etiquetas=False` la deja sin rótulos; por defecto no entra en la leyenda |
| `rename_layer` | Cambia el nombre de una capa o grupo (TOC y leyenda) **sin sustituirla**: su entrada de leyenda conserva posición y opciones. Refresca las leyendas del mapa |
| `apply_symbology_from_layer` | Aplica un `.lyr` (estilos canónicos) a una capa |
| `save_layer_as_lyr` | Guarda una capa (o un **grupo** con todo su árbol) como `.lyr`, con simbología, etiquetas, def. query y transparencia: el «Guardar como archivo de capa» de ArcMap, para llevar la simbología buena al resto de la serie. `ruta` absoluta; si existe, error salvo `sobrescribir=True`. Se escribe a un temporal y se **reabre** antes de dejarlo en `ruta`: si lo leído no coincide con la capa, error y no se deja nada (la respuesta trae `leido`). Las rutas de los datos se guardan absolutas. También es operación de `edit_mxd_lote` |
| `set_scale` | Fija la escala del df activo |

**Servicios WMS.** `add_layer` con una URL conecta el servicio (`WMSConnectionName`) y
enciende las subcapas: un WMS recién conectado en ArcMap 10.5 las trae **todas apagadas**, y
encender solo el nodo padre no pinta nada. Se encienden las pedidas en `subcapas` (por nombre
o ruta `Grupo/Subcapa`) con los grupos que las contienen, o todas. Una subcapa que no existe
es error antes de tocar la TOC. Con un WMS encendido, cada zoom o captura lo redibuja por la
red en el hilo único de ArcMap: al montar un documento, añádelo con `visible=False` y
enciéndelo al final.

**Reordenar sin perder nada.** `move_layer` mueve el mismo objeto de capa
(`IMapLayers.MoveLayerEx`): simbología, etiquetas y entradas de leyenda se quedan. Antes, la
única vía para meter un grupo entre otros dos era quitar las capas de debajo y volver a
añadirlas, que pierde la simbología que no venga de un `.lyr`.

---

## Simbología

Todas **nativas sobre la sesión viva**. Y la aclaración que ahorra una sesión perdida:
`execute_arcpy` **NO sirve** para simbolizar, porque opera sobre una copia del documento y
sus cambios al renderer se descartan (ADR-004). Si tu código toca renderer o symbology, la
respuesta lo avisa y te manda aquí.

| Tool | Qué hace |
|---|---|
| `set_graduated_symbology` | Rangos sobre un campo numérico (capas de ENTIDADES), por método o con `cortes` fijos (límites superiores de cada clase). `etiquetas` por clase o `"auto"` (desde los cortes, con coma decimal). Devuelve cortes y etiquetas leídos del renderer |
| `set_unique_values_symbology` | Categorías por valores únicos de un campo o de **hasta 3** (`campos`), en capas de ENTIDADES. `clases` explícitas con varios valores por clase, etiqueta y color; `resto` para «todos los demás valores». Devuelve las clases leídas del renderer y los valores que se quedan sin clase |
| `set_raster_symbology` | RÁSTER: `modo="clasificado"` (2-32 clases, o `cortes` fijos; `etiquetas="auto"`), `"estirado"` (rampa continua), `"unico"` (un color por valor de píxel) o `"rgb"` (composición de color de un multibanda, `bandas=[r, g, b]`; sin estirar en 8 bits) |
| `apply_symbology_from_layer` | Aplica un `.lyr` plantilla ya preparado |
| `set_single_symbology` | **Un solo símbolo** (sin clasificar) con los colores pedidos: relleno, borde y grosor, hueco, tamaño en puntos, transparencia. Sin colores sale gris neutro, siempre el mismo |
| `edit_symbol` | Cambia **solo lo pedido** de la simbología que ya tiene la capa (símbolo único, valores únicos o rangos), sin rehacerla; `categoria` elige la clase. `etiqueta` cambia el texto de una clase en la leyenda; `quitar=True` quita una clase de valores únicos sin recolorear las demás; `anadir_valores` mete valores en una clase que ya existe; `encabezado` cambia el texto de encima de las clases. En RÁSTER clasificado o por valores únicos: `encabezado`, y `etiqueta` y `color_relleno` de una clase. Sin nada que cambiar, la lee |
| `list_style_symbols` | Sin `estilo`, los estilos `.style` **cargados** en ArcMap; con él (nombre o ruta), sus símbolos de relleno, línea o marcador con nombre y categoría, filtrables por `patron` |
| `apply_style_symbol` | Pone a una capa, como símbolo único, un símbolo de un `.style` por su nombre. La clase sale de la geometría de la capa |
| `set_labels` | Etiquetas de una capa de ENTIDADES: expresión, tamaño, color y halo; enciende o apaga. Con Maplex, colocación: `parte_mayor`, `sin_duplicados_m`, `tamano_minimo_m`, `repetir_cada_mm`. `copiar_de` clona las clases de otra capa; `por_valor` hace una clase por valor con el color de su categoría. Devuelve cómo quedó cada clase **leída de la capa** y el motor del mapa (Maplex o estándar) |

**Un color, sin rehacer la leyenda.** Antes de `set_single_symbology` no había forma de dejar
una capa con un solo color: forzar la graduada o los valores únicos a un color pinta bien el
mapa pero llena la tabla de contenidos con una entrada por entidad, y recargar la capa la trae
con un color aleatorio. `edit_symbol` trabaja sobre una **copia** del símbolo de cada clase y
cambia solo lo pedido, así que el tipo de símbolo, los patrones y las demás categorías no se
tocan. Sin `categoria`, en una capa clasificada cambia lo común a todas (borde, grosor, tamaño,
transparencia) y **se niega a cambiar el relleno de todas**, que borraría la clasificación. Lo
que no aplica a la geometría (relleno en líneas, `tamano` en polígonos) es error, no se ignora.

**Los estilos del usuario, sin tocar su galería.** `list_style_symbols` y `apply_style_symbol`
leen los `.style` con la galería de estilos de ArcMap. Un `.style` que no estaba cargado se
añade **solo mientras dura la llamada** y se quita al terminar, también si falla: las
«Referencias de estilo» del usuario no se quedan cambiadas por una consulta. Los estilos de la
instalación (`ESRI.style`) figuran en la galería con ruta relativa; la respuesta los devuelve
siempre con la ruta completa. Si hay dos símbolos con el mismo nombre en categorías distintas,
`apply_style_symbol` falla listándolas y pide `categoria_estilo`.

**`set_labels` modifica las clases de etiquetas que ya tiene la capa; no las sustituye.**
La vía obvia en ArcObjects, vaciar las clases y crear unas nuevas, tiene un fallo silencioso: en un
mapa con **Maplex** la capa se queda sin etiquetas y sin error. Con el motor estándar esa vía sí
pinta, pero modificar las existentes funciona con los dos y además conserva la fuente, la
colocación y los filtros de cada clase. Solo si la capa no tiene ninguna clase se crea una (hace
falta `expresion`), con las propiedades de colocación del motor del mapa. La regresión en vivo
comprueba que las etiquetas **se dibujan** comparando la vista con ellas y sin ellas, porque la
respuesta de la herramienta sola no delataría el fallo.

Cada una acepta la capa que le toca y **redirige a la correcta si te equivocas**: pedir
simbología ráster sobre un vectorial responde nombrando las dos alternativas, y al revés.

**Colores y rampa, igual en las tres.** Un color es `[r, g, b]` (0-255) o `"#RRGGBB"`, y uno
mal formado es **error**: hasta la 2.11.0 se ignoraba y salía la rampa por defecto, con el
cliente creyendo que se había aplicado la suya. `algoritmo` (`cielab` por defecto, `lablch`,
`hsv`) existe también en las dos vectoriales desde la 2.12.0. **Esto cambia el resultado por
defecto de `set_graduated_symbology`**, que hasta entonces interpolaba en HSV: si tienes que
reproducir exactamente los colores de una serie ya entregada, pasa `algoritmo="hsv"`. Las
dos vectoriales admiten campos de una tabla unida (join), y los parámetros se validan
**antes** de tocar la capa: un valor inválido ya no deja el renderer cambiado a medias.

**Categórica:** la paleta por defecto reparte tonos por el círculo cromático, no una rampa
secuencial, porque en categórico hace falta **distinguir**, no ordenar. Es reproducible, así
que la misma capa con el mismo campo sale siempre igual (importa al reexportar una serie).
Tope de 100 categorías; los NULL se omiten; un valor no listado **no se dibuja** en vez de
colarse con un color cualquiera. Las categorías son las que la capa **dibuja**: se respeta su
definition query (una capa regional filtrada a un municipio da la leyenda del municipio), y
un campo numérico se ordena numéricamente. La selección no se tiene en cuenta, a propósito.

**Ráster:** si la banda no tiene estadísticas, se calculan solas (en clasificado y en
estirado) y la respuesta trae `estadisticas_calculadas`. Sin ellas la clasificación falla con
un `E_FAIL` de COM que no explica nada. Los píxeles no cambian, pero ArcMap guarda las
estadísticas con el ráster al liberarlo (metadatos dentro del TIFF e histograma en
`.aux.xml`): la primera vez cuesta (~50 s en un TIFF de 2 GB) y después es instantáneo. En
una carpeta sin escritura se recalculan en cada llamada. El número real de clases puede ser menor
que el pedido si el ráster no tiene variación suficiente, y se devuelve el real.

`set_raster_symbology(capa, modo, num_clases, color_desde, color_hasta, colores, etiquetas,
algoritmo, valores, transparentes, transparencia)`. Los tres modos:

- **`clasificado`** (por defecto): `num_clases` clases (2-32) con rampa de color.
- **`estirado`**: rampa continua entre el mínimo y el máximo de la banda 0.
- **`unico`** (desde la 2.11.0): un color por valor de píxel. Es **el modo de un ráster
  CATEGÓRICO** —máscara de visibilidad, FCC binario, reclasificación, el `paletted` de
  QGIS—, donde clasificar falla con un `E_FAIL` de COM porque no hay histograma que
  cortar, y donde además clasificar sería mentir sobre el dato.

En modo `unico`:

- **`valores`** es obligatorio: la lista de valores de píxel a pintar (`[1, 2]`). No se
  deducen del ráster, porque adivinarlos sería inventar la leyenda.
- **`colores`** / **`etiquetas`**: uno por valor. Sin `colores`, tonos repartidos por el
  círculo cromático.
- **`transparentes`**: valores que **no se pintan ni salen en la leyenda** (`[0]`). Es la
  traducción exacta del `paletted` de QGIS. **Sin esto el fondo tapa el mapa**, y en una
  máscara de visibilidad el fondo suele ser el 90 % del ráster.

**`transparencia`** (0-100, en cualquier modo) es la opacidad de la capa: la opacidad 0,6
de QGIS es `transparencia=40`.

**Color.** Hay dos vías y la explícita manda: `colores` (una lista `[R, G, B]` por clase,
la vía fiable cuando las clases son categóricas o los cortes vienen por percentil) o
`color_desde`/`color_hasta`, los dos extremos de una rampa (por defecto amarillo claro
`[255, 255, 178]` → rojo oscuro `[189, 0, 38]`). **`etiquetas`** rotula la leyenda con el
significado ("Defoliación fuerte") en vez de con el rango numérico del corte.

**`algoritmo`** de interpolación de la rampa: `cielab` (por defecto), `lablch` o `hsv`. **No
uses `hsv` para nada cuantitativo**: interpola el tono dando la vuelta a la rueda de color,
así que entre dos rojos separados 27° saca un arcoíris que pasa por morado, turquesa y
verde. Era el comportamiento por defecto hasta el 2026-09-04.

La cabecera de la leyenda queda siempre en `Value` (ArcObjects la fija en el `Update()` y
después es de solo lectura); se quita en el elemento de leyenda del layout, no desde aquí.

---

## Marcadores espaciales

| Tool | Qué hace |
|---|---|
| `get_bookmarks` | Lista los marcadores del df activo, con su extensión |
| `add_bookmark` | Guarda la extensión ACTUAL con un nombre |
| `remove_bookmark` | Borra un marcador por nombre |
| `goto_bookmark` | Encuadra la vista en un marcador guardado |

`add_bookmark` con un nombre que ya existe **reemplaza**, no duplica: dos entradas iguales en
el menú de marcadores no se distinguen. La búsqueda por nombre ignora mayúsculas.

---

## Geoprocesamiento y mantenimiento

| Tool | Qué hace | Modo |
|---|---|---|
| `run_geoprocessing` | Geoproceso por nombre punteado (`analysis.Buffer`, `management.GetCount`, `sa.Slope`…) + params, sin escribir código. Resuelve nombres de capa de la TOC (honra def. query/selección). Una tool inexistente o con parámetros de más da error **antes** de ejecutar | **nativo** — ocupa la interfaz mientras dura. Con `fuera_de_arcmap=True`, **out-of-process**: ArcMap libre, las capas viajan por la ruta de su fuente (con def. query o selección se niega), `anadir_al_mapa` y `sobrescribir` |
| `save_mxd` | Guarda el .mxd en su ruta actual. Predice las capas que perderán la ruta relativa (`capas_perderan_ruta`) y, con `verificar` o si predice alguna, **reabre** lo guardado y lista las rotas nuevas | nativo; la verificación, arcpy standalone |
| `save_mxd_as` | Guarda una copia en otra ruta (absoluta). **`sobrescribir=False` por defecto**: si el destino existe, falla sin tocarlo. Al regenerar una serie de .mxd, pasa `sobrescribir=True`. Predice y verifica las rutas relativas como `save_mxd`, contra la carpeta de destino | nativo |
| `open_mxd` | Abre otro .mxd en la misma sesión **sin cerrar ArcMap**. Nunca deja que ArcMap pregunte: los cambios pendientes del actual se guardan, se descartan o no se abre (`cambios_sin_guardar`); un .mxd de versión posterior no se abre | nativo |
| `list_broken_data_sources` | Capas y tablas standalone con ruta rota (muy común en ArcMap), de **todos** los data frames, con `data_frame` y `ruta` | nativo |
| `repair_data_source` | Reapunta la fuente de una capa (verifica releyendo la fuente). Busca en todos los data frames; `data_frame` acota. Avisa (`aviso_ruta_relativa`) si la ruta nueva se va a perder al guardar | nativo |
| `replace_data_source` | Apunta una capa del df activo a **otro dataset** (cambia el fichero, no solo la carpeta: para eso, `repair_data_source`) conservando simbología, etiquetas y def. query, como el `replaceDataSource` de arcpy. La `fuente` nueva se abre antes de tocar nada; ráster con ráster y entidades con entidades de la misma geometría. En un ráster clasificado conserva cortes, etiquetas, colores y encabezado (y avisa si el rango del dato nuevo es otro); avisa si la def. query usa campos que el dataset nuevo no tiene. Devuelve `fuente_anterior`, `fuente_nueva` (leída de vuelta) y si la capa estaba o queda rota. También es operación de `edit_mxd_lote` | nativo |

> **Reparada en memoria no es reparada en disco.** Con *Store relative pathnames*, ArcMap
> 10.5 concatena sin normalizar la carpeta del .mxd y la ruta relativa del workspace de cada
> capa (`C:\planos\..\..\datos`). Si esa cadena llega a **260 caracteres** (MAX_PATH), no
> escribe el workspace: al reabrir la fuente es `\fichero.shp` y la capa está rota, aunque en
> memoria se vea bien. No depende de la longitud del dato: en la medición del 2026-09-25 un
> dato a 252 caracteres guardaba bien con el .mxd en una carpeta corta, y uno a 43 se perdía
> con el .mxd en una carpeta de 247. Por eso `repair_data_source` avisa y `save_mxd` /
> `save_mxd_as` predicen y, si hace falta, reabren. Arreglo: acortar la carpeta de los datos o
> la del .mxd, o guardar ese .mxd con rutas absolutas.

> **Multivalor en `run_geoprocessing`.** Un parámetro que admite varias entradas (Merge,
> Union, Intersect…) se pasa como **lista dentro de `params`**:
> `[["capaA", "capaB"], "C:\\out.shp"]`. Hasta la 2.11.0 llegaba al geoproceso como texto
> JSON y fallaba con ERROR 000732. Ojo con la diferencia: una capa de la TOC **dentro de una
> lista** entra por la ruta de su fuente, así que **pierde su definition query y su
> selección**; como argumento suelto las conserva.

---

## Visualización y catálogo

| Tool | Qué hace |
|---|---|
| `get_canvas_screenshot` | Captura la vista/layout y la devuelve como **IMAGEN INLINE** — el agente la ve al instante; cancelable con ESC |
| `list_data_frames` / `set_active_df` | Lista/cambia el data frame activo |
| `set_frame_clip` | Recorta un data frame a la forma (unión de entidades) de una capa de polígonos, con `where` opcional y capas que se libran del recorte (`excluir`); `quitar=True` lo deja sin recorte. Sin parámetros, el estado. El recorte solo se ve en el export |
| `set_extent` | Encuadra a coords o al extent de una capa/selección |
| `describe_data` | Describe un dataset en disco (CRS, tipo, campos) |
| `get_workspace` / `set_workspace` | Lee/fija el workspace por defecto de los listados (estado del add-in; se restablece al reiniciar ArcMap) |
| `list_feature_classes` | Feature classes del workspace (incl. datasets) |
| `list_tables` | Tablas del workspace |
| `list_rasters` | Rásters del workspace |

> La captura exporta con garantías la **vista activa**; si pides la otra (datos ↔
> layout), el add-in conmuta de vista temporalmente y lo avisa en la respuesta.

---

## Análisis ambiental y teledetección

> Requieren **Spatial Analyst** o **3D Analyst** y operan sobre datos **en disco**.
> Corren **fuera del proceso de ArcMap** (modo snapshot): pueden tardar minutos pero
> **no congelan la interfaz**. Usan el timeout de fondo (`ARCMAP_FONDO_TIMEOUT`,
> 46 min), **no** `ARCMAP_GP_TIMEOUT`.
> Por defecto añaden el resultado al data frame activo (`anadir_al_mapa=True`).

| Tool | Qué hace | Extensión |
|---|---|---|
| `raster_index` | **Índice espectral con nombre** (NDVI, GNDVI, NDRE, NDWI, MNDWI, NDMI, NBR, SAVI, EVI) desde bandas | Spatial |
| `hydrology` | `cuenca` (Fill→FlowDir→FlowAcc→Watershed/Basin) · `red_drenaje` · `inundacion` | Spatial |
| `contours` | Curvas de nivel desde MDT (export DXF opcional) | 3D |
| `topographic_profile` | Perfil topográfico: línea 2D → línea 3D sobre superficie | 3D |
| `least_cost_path` | Ruta de mínimo coste (origen→destino sobre ráster de fricción) | Spatial |
| `calculate_geometry` | Área/perímetro/longitud/coords/centroide a campos (in place) | — |

> **Matiz de `calculate_geometry`:** es la excepción del grupo — corre **nativa, en
> el proceso de ArcMap**. Añadir campos a una fuente que está cargada en la TOC desde
> un proceso externo falla por bloqueo de esquema (schema lock); al ir por dentro no
> hay bloqueo y además honra la definition query y la selección de la capa.

### Índices espectrales de `raster_index`

`raster_index(indice, bandas, salida)` donde `bandas` es un dict `{ROL: ruta_raster}`.
Fórmulas y roles de banda que pide cada índice:

| Índice | Fórmula | Roles requeridos | Uso |
|---|---|---|---|
| **NDVI** | (NIR − RED)/(NIR + RED) | NIR, RED | Verdor de la vegetación |
| **GNDVI** | (NIR − GREEN)/(NIR + GREEN) | NIR, GREEN | Clorofila |
| **NDRE** | (NIR − REDEDGE)/(NIR + REDEDGE) | NIR, REDEDGE | Red-edge (solo S2): vigor/estrés |
| **NDWI** | (GREEN − NIR)/(GREEN + NIR) | GREEN, NIR | Agua superficial (McFeeters) |
| **MNDWI** | (GREEN − SWIR1)/(GREEN + SWIR1) | GREEN, SWIR1 | Agua mejorado (Xu) |
| **NDMI** | (NIR − SWIR1)/(NIR + SWIR1) | NIR, SWIR1 | Humedad de la vegetación |
| **NBR** | (NIR − SWIR2)/(NIR + SWIR2) | NIR, SWIR2 | Área quemada / severidad |
| **SAVI** | ((NIR − RED)/(NIR + RED + L))·(1+L) | NIR, RED (+`L`=0.5) | Vegetación corregido por suelo |
| **EVI** | 2.5·((NIR − RED)/(NIR + 6·RED − 7.5·BLUE + 1)) | NIR, RED, BLUE | Vegetación, alta biomasa |

`indice="CUSTOM"` + `banda_a`/`banda_b` → índice normalizado genérico (a−b)/(a+b).

### Correspondencia ROL → banda física por sensor

| ROL | Sentinel-2 | Landsat 8-9 (OLI) | Landsat 4-7 (TM/ETM+) |
|---|---|---|---|
| BLUE | B2 | B2 | B1 |
| GREEN | B3 | B3 | B2 |
| RED | B4 | B4 | B3 |
| REDEDGE | B5 | — | — |
| NIR | B8 / B8A | B5 | B4 |
| SWIR1 | B11 | B6 | B5 |
| SWIR2 | B12 | B7 | B7 |

> Ejemplo NBR sobre Sentinel-2:
> `raster_index(indice="NBR", bandas={"NIR": "B08.tif", "SWIR2": "B12.tif"}, salida="nbr.tif")`.

---

> **¿Cuándo añadir un wrapper nuevo?** Si la operación se va a repetir y/o es propensa
> a errores escrita a mano en `execute_arcpy` → merece wrapper. Si es puntual → queda
> en `execute_arcpy`. Los **marcadores espaciales** son el ejemplo de operación que
> pasó el listón y ya tiene sus cuatro tools (*Marcadores espaciales*, arriba).
