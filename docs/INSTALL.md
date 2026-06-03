# Instalación de arcmap-mcp (end-to-end)

Guía única para dejar el MCP funcionando en cualquiera de los clientes soportados
(Claude Code, Claude Desktop, Gemini CLI, Antigravity, OpenCode). El orden importa:
el servidor MCP no sirve de nada si el puente dentro de ArcMap no está levantado.

> Recuerda la arquitectura: `cliente IA → arcmap_mcp_server.py (Py3) → socket
> 127.0.0.1:27179 → arcmap_bridge.py (dentro de ArcMap)`.
>
> **Ruta recomendada del repo: `C:\mcp\arcmap-mcp`** (fuera de carpetas sincronizadas
> tipo Drive/Dropbox). En los ejemplos, sustituye `<USUARIO>` por tu usuario de Windows.

---

## Paso 1 — Entorno Python 3 del servidor (una vez)

Necesitas Python 3 (64 bits) con el paquete `mcp`. El lanzador lo prepara solo:

```powershell
cd C:\mcp\arcmap-mcp
.\start-arcmap-mcp.ps1        # crea el venv (LOCAL) e instala requirements.txt
```
(Ctrl+C cuando ya esté esperando al puente; el venv ya queda creado.)

> El venv se crea en `%LOCALAPPDATA%\arcmap-mcp\venv` (local, por usuario): son miles
> de archivos regenerables, así que no conviene tenerlos en el repo ni en carpetas
> sincronizadas.

Esto deja el intérprete en:
`C:\Users\<USUARIO>\AppData\Local\arcmap-mcp\venv\Scripts\python.exe`  ← lo usarás en la config del cliente.

---

## Paso 2 — Levantar el puente dentro de ArcMap 10.5

**Opción A (botón):** instala `arcmap-mcp-addin\arcmap-mcp-addin.esriaddin`
(doble clic ▸ Install) y en ArcMap pulsa **Iniciar** (barra *arcmap-mcp*).

**Opción B (una línea):** en **Geoprocessing ▸ Python**:
```python
execfile(r"C:\mcp\arcmap-mcp\src\arcmap_bridge.py")
```

Debe decir: `[arcmap-bridge] activo en 127.0.0.1:27179`.

---

## Paso 3 — Registrar el servidor en tu cliente IA

### Claude Code  → `.mcp.json` del proyecto (o config global `~/.claude.json`)
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

> **Rutas:** usa barras normales `/` (como hacen los demás MCP de GIS: qgis,
> arcgis-pro). Funcionan en Windows y evitan el doble-escape `\\`.
> El bloque `env` es **opcional**: solo si cambias host/puerto (p. ej. ArcMap en
> otra máquina: `"env": {"ARCMAP_BRIDGE_HOST": "<IP del servidor>"}`).

Mismo `command`/`args` para todos los clientes; cambia solo el **fichero** y, en
algún caso, la **clave** y el **estilo** del JSON.

### Gemini CLI  → `C:\Users\<USUARIO>\.gemini\settings.json`
Añade dentro del objeto `mcpServers` existente:
```json
"arcmap": {
  "command": "C:/Users/<USUARIO>/AppData/Local/arcmap-mcp/venv/Scripts/python.exe",
  "args": ["C:/mcp/arcmap-mcp/src/arcmap_mcp_server.py"]
}
```

