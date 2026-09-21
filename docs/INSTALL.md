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

## Vía rápida: `INSTALAR.bat`

Con **ArcMap cerrado**, doble clic en `INSTALAR.bat` (en la raíz del repo). Prepara el
entorno, instala el add-in y registra el servidor en los clientes IA detectados.

Desde terminal es lo mismo:

```powershell
cd C:\mcp\arcmap-mcp
powershell -ExecutionPolicy Bypass -File .\install.ps1   # clientes detectados automáticamente
.\install.ps1 -Clientes todos                            # o los cinco, detectados o no
.\install.ps1 -Clientes claude-desktop                   # o solo uno
```

> **Si has descargado el ZIP** en vez de clonar: desbloquéalo antes de extraerlo
> (clic derecho en el ZIP ▸ *Propiedades* ▸ *Desbloquear*). Windows marca lo que viene
> de internet y ArcMap puede negarse a cargar un add-in marcado, sin decir por qué. El
> instalador desbloquea también por su cuenta lo que encuentra, por si acaso.
>
> El `-ExecutionPolicy Bypass` es necesario porque Windows no ejecuta scripts `.ps1`
> con su configuración de fábrica; `INSTALAR.bat` ya lo incluye.

El instalador detecta la versión de ArcMap y el Python 2.7 de ArcGIS por registro,
prepara el entorno del servidor, copia el add-in a la carpeta que ArcMap lee de verdad
y añade el bloque `arcmap` a la configuración de cada cliente sin tocar el resto (hace
copia de seguridad antes de escribir). Es idempotente: repítelo tras cada actualización
del add-in.

Después, abre ArcMap, activa la barra `arcmap-mcp` en *Customize ▸ Toolbars* (no
aparece sola) y pon su desplegable **Autoarranque** en **Sí**: el puente se levantará
solo en cada sesión. Para comprobar que todo responde:

```powershell
.\install.ps1 -SoloVerificar       # entorno, versión del add-in instalado y ping real al puente
.\install.ps1 -Desinstalar         # retira add-in, venv, entradas de los clientes y restos locales
```

Si algo no encaja en tu equipo, los pasos manuales equivalentes son los de abajo.

---

## Actualizar

Con **ArcMap cerrado**, doble clic en `ACTUALIZAR.bat` (en la raíz del repo). Baja los
cambios y reinstala las dos piezas. Desde terminal es lo mismo:

```powershell
cd C:\mcp\arcmap-mcp
git pull
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.ps1` es **idempotente**: se puede repetir siempre que haga falta. Refresca el venv
(`pip install -r requirements.txt`), borra la carpeta del add-in anterior antes de copiar la nueva
(sin restos de versiones viejas), desbloquea los ficheros marcados como «procedentes de internet» y
vuelve a registrar el bloque `arcmap` en los clientes detectados. Si ArcMap está abierto se niega
con un aviso, en vez de dejar una instalación a medias.

### Por qué hay dos piezas y solo una da guerra

- **El servidor MCP (Python) se actualiza con el `git pull` y ya está.** Tu cliente IA tiene
  registrado el `python.exe` del venv apuntando al `.py` **de esta carpeta**, así que corre siempre
  el código actual. Basta con reiniciar la sesión del cliente. Solo necesitas volver a pasar
  `install.ps1` si cambió `requirements.txt`.
