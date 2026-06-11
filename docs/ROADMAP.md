# Roadmap — arcmap-mcp

> Estado del proyecto y posibles mejoras. Catálogo de herramientas en [`TOOLS.md`](TOOLS.md);
> instalación y arquitectura en [`INSTALL.md`](INSTALL.md) y el README.

## Estado

Estable. La arquitectura actual —add-in .NET dentro de ArcMap (ArcObjects nativo +
runner arcpy out-of-process) y servidor FastMCP en Python 3— está validada end-to-end
en ArcMap 10.5, y las **48 herramientas** están probadas por llamada cableada real,
incluido el análisis ambiental (índices espectrales, hidrología, curvas, perfiles 3D
y ruta de mínimo coste) y series de planos reales de decenas de páginas.

El servidor es registrable en cinco clientes IA (Claude Code, Claude Desktop, Gemini
CLI, Antigravity, OpenCode).

## Posibles mejoras

- **LiDAR**: importar LAS, construir TIN y derivar MDT/MDS y CHM. Se añadirá como
  tools `lidar_*` cuando un flujo concreto lo justifique.
- **Arranque automático** del puente al abrir ArcMap (hoy es un clic en el botón *Iniciar*).
- **Marcadores espaciales (bookmarks)**: posible vía ArcObjects; se añadirá si el
  caso de uso se repite.
- **Cobertura de pruebas**: ampliar la batería conforme se añadan herramientas;
  validar el acceso a ArcMap en otra máquina (vía túnel/VPN).
- **Documentación**: guía rápida "primer plano en 5 pasos" y capturas del add-in.

## Limitaciones por diseño

- Atado a **ArcMap 10.x** (fin de vida). El proyecto tiene sentido mientras una
  organización siga en ArcMap; quien migre a ArcGIS Pro o QGIS ya tiene MCP propios.
- El atlas (Data Driven Pages) **solo se puede crear/configurar a mano** en ArcMap;
  las tools lo leen, exportan y aproximan el encuadre, pero no lo crean ni lo paginan
  en vivo (la API de DDP solo existe en arcpy, que corre sobre un snapshot).
- `run_geoprocessing` (nativo) y los exports ocupan la interfaz de ArcMap mientras
  duran; el resto del análisis arcpy corre fuera de proceso y no la congela
  (detalle en el README, *Límites conocidos*).
- Dos piezas (add-in + servidor Python 3): es la arquitectura definitiva — servir
  HTTP/MCP directamente desde el add-in se evaluó y descartó (sin SDK MCP para .NET
  Framework 4.5; `HttpListener` exige permisos de administrador; los clientes stdio
  necesitarían proxy igualmente).
