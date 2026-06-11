# Instalación de arcmap-mcp (end-to-end)

Guía única para dejar el MCP funcionando en cualquiera de los clientes soportados
(Claude Code, Claude Desktop, Gemini CLI, Antigravity, OpenCode). El orden importa:
el servidor MCP no sirve de nada si el add-in dentro de ArcMap no está escuchando.

> Recuerda la arquitectura: `cliente IA → arcmap_mcp_server.py (Py3) → socket
> 127.0.0.1:27179 → add-in .NET (dentro de ArcMap)`.
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

## Paso 2 — Instalar el add-in .NET en ArcMap (una vez)

1. **Cierra ArcMap** (el instalador de add-ins no actualiza una sesión abierta).
2. Doble clic en `addin\dist\arcmap-mcp.esriaddin` ▸ **Install**.
3. Abre ArcMap → debe aparecer la barra **arcmap-mcp** (si no está visible:
   *Customize ▸ Toolbars ▸ arcmap-mcp*), con 4 botones:
   **Iniciar / Detener / Estado / Acerca de**.

**Si el instalador falla en silencio** (no hay barra ni rastro en el Add-In Manager):
el `.esriaddin` es un ZIP — extráelo a
`%USERPROFILE%\Documents\ArcGIS\AddIns\Desktop10.5\{51f4ce63-6bcf-49b2-ae3a-ba2c79ea3e1a}\`
(la carpeta debe contener `Config.xml` e `Install\`) y reabre ArcMap.

4. Pulsa **Iniciar** → MessageBox «Puente iniciado». El add-in escucha en
   `127.0.0.1:27179` y registra su actividad en `C:\MCP_Logs\arcmap-mcp.log`
   (si algo no va, ese log es el primer sitio donde mirar).

> **Una sola instancia de ArcMap.** Con dos ArcMap abiertos a la vez, el segundo
> puede arrancar **sin el add-in** y sin avisar (el primero bloquea la DLL). Trabaja
> con una única instancia.
>
> **Requisito del análisis arcpy:** las herramientas `execute_arcpy`, Data Driven
> Pages y análisis ambiental usan el **Python 2.7 de ArcGIS Desktop**
> (`C:\Python27\ArcGIS10.5\python.exe`, instalado con ArcMap). Si tu instalación está
> en otra ruta, defínela en la variable de entorno `ARCMAP_PYTHON27`.

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
> El bloque `env` es **opcional**: solo si cambias host/puerto/timeouts (p. ej.
> `"env": {"ARCMAP_GP_TIMEOUT": "3600"}`).

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

> Trusted Workspaces / permisos: este server escribe en disco (exporta PDF/JPG/PNG);
> habilítalo solo donde confíes.

---

## Paso 4 — Verificar

Con el add-in escuchando (Paso 2), en el chat del cliente pide la herramienta `ping`.
Debe responder con la versión del add-in, el documento abierto, el mapa activo y el
nº de capas. Luego `list_layers` → devuelve tus capas. Para feedback visual,
`get_canvas_screenshot` devuelve el mapa como imagen inline.

> Si `ping` no responde, casi siempre es que el puente no está iniciado (Paso 2,
> botón **Iniciar**) o que el cliente no se reinició tras editar su config (el
> registro MCP se fija al arrancar el cliente; reinícialo por completo, no basta con
> reconectar el server). Tercer sospechoso: `C:\MCP_Logs\arcmap-mcp.log`.

---

## Resumen de rutas

| Qué | Ruta |
|---|---|
| Repo (recomendado) | `C:\mcp\arcmap-mcp` |
| Add-in .NET (instalador) | `C:\mcp\arcmap-mcp\addin\dist\arcmap-mcp.esriaddin` |
| Add-in instalado | `%USERPROFILE%\Documents\ArcGIS\AddIns\Desktop10.5\{51f4ce63-…}\` |
| Log del add-in | `C:\MCP_Logs\arcmap-mcp.log` |
| Intérprete del servidor (venv LOCAL) | `C:\Users\<USUARIO>\AppData\Local\arcmap-mcp\venv\Scripts\python.exe` |
| Script del servidor MCP | `C:\mcp\arcmap-mcp\src\arcmap_mcp_server.py` |
| Python 2.7 del runner arcpy | `C:\Python27\ArcGIS10.5\python.exe` (override: `ARCMAP_PYTHON27`) |
| Config Claude Code | `C:\Users\<USUARIO>\.claude.json` (global, clave `mcpServers`) |
| Config Claude Desktop | `C:\Users\<USUARIO>\AppData\Roaming\Claude\claude_desktop_config.json` |
| Config Gemini CLI | `C:\Users\<USUARIO>\.gemini\settings.json` |
| Config Antigravity | `C:\Users\<USUARIO>\.gemini\antigravity\mcp_config.json` |
| Config OpenCode | `C:\Users\<USUARIO>\.config\opencode\opencode.json` (clave `mcp`) |

---

## Geoprocesos pesados y análisis arcpy

Las herramientas de análisis (`execute_arcpy`, `raster_index`, `hydrology`,
`contours`, `topographic_profile`, `least_cost_path`, y las de Data Driven Pages)
corren **fuera del proceso de ArcMap**, sobre una copia temporal del documento:
pueden tardar minutos pero **no congelan la interfaz**. `run_geoprocessing` (nativo)
y los exports sí ocupan la interfaz mientras duran. Tres cosas a tener en cuenta:

1. **Extensiones.** Requieren licencia activa:
   - **Spatial Analyst** → `raster_index`, `hydrology`, `least_cost_path`.
   - **3D Analyst** → `contours`, `topographic_profile`.

   Actívalas en ArcMap: *Customize ▸ Extensions*. Si falta la licencia, la tool
   devuelve un error claro.
2. **Timeout.** Si un proceso es muy largo (rásters nacionales, atlas enormes) y el
   server reporta *timeout*, sube `ARCMAP_GP_TIMEOUT` (segundos, 1800 por defecto) en
   el bloque `env` de la config del cliente. El proceso sigue vivo aunque el server
   deje de esperar.
3. **Atlas grandes.** `export_ddp` con `paginas="ALL"` sobre cientos de páginas puede
   superar el timeout estándar de 60 s: exporta por lista de valores o por rango.

## Variables de entorno

Todas son opcionales (hay valores por defecto). Están documentadas en
[`../.env.example`](../.env.example): host/puerto/timeouts del servidor
(`ARCMAP_BRIDGE_HOST`, `ARCMAP_BRIDGE_PORT`, `ARCMAP_BRIDGE_TIMEOUT`,
`ARCMAP_GP_TIMEOUT`) y ruta del Python 2.7 del runner (`ARCMAP_PYTHON27`). El server
las lee del entorno o del bloque `env` de la config MCP; el add-in, del entorno del
usuario (defínelas antes de abrir ArcMap).
