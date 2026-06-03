# arcmap-mcp

Servidor MCP local para **ArcMap 10.4–10.8** (validado en **10.5**). 100% abierto,
libre y soberano. Permite a un agente IA (Claude Code, Claude Desktop, Gemini CLI,
Antigravity, OpenCode…) conducir una **sesión viva de ArcMap** — listar capas,
ejecutar arcpy, encuadrar, simbolizar, exportar series de planos y **ver el canvas** —
igual que hacen los MCP de QGIS y ArcGIS Pro, pero para ArcMap legacy (que ninguno
de esos cubre).

## Arquitectura (sesión viva, dos piezas)

Mismo patrón que QGIS MCP (open source) — copiado, no inventado:

```
Cliente IA (Claude Code / Desktop / Gemini / Antigravity / OpenCode)
        │  protocolo MCP (stdio)
        ▼
arcmap_mcp_server.py   ← servidor MCP externo (Python 3 + FastMCP)
        │  socket TCP local  127.0.0.1:27179   ("el túnel")
        ▼
arcmap_bridge.py       ← puente DENTRO de ArcMap (Python 2.7)
  arcpy + arcpy.mapping.MapDocument("CURRENT")
        │
        ▼
ArcMap ABIERTO y vivo  →  canvas, capas, layout, exportación
```

- **El puente** corre dentro de ArcMap y toca el documento **vivo** vía
  `MapDocument("CURRENT")`.
- **El servidor externo** expone las `@mcp.tool()` y reenvía cada comando por socket.
- **Clave técnica:** ArcMap no es Qt → no hay `QTimer` como en QGIS, y su intérprete
  embebido (Py2.7/MFC) **no atiende sockets desde un hilo de fondo**. El puente sondea
  el socket con `user32.SetTimer` en el **hilo principal**, de modo que `arcpy` y
  `MapDocument("CURRENT")` se ejecutan en el hilo correcto. Validado end-to-end en 10.5.

## Estructura del repo

```
arcmap-mcp/
├── src/      arcmap_bridge.py · arcmap_mcp_server.py   ← núcleo (puente + servidor)
├── docs/     INSTALL.md · TOOLS.md · ROADMAP.md
├── tests/    test_bridge.py
├── arcmap-mcp-addin/              ← Python Add-In (barra de botones)
├── start-arcmap-mcp.ps1 · requirements.txt · LICENSE · README.md
```

| Archivo | Dónde corre | Qué es |
|---|---|---|
| `src/arcmap_bridge.py` | **dentro de ArcMap** (Py2.7) | El socket vivo + handlers arcpy |
| `src/arcmap_mcp_server.py` | externo (Py3) | Servidor MCP que registras en tu cliente IA |
| `start-arcmap-mcp.ps1` | Windows | Lanzador: prepara venv, vigila el túnel, hace ping |

> **Ruta de instalación recomendada: `C:\mcp\arcmap-mcp`** (fuera de carpetas
> sincronizadas tipo Drive/Dropbox). Ajusta las rutas de los ejemplos si instalas
> en otra ubicación.

## Herramientas MCP

**47 herramientas**, todas probadas por llamada cableada real sobre ArcMap 10.5 (ver
`docs/TOOLS.md` para el catálogo completo con firmas y ejemplos):

- **Esenciales:** `ping` · `get_arcmap_info` · `list_layers` · `zoom_to_layer` ·
  `export_pdf` · `refresh` · **`execute_arcpy`** (código arbitrario con `mxd`/`df` vivos).
- **Series de planos (Data Driven Pages):** `list_ddp` · `export_ddp` ·
  `list_layout_elements` · `set_text_element` · `goto_ddp_page` · `set_definition_query` ·
  `set_layer_visibility` · `export_view_png`.
- **Capas y datos:** `select_by_attribute` · `clear_selection` · `get_unique_values` ·
  `count_features` · `list_fields` · `get_layer_info` · `get_layer_features` · `add_layer` ·
  `remove_layer` · `apply_symbology_from_layer` · `set_scale`.
- **Geoprocesamiento y mantenimiento:** `run_geoprocessing` · `save_mxd` · `save_mxd_as` ·
  `list_broken_data_sources` · `repair_data_source`.
