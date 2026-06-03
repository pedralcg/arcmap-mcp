# Roadmap — arcmap-mcp

> Estado del proyecto y posibles mejoras. Catálogo de herramientas en [`TOOLS.md`](TOOLS.md);
> instalación y arquitectura en [`INSTALL.md`](INSTALL.md) y el README.

## Estado

Estable. La arquitectura de sesión viva (puente socket + `SetTimer` en el hilo principal
de ArcMap, servidor FastMCP en Python 3) está validada end-to-end en ArcMap 10.5, y las
**47 herramientas** están probadas por llamada cableada real, incluido el análisis
ambiental (índices espectrales, hidrología, curvas, perfiles y ruta de mínimo coste).

El servidor es registrable en cinco clientes IA (Claude Code, Claude Desktop, Gemini CLI,
Antigravity, OpenCode) y trae un Add-In con barra de botones para levantar el puente sin
tocar la consola.

## Posibles mejoras

- **LiDAR**: importar LAS, construir TIN y derivar MDT/MDS y CHM. Se añadirá como tools
  `lidar_*` cuando un flujo concreto lo justifique.
- **Arranque automático** del puente al abrir ArcMap (hoy es un clic en el botón *Iniciar*).
- **Cobertura de pruebas**: ampliar la batería conforme se añadan herramientas; validar
  rutas con acentos/ñ y el acceso a ArcMap en otra máquina (vía VPN).
- **Documentación**: guía rápida "primer plano en 5 pasos" y capturas del Add-In.

## Limitaciones por diseño

- Atado a **Python 2.7 / ArcMap 10.x** (fin de vida). El proyecto tiene sentido mientras
  una organización siga en ArcMap; quien migre a ArcGIS Pro o QGIS ya tiene MCP propios.
- `arcpy.mapping` (10.x) no expone toda la API de ArcObjects: no puede crear/activar Data
  Driven Pages ni gestionar marcadores (solo leer, navegar y exportar atlas existentes).
- Un geoproceso pesado **congela la GUI de ArcMap mientras dura**, porque `arcpy` debe
  ejecutarse en el hilo principal. Es inherente al modelo, no un fallo (detalle en el
  README, *Límites conocidos*).
