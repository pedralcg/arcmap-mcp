# Changelog — arcmap-mcp

Formato inspirado en [Keep a Changelog](https://keepachangelog.com/es/); versionado
[SemVer](https://semver.org/lang/es/).

## [2.4.3] — 2026-06-11

El puente dentro de ArcMap se reescribe como **add-in .NET (C#/ArcObjects)**,
sustituyendo al puente Python 2.7 embebido de la 1.x. El servidor MCP, el protocolo,
los nombres de las herramientas y la configuración de los clientes **no cambian**:
quien ya tenía el server registrado solo necesita instalar el add-in.

**Por qué.** La 1.x validó el concepto, pero ejecutar un runtime Python embebido
dentro del proceso de ArcMap resultó ser fuente de corrupción de heap
(crashes `0xc0000374` al cerrar, `Normal.mxt` corrupto). El add-in .NET usa la vía
de extensión soportada por la plataforma: COM gestionado por el CLR, sin intérprete
embebido. El código arcpy que sigue haciendo falta (código arbitrario, Data Driven
Pages, análisis ambiental) corre ahora **fuera de proceso**, sobre un snapshot del
documento.

### Añadido
- `export_jpg`: exporta el layout a JPG (48 herramientas en total).
- Cancelación con **ESC** de render y exports (`ITrackCancel`).
- Las herramientas arcpy (execute_arcpy, DDP, ambientales) ya **no congelan la
  interfaz** de ArcMap: corren en un proceso aparte.
- Log de diagnóstico del add-in en `C:\MCP_Logs\arcmap-mcp.log`.
- Instalación de un clic del puente: `addin\dist\arcmap-mcp.esriaddin`.

### Cambiado
- Puente reimplementado en .NET nativo (ArcObjects vía CLR); mismo protocolo, mismo
  puerto (27179), mismos contratos JSON.
- `execute_arcpy` y las herramientas de Data Driven Pages operan sobre un **snapshot**
  del documento: leen el estado real de la sesión, pero sus cambios al .mxd no
  repercuten en la sesión viva (las salidas a disco sí; detalle en `docs/TOOLS.md`).
- `goto_ddp_page` pasa a ser un encuadre aproximado a la página (el atlas vivo no se
  pagina desde fuera de arcpy).
- `calculate_geometry` corre nativa en el proceso de ArcMap (evita el bloqueo de
  esquema sobre fuentes cargadas en la TOC y honra definition query y selección).
- Las herramientas que mutan el mapa notifican el cambio a la vista: las leyendas
  configuradas con "only show classes that are visible" se actualizan también en
  exports automatizados.
- El add-in escucha solo en `127.0.0.1`; el acceso remoto se documenta vía túnel
  cifrado.

### Retirado
- El puente Python embebido (`arcmap_bridge.py`) y su add-in de botonera: el add-in
  .NET cubre ambos papeles. Quedan disponibles en el historial del repositorio
  (tag `v1.0.0`).
- Variables de entorno del puente retirado: `ARCMAP_BRIDGE_BIND`, `ARCMAP_MCP_DIR`.

## [1.0.0] — 2026-06-03

Primera versión pública: puente Python 2.7 embebido en ArcMap (socket + sondeo en el
hilo principal) + servidor MCP externo (Python 3 + FastMCP). 47 herramientas probadas
end-to-end sobre ArcMap 10.5, registrables en 5 clientes IA (Claude Code, Claude
Desktop, Gemini CLI, Antigravity, OpenCode).