- **Visualización y catálogo:** **`get_canvas_screenshot`** (imagen INLINE, el agente
  ve el mapa) · `describe_data` · `list_data_frames` / `set_active_df` · `set_extent` ·
  `get_workspace` / `set_workspace` · `list_feature_classes` / `list_tables` / `list_rasters`.
- **Análisis ambiental y teledetección** (geoprocesos pesados, requieren Spatial/3D
  Analyst): **`raster_index`** (índices espectrales con nombre: NDVI, GNDVI, NDRE, NDWI,
  MNDWI, NDMI, NBR, SAVI, EVI — con mapeo de bandas Sentinel-2 / Landsat) · `hydrology`
  (cuencas, red de drenaje, inundación) · `contours` · `topographic_profile` ·
  `least_cost_path` · `calculate_geometry`.

La filosofía es **híbrida**: `execute_arcpy` es la base universal (cualquier cosa de
ArcMap 10.x se puede hacer con él) y los wrappers existen solo para lo repetitivo y de
alto valor.

## Puesta en marcha

### 1. Levantar el puente dentro de ArcMap

**Opción A (recomendada) — botón en la barra de herramientas.** Instala el Add-In
`arcmap-mcp-addin\arcmap-mcp-addin.esriaddin` (doble clic ▸ Install). En ArcMap aparece
la barra **arcmap-mcp** con 4 botones: **Iniciar / Detener / Estado / Acerca de**.
Pulsa **Iniciar**. Detalle en `arcmap-mcp-addin\INSTRUCCIONES.md`.

**Opción B — una línea en la ventana de Python** (sin instalar nada):
**Geoprocessing › Python** →
```python
execfile(r"C:\mcp\arcmap-mcp\src\arcmap_bridge.py")
```
Para pararlo: `stop()`.

Ambas responden al instante:
`[arcmap-bridge] activo en 127.0.0.1:27179`.

### 2. Levantar / vigilar el túnel
```powershell
.\start-arcmap-mcp.ps1            # prepara venv, espera al puente, hace ping
.\start-arcmap-mcp.ps1 -Server    # además arranca el servidor MCP (standalone)
```
El lanzador reintenta hasta que el puente aparece, así que puedes correrlo antes de
arrancar el puente en ArcMap: te guía y se conecta solo cuando esté vivo.

### 3. Registrar en tu cliente IA
Ejemplo Claude Code (`.mcp.json` del proyecto o config global). Sustituye `<USUARIO>`
por tu nombre de usuario de Windows:
```json
{
  "mcpServers": {
    "arcmap": {
      "command": "C:/Users/<USUARIO>/AppData/Local/arcmap-mcp/venv/Scripts/python.exe",
      "args": ["C:/mcp/arcmap-mcp/src/arcmap_mcp_server.py"]
    }
  }
}
```
La guía completa de los 5 clientes está en `docs/INSTALL.md`.

### 4. Verificar
Con el puente vivo, pide por MCP la herramienta `ping` → debe devolver `pong` +
versión `10.5`. Luego `list_layers` → tus capas. Todo OK.

## Acceso remoto (opcional)
Si ArcMap corre en otra máquina (p. ej. vía Tailscale):
- En ArcMap: `set ARCMAP_BRIDGE_BIND=0.0.0.0` antes de abrirlo (escucha en red).
- En el cliente: `ARCMAP_BRIDGE_HOST=<IP del servidor>` y
  `.\start-arcmap-mcp.ps1 -BridgeHost <IP del servidor>`.

> ⚠️ **Seguridad — lee esto antes de poner `BIND=0.0.0.0`.** El puente expone
> `execute_arcpy`, es decir **ejecución de código Python arbitrario** dentro de ArcMap (y
> por tanto en la máquina que lo aloja). Con `127.0.0.1` (valor por defecto) el socket solo
> es accesible desde el propio equipo y es seguro. Con `0.0.0.0` queda accesible para
> **cualquiera que alcance ese puerto en la red**, lo que equivale a ejecución remota de
> código (RCE) sin autenticación. **No uses `0.0.0.0` en redes abiertas** (oficina, WiFi
> pública). Úsalo solo dentro de una **red privada cifrada (Tailscale/VPN)** o detrás de un
> firewall que restrinja el puerto a hosts de confianza. El puente no implementa
> autenticación por diseño: la frontera de seguridad es la red.

