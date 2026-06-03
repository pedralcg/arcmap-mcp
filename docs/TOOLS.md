# Catálogo de herramientas — arcmap-mcp

> **Filosofía híbrida.** `execute_arcpy` (código arbitrario) es la base universal:
> cualquier cosa de ArcMap 10.x se puede hacer con él. Los **wrappers** de esta lista
> existen solo para lo **repetitivo y de alto valor** —sobre todo las series de planos
> (Data Driven Pages)—, no para replicar toda la API de arcpy.
>
> Patrón de implementación: handler `h_<tool>(params)` en `arcmap_bridge.py`
> (Py2.7, `arcpy.mapping as MAP`) + entrada en `HANDLERS` + `@mcp.tool()` en
> `arcmap_mcp_server.py` (Py3) que hace `_client.send("<tool>", {...})`.

**47 herramientas**, todas probadas por llamada cableada real sobre ArcMap 10.5.

---

## Esenciales

| Tool | Qué hace |
|---|---|
| `ping` | Versión/build de ArcGIS; confirma que el puente está vivo |
| `get_arcmap_info` | `.mxd`, data frames, df activo, escala |
| `list_layers` | Capas del df (nombre, visibilidad, fuente, def. query) |
| `zoom_to_layer` | Encuadra a una capa y refresca el canvas |
| `export_pdf` | Exporta el layout a PDF |
| `refresh` | Refresca vista activa + TOC |
| `execute_arcpy` | **Código arbitrario** (arcpy, mxd, df, RESULT) — la base de la filosofía híbrida |

---

## Series de planos (Data Driven Pages)

| Tool | Qué hace | arcpy.mapping 10.x |
|---|---|---|
| `list_ddp` | ¿Hay Data Driven Pages?, nº páginas, campo índice, valores | `mxd.dataDrivenPages` (`.pageCount`, `.pageNameField`) |
| `export_ddp` | Exporta el atlas a PDF: todas / rango / lista de valores; multipágina o 1 PDF por página | `ddp.exportToPDF(out, "ALL"/"RANGE"/"CURRENT")` |
| `list_layout_elements` | Lista elementos del layout (texto, leyenda, imagen) con nombre/tipo | `MAP.ListLayoutElements(mxd, "TEXT_ELEMENT")` |
| `set_text_element` | Cambia el texto de un elemento (título, fecha, nº expediente) | `elem.text = ...` |
| `goto_ddp_page` | Sitúa el atlas en una página (por nº o valor de índice) y refresca | `ddp.currentPageID = ...` |
| `set_definition_query` | Fija/limpia la def. query de una capa (planos temáticos por filtro) | `lyr.definitionQuery = ...` |
| `set_layer_visibility` | Enciende/apaga capa o grupo | `lyr.visible = True/False` |
| `export_view_png` | Exporta la vista activa (o el layout completo con `modo="layout"`) a PNG | `MAP.ExportToPNG(mxd, out, df)` |

---

## Capas y datos

| Tool | Qué hace | arcpy |
|---|---|---|
| `select_by_attribute` | Selección por SQL sobre una capa | `arcpy.SelectLayerByAttribute_management` |
| `clear_selection` | Limpia la selección | `...SelectLayerByAttribute(lyr,"CLEAR_SELECTION")` |
| `get_unique_values` | Valores únicos de un campo (iterar planos por categoría) | `da.SearchCursor` |
| `count_features` | Conteo (total o con `where`) | `arcpy.GetCount_management` |
| `list_fields` | Campos de capa/tabla (nombre, tipo) | `arcpy.ListFields` |
| `get_layer_info` | Detalle de una capa: campos, tipo geom, extent, CRS, count | `arcpy.Describe` + `ListFields` |
| `get_layer_features` | Lee FILAS de atributos (respeta def. query/selección) | `arcpy.da.SearchCursor` |
| `add_layer` | Añade capa desde shp/fgdb/raster al df | `MAP.Layer(...)` + `MAP.AddLayer` |
| `remove_layer` | Quita capa por nombre | `MAP.RemoveLayer` |
| `apply_symbology_from_layer` | Aplica un `.lyr` (estilos canónicos) a una capa | `MAP.ApplySymbologyFromLayer` |
| `set_scale` | Fija la escala del df activo | `df.scale = ...` |