### Antigravity  → `C:\Users\<USUARIO>\.gemini\antigravity\mcp_config.json`
(El path real es `…\.gemini\antigravity\`, **no** `…\.gemini\config\`.) Mismo
bloque que Gemini CLI pero con `"disabled": false` al estilo del fichero. También
desde la app: **Manage MCP Servers ▸ View raw config**.

### Claude Desktop  → `C:\Users\<USUARIO>\AppData\Roaming\Claude\claude_desktop_config.json`
Mismo bloque `arcmap` dentro de `mcpServers`. Reinicia Claude Desktop tras editar.

### OpenCode  → `C:\Users\<USUARIO>\.config\opencode\opencode.json`
Formato propio (clave `mcp`, `type: "local"`, `command` como **array**):
```json
"mcp": {
  "arcmap": {
    "type": "local",
    "command": [
      "C:/Users/<USUARIO>/AppData/Local/arcmap-mcp/venv/Scripts/python.exe",
      "C:/mcp/arcmap-mcp/src/arcmap_mcp_server.py"
    ],
    "enabled": true
  }
}
```

> Trusted Workspaces / permisos: este server escribe en disco (exporta PDF);
> habilítalo solo donde confíes.

---

## Paso 4 — Verificar

Con el puente activo (Paso 2), en el chat del cliente pide la herramienta `ping`.
Debe responder `pong` + versión `10.5`. Luego `list_layers` → devuelve tus capas.
Para feedback visual, `get_canvas_screenshot` devuelve el mapa como imagen inline.

> Si `ping` no responde, casi siempre es que el puente no está levantado (Paso 2) o
> que el cliente no se reinició tras editar su config (el registro MCP se fija al
> arrancar el cliente; reiniciarlo por completo, no basta con reconectar el server).

---

## Resumen de rutas

| Qué | Ruta |
|---|---|
| Repo (recomendado) | `C:\mcp\arcmap-mcp` |
| Intérprete del servidor (venv LOCAL) | `C:\Users\<USUARIO>\AppData\Local\arcmap-mcp\venv\Scripts\python.exe` |
| Script del servidor MCP | `C:\mcp\arcmap-mcp\src\arcmap_mcp_server.py` |
| Config Claude Code | `C:\Users\<USUARIO>\.claude.json` (global, clave `mcpServers`) |
| Config Claude Desktop | `C:\Users\<USUARIO>\AppData\Roaming\Claude\claude_desktop_config.json` |
| Config Gemini CLI | `C:\Users\<USUARIO>\.gemini\settings.json` |
| Config Antigravity | `C:\Users\<USUARIO>\.gemini\antigravity\mcp_config.json` |
| Config OpenCode | `C:\Users\<USUARIO>\.config\opencode\opencode.json` (clave `mcp`) |
| Add-In (botón) | `C:\mcp\arcmap-mcp\arcmap-mcp-addin\arcmap-mcp-addin.esriaddin` |

---

## Geoprocesos pesados (análisis ambiental)

Las tools de análisis ambiental (`raster_index`, `hydrology`, `contours`,
`topographic_profile`, `least_cost_path`, `calculate_geometry`) son geoprocesos que pueden
tardar minutos y **congelan la GUI de ArcMap mientras corren** (ver *Límites conocidos* en
el README).
Dos cosas a tener en cuenta:

1. **Extensiones.** Requieren licencia activa:
   - **Spatial Analyst** → `raster_index`, `hydrology`, `least_cost_path`.
   - **3D Analyst** → `contours`, `topographic_profile`.

   Actívalas en ArcMap: *Customize ▸ Extensions*. Si falta la licencia, la tool
   devuelve un error claro.
2. **Timeout.** Si un proceso es muy largo (LiDAR/TIN, rásters nacionales) y el server
   reporta *timeout*, sube `ARCMAP_GP_TIMEOUT` (segundos, 1800 por defecto) en el bloque
   `env` de la config del cliente. El proceso sigue vivo dentro de ArcMap aunque el
   server deje de esperar.

## Variables de entorno

Todas son opcionales (hay valores por defecto). Están documentadas en
[`../.env.example`](../.env.example): host/puerto/timeouts del puente
(`ARCMAP_BRIDGE_HOST`, `ARCMAP_BRIDGE_PORT`, `ARCMAP_BRIDGE_TIMEOUT`,
`ARCMAP_GP_TIMEOUT`), interfaz de escucha (`ARCMAP_BRIDGE_BIND`) y ruta del repo para
el Add-In (`ARCMAP_MCP_DIR`). El server las lee del entorno o del bloque `env` de la
config MCP; el puente, del entorno del usuario (defínelas antes de abrir ArcMap).