## Límites conocidos y rendimiento

Son consecuencia directa de la arquitectura (un puente que ejecuta `arcpy` en el
**hilo principal** de ArcMap, porque es el único hilo donde `MapDocument("CURRENT")`
y `arcpy` funcionan en el intérprete embebido). Conviene conocerlos:

- **Un geoproceso pesado congela la GUI mientras dura.** No es un bug: es lo mismo que
  pasaría si lo lanzaras a mano en ArcMap. Afecta sobre todo a las tools de análisis
  ambiental (índices sobre rásters grandes, cuencas, curvas, coste) y a
  `run_geoprocessing`. ArcMap se
  "descongela" al terminar. *No es evitable* sin cambiar el modelo (mover `arcpy` a otro
  hilo lo rompe). Para lotes muy largos, considera ejecutarlos fuera de horas.
- **Timeout de geoprocesos.** Las tools pesadas usan un timeout amplio
  (`ARCMAP_GP_TIMEOUT`, **30 min** por defecto); las rápidas, `ARCMAP_BRIDGE_TIMEOUT`
  (60 s). Si una tool pesada supera su timeout, el server informa de "timeout" **pero el
  proceso sigue corriendo dentro de ArcMap**. Sube `ARCMAP_GP_TIMEOUT` para procesos muy
  largos (LiDAR/TIN, rásters nacionales). Aparte, tu cliente IA puede tener su propio
  timeout de herramienta MCP.
- **Extensiones.** `raster_index`, `hydrology`, `least_cost_path` requieren **Spatial
  Analyst**; `contours`, `topographic_profile` requieren **3D Analyst**. Si no hay
  licencia disponible, la tool devuelve un error claro (actívalas en *Customize ▸
  Extensions*).
- **Un comando por conexión.** El puente atiende una orden por conexión de socket
  (simple y robusto). No hay paralelismo de comandos: las tools se ejecutan en serie.
- **Editar un handler exige reiniciar ArcMap.** El puente se importa una vez y queda
  cacheado en `sys.modules`; el botón *Detener ▸ Iniciar* solo reabre el socket, **no
  reimporta el código**. Si modificas `src/arcmap_bridge.py`, **cierra y reabre ArcMap**
  para que el cambio entre (regla solo de desarrollo; no afecta al uso normal).

## Estado
- [x] Arquitectura validada end-to-end en ArcMap 10.5 (`SetTimer` en hilo principal)
- [x] 47 herramientas probadas por llamada cableada real, incluido el análisis ambiental
      (índices espectrales, hidrología, curvas, perfiles 3D y ruta de mínimo coste)
- [x] Add-In con barra de 4 botones (Iniciar/Detener/Estado/Acerca de)
- [x] Registrable en 5 clientes (Claude Code/Desktop, Gemini CLI, Antigravity, OpenCode)
- [ ] (Opcional) arranque automático del puente al abrir ArcMap

## Soporte y servicios

El proyecto es **libre y abierto** (MIT): puedes usarlo, modificarlo y desplegarlo sin
coste. Está pensado para organizaciones que siguen atadas a ArcMap 10.x y quieren
automatizar su trabajo cartográfico con agentes IA sin esperar a migrar a ArcGIS Pro.

Si tu equipo necesita ayuda para ponerlo en producción, lo mantiene quien lo ha
construido —técnico GIS y desarrollador— y ofrece, como servicio:

- **Instalación y puesta en marcha** en tu entorno (clientes IA, Add-In, red/Tailscale).
- **Herramientas a medida**: wrappers nuevos para tus flujos concretos (series de planos,
  índices, modelos de geoprocesamiento propios).
- **Integración** con tus datos y plantillas (.mxd, estilos `.lyr`, geodatabases).
- **Formación** del equipo para sacarle partido en el día a día.

Contacto: **pedro@pedralcg.dev** · [pedralcg.dev](https://pedralcg.dev)

## Licencia
MIT — © 2026 Pedro Alcoba Gómez · [pedralcg.dev](https://pedralcg.dev) · GitHub [@pedralcg](https://github.com/pedralcg)