- **El add-in .NET sí exige cerrar ArcMap.** Vive copiado en
  `%USERPROFILE%\Documents\ArcGIS\AddIns\Desktop<versión>\{51f4ce63-…}\`, y ArcMap mantiene su
  DLL cargada mientras está abierto: no hay forma de sustituirlo en caliente. Por eso el aviso de
  versión nueva no puede instalar nada por su cuenta.

### Tras actualizar el add-in

**La barra `arcmap-mcp` no aparecerá sola**, y es deliberado desde la 2.8.2 (ver el CHANGELOG).
Actívala una vez en *Customize ▸ Toolbars ▸ arcmap-mcp* y ArcMap recordará su posición. Comprueba
con el botón **Estado** que la versión es la que esperas.

### Si descargaste el ZIP en vez de clonar

No hay `git pull`: baja el ZIP nuevo, **desbloquéalo antes de extraerlo** (clic derecho ▸
*Propiedades* ▸ *Desbloquear*), extráelo sobre la misma carpeta y ejecuta `INSTALAR.bat`. Clonar con
git ahorra este paso en cada actualización.

### Comprobar en qué versión estás

```powershell
.\install.ps1 -SoloVerificar     # versión del add-in instalado, estado del venv y ping al puente
```

El add-in avisa por su cuenta: consulta los tags del repo **una vez al día** y, si hay versión
mayor, lo dice una sola vez por versión (más el indicador en *Estado* y el badge en *Acerca de*). La
comprobación se cachea en `HKCU\Software\pedralcg\arcmap-mcp`; si necesitas forzarla, borra el
valor `UltimaComprobacion` y reinicia ArcMap.

---

## Paso 1 — Entorno Python 3 del servidor (una vez)

Necesitas **Python 3.10 o superior** (64 bits) con el paquete `mcp`. No hace falta que
esté en el PATH ni que lo instales aparte si ya tienes QGIS o ArcGIS Pro: `install.ps1`
busca el intérprete en el lanzador `py`, en el PATH, en las instalaciones de python.org,
en `QGIS *\apps\Python3*`, en OSGeo4W y en el entorno `arcgispro-py3`, y descarta los
que se queden por debajo de 3.10. Si no encuentra ninguno, ofrece instalarlo con
`winget`. Ojo: el Python **2.7** que trae ArcMap no sirve aquí; ese es el del análisis
arcpy, dentro del add-in.

El lanzador también prepara el entorno por su cuenta:

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
3. Abre ArcMap y activa la barra **arcmap-mcp** en *Customize ▸ Toolbars ▸ arcmap-mcp*.
   Trae 6 controles: **Iniciar · Detener · Estado · Autoarranque · Reportar ·
   Acerca de** (*Autoarranque* es un **desplegable** Sí/No, no una casilla).

   > **Desde la 2.8.2 la barra no aparece sola tras instalar, y es deliberado.** El
   > add-in declaraba `showInitially="true"`, que fuerza la barra visible en **cada**
   > arranque; ArcMap recolocaba entonces el resto de barras en bucle, sesión tras
   > sesión. Hay que activarla **una vez** y ArcMap ya recuerda la posición. Si vienes
   > de la 2.8.1 o anterior y tus barras estaban descolocadas, esto es lo que lo
   > causaba.

**Si el instalador falla en silencio** (no hay barra ni rastro en el Add-In Manager):
el `.esriaddin` es un ZIP — extráelo a
`%USERPROFILE%\Documents\ArcGIS\AddIns\Desktop10.5\{51f4ce63-6bcf-49b2-ae3a-ba2c79ea3e1a}\`
(la carpeta debe contener `Config.xml` e `Install\`) y reabre ArcMap.

4. Pulsa **Iniciar** → MessageBox «Puente iniciado». O pon el desplegable
   **Autoarranque** en **Sí** y el puente se levantará solo en cada sesión de ArcMap
   (la preferencia se guarda por usuario en `HKCU\Software\pedralcg\arcmap-mcp`).
   El add-in escucha en
   `127.0.0.1:27179` y registra su actividad en `C:\MCP_Logs\arcmap-mcp.log`
   (si algo no va, ese log es el primer sitio donde mirar).

> **Una sola instancia de ArcMap.** El add-in **sí se carga en todas las ventanas** —
> eso no es el problema. Lo que es único es el **puerto**: lo agarra el primer ArcMap
> donde pulses *Iniciar*, y en los demás el puente no levanta (queda anotado en
> `C:\MCP_Logs\arcmap-mcp.log`, sin romper nada). Consecuencia práctica: con varios
> ArcMap abiertos, el servidor MCP ve **uno solo**, el del puente, y los otros le son
> invisibles. Trabaja con una única instancia; si de verdad necesitas dos puentes a la
> vez, desde la 2.10.0 puedes darle otro puerto al segundo con `ARCMAP_BRIDGE_PORT`
> (definida antes de abrir ese ArcMap) y apuntar allí a otro cliente.
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
2. **Timeout.** Si el server reporta *timeout*, sube **la variable del grupo al que
   pertenece esa tool** en el bloque `env` de la config del cliente (tabla completa
   abajo, en *Variables de entorno*). El trabajo sigue vivo en ArcMap aunque el server
   deje de esperar. Dos avisos que ahorran tiempo:
   - `run_geoprocessing`, `calculate_geometry` y los export van por
     `ARCMAP_GP_TIMEOUT`; **las DDP y las ambientales, no**: esas van por
     `ARCMAP_FONDO_TIMEOUT`.
   - **`execute_arcpy` tampoco va por `ARCMAP_GP_TIMEOUT`**, sino por
     `ARCMAP_EXEC_TIMEOUT_CLIENTE` (930 s) en el server y `ARCMAP_EXEC_TIMEOUT`
     (900 s) en el add-in. Subir `ARCMAP_GP_TIMEOUT` no alarga nada allí.
3. **Atlas grandes.** `export_ddp` sobre cientos de páginas puede agotar la espera aun
   con el timeout amplio: exporta por lista de `valores` o por `rango`. (No existe un
   parámetro `paginas`; la firma es
   `export_ddp(salida, modo, rango, valores, un_pdf_por_pagina, dpi)`.)

## Variables de entorno

Todas son opcionales (hay valores por defecto) y están documentadas una a una en
[`../.env.example`](../.env.example). Lo que importa aquí es **quién lee cada cosa**,
porque no es intercambiable:

| Variable | La lee | Default | Para qué |
|---|---|---|---|
| `ARCMAP_BRIDGE_HOST` | server | `127.0.0.1` | Extremo local del túnel, si no es el loopback |
| `ARCMAP_BRIDGE_PORT` | **server y add-in** | `27179` | Puerto del puente (1024-65535) |
| `ARCMAP_BRIDGE_TIMEOUT` | server | `60` | Tools rápidas: consultas, capas, simbología, navegación |
| `ARCMAP_GP_TIMEOUT` | server | `1860` | Lo que ocupa el hilo de ArcMap: `run_geoprocessing`, `calculate_geometry` y los 3 export |
| `ARCMAP_FONDO_TIMEOUT` | server | `2760` | Handler de fondo: las 3 de Data Driven Pages y las 5 ambientales |
| `ARCMAP_SAVE_TIMEOUT` | server | `630` | `save_mxd` y `save_mxd_as` |
| `ARCMAP_EXEC_TIMEOUT_CLIENTE` | server | `930` | **Solo `execute_arcpy`** |
| `ARCMAP_EXEC_SESION_TIMEOUT_CLIENTE` | server | `1560` | `execute_arcpy` con `serializar_sesion=True` (600 s de copia + 900 s de ejecución + margen) |
| `ARCMAP_EXEC_TIMEOUT` | add-in | `900` | Tope del subproceso de `execute_arcpy` |
| `ARCMAP_SUBPROCESS_TIMEOUT` | add-in | `1800` | Tope del resto de trabajos del runner (DDP, ambientales) |
| `ARCMAP_PYTHON27` | add-in | `C:\Python27\ArcGIS10.5\python.exe` | Ruta del Python 2.7 del runner arcpy |

Las del **server** van en el bloque `env` de la config MCP del cliente (o en el entorno
desde el que ese cliente arranca). Las del **add-in** van en las **variables de usuario
de Windows, definidas antes de abrir ArcMap**: el add-in corre dentro del proceso de
ArcMap, que no ve nada del bloque `env` del cliente MCP — ponerlas ahí no hace nada.

Los topes del server están **por encima** de los del add-in a propósito: así vence
primero el del add-in, que mata el subproceso y devuelve un error diciendo en qué fase
se quedó, en vez de un corte mudo de socket que además deja el runner huérfano vivo (y
un runner huérfano impide cerrar ArcMap).

`ARCMAP_BRIDGE_PORT` es la única que hay que poner **en los dos extremos**: el add-in y
el servidor la leen con el mismo nombre, cada uno de su propio entorno, así que
definirla solo en uno los deja sin encontrarse. Sirve de vía de escape cuando un ArcMap
que ya no responde deja cogido el `27179`. Se admite `1024`–`65535`; un valor inválido
se ignora, se avisa en `C:\MCP_Logs\arcmap-mcp.log` y se vuelve al puerto por defecto.

**No existe una variable para el bind**: el add-in escucha solo en `127.0.0.1` por
decisión de seguridad, y el acceso a un ArcMap remoto se hace por túnel al loopback de
esa máquina (ver *Acceso remoto* en el README).
