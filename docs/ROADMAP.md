# Roadmap — arcmap-mcp

> Estado del proyecto y posibles mejoras. Catálogo de herramientas en [`TOOLS.md`](TOOLS.md);
> instalación y arquitectura en [`INSTALL.md`](INSTALL.md) y el README.

## Estado

Estable. La arquitectura actual —add-in .NET dentro de ArcMap (ArcObjects nativo +
runner arcpy out-of-process) y servidor FastMCP en Python 3— está validada end-to-end
en ArcMap 10.5, con **57 herramientas** que incluyen el análisis ambiental (índices
espectrales, hidrología, curvas, perfiles 3D y ruta de mínimo coste) y series de planos
reales de decenas de páginas. La regresión en sesión viva cubre unas **34** de ellas con
105 comprobaciones; el resto se ha ejercitado a mano, no automáticamente, y ahí es donde
toca seguir ampliando cobertura.

El servidor es registrable en cinco clientes IA (Claude Code, Claude Desktop, Gemini
CLI, Antigravity, OpenCode).

## Posibles mejoras

- **LiDAR**: importar LAS, construir TIN y derivar MDT/MDS y CHM. Se añadirá como
  tools `lidar_*` cuando un flujo concreto lo justifique.
- **Cobertura de pruebas**: ampliar la batería conforme se añadan herramientas;
  validar el acceso a ArcMap en otra máquina (vía túnel SSH/Tailscale al loopback).
- **Documentación**: capturas del add-in.

Ya hechas (estaban aquí y conviene no volver a proponerlas): el **arranque
automático** del puente (desplegable *Autoarranque* de la barra, preferencia por
usuario en `HKCU\Software\pedralcg\arcmap-mcp`), los **marcadores espaciales**
(`get_bookmarks`, `add_bookmark`, `remove_bookmark`, `goto_bookmark`) y la guía
rápida *"tu primer plano en 5 pasos"* del README.

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
