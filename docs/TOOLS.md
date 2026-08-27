# Catálogo de herramientas — arcmap-mcp

> **Filosofía híbrida.** `execute_arcpy` (código arbitrario) es la base universal:
> cualquier análisis de ArcMap 10.x se puede expresar con él. Los **wrappers** de esta
> lista existen solo para lo **repetitivo y de alto valor** —sobre todo las series de
> planos (Data Driven Pages)—, no para replicar toda la API.

**57 herramientas**, todas probadas por llamada cableada real sobre ArcMap 10.5.

## Cómo se ejecuta cada herramienta (importa para lo que puedes esperar)

El add-in .NET tiene **tres modos de ejecución**, y cada herramienta usa el que le
corresponde:

| Modo | Qué herramientas | Consecuencias |
|---|---|---|
| **Nativo en sesión viva** (ArcObjects, hilo STA) | capas, selección, layout, exports, navegación, `run_geoprocessing`, `calculate_geometry` | Opera sobre el documento **vivo**: los cambios se ven al instante. Ocupa la interfaz mientras dura (exports cancelables con **ESC**). |
| **Out-of-process sobre snapshot** (arcpy en Python 2.7 aparte) | `execute_arcpy`, `list_ddp`, `export_ddp`, `goto_ddp_page`, `raster_index`, `hydrology`, `contours`, `topographic_profile`, `least_cost_path` | Trabaja sobre una **copia temporal del .mxd** con el estado actual: lee el documento real, pero **sus cambios al documento no afectan a la sesión viva**. Las salidas a disco sí son reales, y los resultados de análisis se añaden al mapa al terminar. **No congela la interfaz.** Coste fijo de unos segundos por llamada (snapshot + arranque de Python). |
| **Solo lectura de datos** (cursores/Describe) | consultas, listados de workspace | Sin efectos secundarios. |
| **Sin ArcMap, leyendo del disco** | `describe_mxd`, `audit_folder` | **No pasan por el puente**: funcionan con ArcMap cerrado y sobre documentos que ArcMap se niega a abrir. Es su razón de ser. |

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
| `list_layers` | Capas del df (nombre, visibilidad, fuente, def. query), incluidos grupos |
| `zoom_to_layer` | Encuadra a una capa y refresca el canvas |
| `export_pdf` | Exporta el layout a PDF |
| `refresh` | Refresca vista activa + TOC |
| `execute_arcpy` | **Código arcpy arbitrario** — la base de la filosofía híbrida (ver matiz abajo) |

> **Matiz de `execute_arcpy`:** corre **fuera del proceso de ArcMap**, sobre un
> snapshot del documento (`mxd`/`df` apuntan a la copia). Perfecto para análisis,
> consultas complejas y exports; **no sirve para mutar la sesión viva** (para eso
> están las tools nativas `set_*`, `add_layer`, …). A cambio, un script largo no
> congela ArcMap. Devuelve `RESULT`, stdout y avisos.

---

## Series de planos (Data Driven Pages)

| Tool | Qué hace | Modo |
|---|---|---|
| `list_ddp` | ¿Hay Data Driven Pages?, nº páginas, campo índice, valores | snapshot |
| `export_ddp` | Exporta el atlas a PDF: todas / rango / lista de valores; multipágina o 1 PDF por página | snapshot |
| `goto_ddp_page` | Encuadra la vista al extent de una página (por nº o valor de índice) | snapshot + nativo |
| `list_layout_elements` | Lista elementos del layout (texto, leyenda, imagen) con nombre/tipo | nativo |
| `set_text_element` | Cambia el texto de un elemento (título, fecha, nº plano) | nativo |
| `set_definition_query` | Fija/limpia la def. query de una capa (planos temáticos por filtro) | nativo |
| `set_layer_visibility` | Enciende/apaga capa o grupo (la leyenda del layout se actualiza) | nativo |
| `export_view_png` | Exporta la vista activa (o el layout con `modo="layout"`) a PNG | nativo |
| `export_jpg` | Exporta el layout a JPG (dpi 230 por defecto — series de planos ligeras) | nativo |

> **Matiz de `goto_ddp_page`:** el atlas vivo de la sesión **no pagina** (la API de
> Data Driven Pages solo existe en arcpy, que corre sobre el snapshot). La tool lee el
> extent de la página pedida en el snapshot y **encuadra la vista viva a ese extent**:
> el resultado es un encuadre aproximado, no un cambio de página real del atlas. Los
> elementos dinámicos del layout (título de página, flechas) no cambian — para series
> de planos usa `set_text_element` + `set_definition_query`/`set_layer_visibility`,
> o exporta directamente con `export_ddp`.
>
> **Atlas grandes:** `export_ddp` con `paginas="ALL"` sobre cientos de páginas puede
> superar el timeout estándar de 60 s — exporta por lista de valores o por rango.
>
> **Truco QA de `set_text_element`:** llamar con `buscar=""` devuelve en el error la
> lista de todos los textos del layout — útil para localizar el elemento a tocar.

---

## Capas y datos

Todas **nativas sobre la sesión viva** (los cambios se ven al instante; las consultas
respetan definition query y selección, igual que la tabla de atributos).

| Tool | Qué hace |
|---|---|
| `select_by_attribute` | Selección por SQL sobre una capa (NEW/ADD/REMOVE/SUBSET) |
| `clear_selection` | Limpia la selección |
| `get_unique_values` | Valores únicos de un campo (iterar planos por categoría) |
| `count_features` | Conteo (total o con `where`; honra def. query y selección) |
| `list_fields` | Campos de capa/tabla (nombre, tipo) |
| `get_layer_info` | Detalle de una capa: campos, tipo geom, extent, CRS, count |
| `get_layer_features` | Lee FILAS de atributos (respeta def. query/selección) |
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
| `set_raster_symbology` | RÁSTER, clasificado (2-32 clases) o estirado |
| `apply_symbology_from_layer` | Aplica un `.lyr` plantilla ya preparado |

Cada una acepta la capa que le toca y **redirige a la correcta si te equivocas**: pedir
simbología ráster sobre un vectorial responde nombrando las dos alternativas, y al revés.

**Categórica:** la paleta por defecto reparte tonos por el círculo cromático, no una rampa
secuencial, porque en categórico hace falta **distinguir**, no ordenar. Es reproducible, así
que la misma capa con el mismo campo sale siempre igual (importa al reexportar una serie).
Tope de 100 categorías; los NULL se omiten; un valor no listado **no se dibuja** en vez de
colarse con un color cualquiera.

**Ráster:** si la banda no tiene estadísticas, se calculan solas. Sin ellas la clasificación
falla con un `E_FAIL` de COM que no explica nada. El número real de clases puede ser menor
que el pedido si el ráster no tiene variación suficiente, y se devuelve el real.

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
| `save_mxd_as` | Guarda una copia en otra ruta | nativo |
| `list_broken_data_sources` | Capas y tablas standalone con ruta rota (muy común en ArcMap) | nativo |
| `repair_data_source` | Reapunta la fuente de una capa (verifica releyendo la fuente) | nativo |

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
> **no congelan la interfaz**. Usan el timeout amplio (`ARCMAP_GP_TIMEOUT`, 30 min).
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
> en `execute_arcpy`. (Los marcadores espaciales/bookmarks, p. ej., no tienen tool
> aún: técnicamente posible vía ArcObjects, se añadirá si el caso de uso se repite.)
