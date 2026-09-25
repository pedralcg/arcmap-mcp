# CLAUDE.md — arcmap-mcp

Servidor MCP que conduce un ArcMap 10.5 **vivo**. Dos piezas: el add-in .NET dentro
de ArcMap (`addin/`, ArcObjects por CLR) y el servidor MCP en Python 3
(`src/arcmap_mcp_server.py`), que habla con el add-in por TCP en `127.0.0.1:27179`.
Los geoprocesos arcpy van fuera de proceso, con el Python 2.7 de ArcGIS
(`addin/ArcmapMcp.AddIn/Python/runner.py`). La arquitectura y las herramientas
están en `README.md`; la historia de cada versión, en `CHANGELOG.md`.

## Flujo de ramas (GitHub Flow)

`main` es lo publicado y **solo cambia por pull request con el CI en verde**. La
regla la impone GitHub (ruleset `main protegida`): PR obligatorio, check
`tests (py3 + py2.7)` obligatorio y al día con `main`, sin force-push ni borrado.
Sin revisores obligatorios: con un solo mantenedor nadie podría aprobar sus
propios PR.

1. `git switch -c <tipo>/<tema>` desde `main` actualizado (`feat/…`, `fix/…`, `docs/…`).
2. Commits en Conventional Commits, en español.
3. **Antes de abrir el PR, en local**:
   - `python -m unittest discover -s tests` con el venv del MCP
     (`%LOCALAPPDATA%\arcmap-mcp\venv`). Es lo mismo que corre el CI.
   - Si se toca el add-in: `addin\build.ps1`, instalar el `.esriaddin`, **reiniciar
     ArcMap** y pasar `tests/regresion_sesion_viva.py` (y `regresion_ddp.py` si toca
     exports o Data Driven Pages) contra ese ArcMap. Pegar el resumen en el PR.
     El CI **no** compila el C#: enlaza ArcObjects de Desktop 10.5, que no existe en
     un runner de GitHub.
4. `git push -u origin <rama>` y `gh pr create`. Merge desde GitHub cuando el CI
   esté en verde (squash), y borrar la rama.
5. **Versiones**: la versión vive en `addin/ArcmapMcp.AddIn/Config.xml` y en el
   `.csproj` (`<Version>`), más su entrada en `CHANGELOG.md`; se suben **por PR**.
   El tag (`vX.Y.Z`) se crea sobre `main` **después** del merge, nunca en una rama,
   y el release lleva el zip de `empaquetar.ps1`.

## Gotchas que ya han costado sesiones

- **«Arreglado en el repo» no es «arreglado»**: ArcMap carga el `.esriaddin`
  instalado, no el fuente. Tras compilar, instalar y reiniciar ArcMap antes de
  probar nada.
- El CI prueba el runner py2.7 con un **arcpy de mentira**
  (`tests/casos_runner_py27.py`). Lo que dependa de arcpy de verdad solo se ve en
  local.
- `PENDIENTE.local.md` es backlog interno ignorado por git: no se publica.