---

## Geoprocesamiento y mantenimiento

| Tool | Qué hace | arcpy |
|---|---|---|
| `run_geoprocessing` | Geoproceso nominal por nombre + params (sin escribir código) | `getattr(arcpy, mod).tool(*args)` |
| `save_mxd` | Guarda el .mxd en su ruta actual | `mxd.save()` |
| `save_mxd_as` | Guarda una copia en otra ruta | `mxd.saveACopy()` |
| `list_broken_data_sources` | Lista capas/tablas con ruta rota (muy común en ArcMap) | `MAP.ListBrokenDataSources` |
| `repair_data_source` | Reapunta el workspace de una capa | `lyr.findAndReplaceWorkspacePath` |

---

## Visualización y catálogo

| Tool | Qué hace | arcpy |
|---|---|---|
| `get_canvas_screenshot` | Captura la vista/layout y la devuelve como **IMAGEN INLINE** — el agente la ve al instante | `ExportToPNG` → base64 → FastMCP `Image` |
| `list_data_frames` / `set_active_df` | Lista/cambia el data frame activo | `MAP.ListDataFrames` |
| `set_extent` | Encuadra a coords o al extent de una capa/selección | `df.extent = ...` |
| `describe_data` | Describe un dataset en disco (CRS, tipo, campos) | `arcpy.Describe` |
| `get_workspace` / `set_workspace` | Lee/fija `arcpy.env.workspace` | `arcpy.env` |
| `list_feature_classes` | Feature classes del workspace (incl. datasets) | `arcpy.ListFeatureClasses` |
| `list_tables` | Tablas del workspace | `arcpy.ListTables` |
| `list_rasters` | Rásters del workspace | `arcpy.ListRasters` |

> **Nota:** los marcadores espaciales (bookmarks) no tienen tool porque `arcpy.mapping`
> de ArcMap 10.x no expone su API (sí lo hace `arcpy.mp` de ArcGIS Pro). Requeriría
> ArcObjects/comtypes; queda fuera de alcance.

---

## Análisis ambiental y teledetección

> Requieren **Spatial Analyst** o **3D Analyst** y operan sobre datos **en disco**. Son
> geoprocesos pesados: usan un timeout amplio (`ARCMAP_GP_TIMEOUT`, 30 min) y mientras
> corren **congelan la GUI** de ArcMap (hilo único — ver *Límites conocidos* en el README).
> Por defecto añaden el resultado al data frame activo (`anadir_al_mapa=True`).

| Tool | Qué hace | Extensión | arcpy |
|---|---|---|---|
| `raster_index` | **Índice espectral con nombre** (NDVI, GNDVI, NDRE, NDWI, MNDWI, NDMI, NBR, SAVI, EVI) desde bandas | Spatial | `arcpy.sa` (álgebra de ráster) |
| `hydrology` | `cuenca` (Fill→FlowDir→FlowAcc→Watershed/Basin) · `red_drenaje` · `inundacion` | Spatial | `arcpy.sa` Hydrology |
| `contours` | Curvas de nivel desde MDT (export DXF opcional) | 3D | `arcpy.ddd.Contour` |
| `topographic_profile` | Perfil topográfico: línea 2D → línea 3D sobre superficie | 3D | `arcpy.ddd.InterpolateShape` |
| `least_cost_path` | Ruta de mínimo coste (origen→destino sobre ráster de fricción) | Spatial | `arcpy.sa` CostDistance + CostPath |
| `calculate_geometry` | Área/perímetro/longitud/coords/centroide a campos (in place) | — | `arcpy.AddGeometryAttributes_management` |

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

> **¿Cuándo añadir un wrapper nuevo?** Si la operación se va a repetir y/o es propensa a
> errores de Py2.7 escrita a mano → merece wrapper. Si es puntual → queda en `execute_arcpy`.
