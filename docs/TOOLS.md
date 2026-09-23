# Catálogo de herramientas — arcmap-mcp

> **Filosofía híbrida.** `execute_arcpy` (código arbitrario) es la base universal:
> cualquier análisis de ArcMap 10.x se puede expresar con él. Los **wrappers** de esta
> lista existen solo para lo **repetitivo y de alto valor** —sobre todo las series de
> planos (Data Driven Pages)—, no para replicar toda la API.

**58 herramientas** sobre ArcMap 10.5. Unas **34 están cubiertas por la regresión en sesión
viva** (`tests/regresion_sesion_viva.py`, 106 comprobaciones); el resto se ha ejercitado a
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
| **Sin ArcMap, leyendo del disco** | `describe_mxd`, `audit_folder`, `export_mxd_lote` | **No pasan por el puente**: funcionan con ArcMap cerrado (y `export_mxd_lote` también con ArcMap abierto) y sobre documentos que no son el abierto. |

---

## Sin ArcMap abierto (inspección de ficheros)

Estas dos no hablan con el puente: leen del disco. Sirven precisamente cuando el camino
normal falla, que es cuando más falta hace saber algo.

| Tool | Qué hace |
|---|---|
| `describe_mxd` | Versión declarada de un `.mxd` **sin abrirlo**, más un veredicto frente a la versión de ArcMap. Milisegundos, sin arcpy y sin licencia |
| `audit_folder` | Inventario de TODOS los `.mxd` de una carpeta: versión, capas, fuentes rotas y definition queries |

**`describe_mxd` descarta tanto como acusa.** `arcpy.mapping.MapDocument()` falla con un
mensaje genérico ("no puede abrir documento de mapa") que vale igual para una ruta mala, un
fichero corrupto o un documento de versión superior. Si la versión declarada es mayor que la
de tu ArcMap, ahí está la causa; si coincide, la versión queda **descartada** y hay que mirar
otra cosa. Ojo con lo que no garantiza: es lo que el documento dice de sí mismo, no
necesariamente la versión de la aplicación que lo grabó.

**⚠️ `audit_folder` con `con_capas=True` exige ArcMap CERRADO.** El arcpy standalone se
**bloquea al abrir un documento mientras ArcMap tiene tomada la licencia de Desktop**: el
mismo `.mxd` abre en 0,7 s con ArcMap cerrado y sigue bloqueado a los 180 s con ArcMap
abierto. Y no se ve venir, porque `import arcpy` tarda lo mismo en ambos casos. La
herramienta lo detecta y omite esa pasada explicando por qué; se fuerza con
`forzar_con_arcmap_abierto=True`, pero entonces cada documento agotará su timeout sin dar
nada. Abre cada documento en un **proceso aparte con timeout**, así que uno atascado no
arrastra al resto, y **anuncia siempre lo que trunca**.

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
| `set_text_element` | Cambia el texto de un elemento (título, fecha, nº plano), también dentro de un grupo. Si el selector casa con varios no elige: los lista numerados y se desempata con `grupo` o, en último caso, `indice` | nativo |
| `set_definition_query` | Fija/limpia la def. query de una capa (planos temáticos por filtro) | nativo |
| `set_layer_visibility` | Enciende/apaga capa o grupo (la leyenda del layout se actualiza) | nativo |
| `export_view_png` | Exporta la vista activa (o el layout con `modo="layout"`) a PNG | nativo |
| `export_jpg` | Exporta el layout a JPG (dpi 230 por defecto — series de planos ligeras). Vale `.jpg` o `.jpeg` | nativo |
| `export_mxd_lote` | Exporta a JPG o PDF una **lista de .mxd del disco**, un proceso `python.exe` por documento. Por defecto, cada fichero junto a su .mxd. Devuelve bytes y `sobrescrito` por documento | sin puente |

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
> **Truco QA de `set_text_element`:** llamar con `buscar=""` devuelve en el error la
> lista de todos los textos del layout — útil para localizar el elemento a tocar.

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
| `add_layer` | Añade capa desde shp/fgdb/raster al df |
| `remove_layer` | Quita capa por nombre |
| `apply_symbology_from_layer` | Aplica un `.lyr` (estilos canónicos) a una capa |
| `set_scale` | Fija la escala del df activo |

---

## Simbología

Todas **nativas sobre la sesión viva**. Y la aclaración que ahorra una sesión perdida:
`execute_arcpy` **NO sirve** para simbolizar, porque opera sobre una copia del documento y
sus cambios al renderer se descartan (ADR-004). Si tu código toca renderer o symbology, la
respuesta lo avisa y te manda aquí.

| Tool | Qué hace |
|---|---|
| `set_graduated_symbology` | Rangos sobre un campo numérico (capas de ENTIDADES) |
| `set_unique_values_symbology` | Categorías por valores únicos de un campo (capas de ENTIDADES) |
| `set_raster_symbology` | RÁSTER: `modo="clasificado"` (2-32 clases), `"estirado"` (rampa continua) o `"unico"` (un color por valor de píxel) |
| `apply_symbology_from_layer` | Aplica un `.lyr` plantilla ya preparado |

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
| `run_geoprocessing` | Geoproceso por nombre punteado (`analysis.Buffer`, `management.GetCount`, `sa.Slope`…) + params, sin escribir código. Resuelve nombres de capa de la TOC (honra def. query/selección) | **nativo** — ocupa la interfaz mientras dura |
| `save_mxd` | Guarda el .mxd en su ruta actual | nativo |
| `save_mxd_as` | Guarda una copia en otra ruta (absoluta). **`sobrescribir=False` por defecto**: si el destino existe, falla sin tocarlo. Al regenerar una serie de .mxd, pasa `sobrescribir=True` | nativo |
| `list_broken_data_sources` | Capas y tablas standalone con ruta rota (muy común en ArcMap), de **todos** los data frames, con `data_frame` y `ruta` | nativo |
| `repair_data_source` | Reapunta la fuente de una capa (verifica releyendo la fuente). Busca en todos los data frames; `data_frame` acota | nativo |

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
