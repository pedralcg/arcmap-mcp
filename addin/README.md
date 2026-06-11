# arcmap-mcp — add-in .NET (código fuente)

> Código C# del add-in que corre dentro de ArcMap. Atiende al servidor MCP
> (`src/arcmap_mcp_server.py`) por TCP en `127.0.0.1:27179`: un objeto JSON por
> conexión, respuesta única y cierre. La instalación y el uso están en el README
> raíz y en `docs/INSTALL.md`; este documento es para quien quiera construirlo o
> modificarlo.

## Arquitectura

```
Cliente IA --stdio--> arcmap_mcp_server.py (Python 3 + FastMCP)
   --TCP 127.0.0.1:27179--> McpServer (TcpListener, thread de fondo, DENTRO del add-in)
       ├─ Dispatcher.InvokeAsync --> hilo STA de ArcMap --> ArcObjects (RCW del CLR)
       └─ subprocess Python 2.7 --> runner.py + arcpy sobre snapshot del .mxd
```

Reglas de diseño:

1. `Dispatcher.CurrentDispatcher` se captura en `OnStartup` (hilo UI) y queda estático
   — es la única vía segura hacia ArcObjects desde el thread del socket.
2. Una petición en vuelo (`SemaphoreSlim(1,1)`); si está ocupado → `busy` inmediato,
   sin encolar.
3. Timeout por tipo de comando (60 s estándar, 1800 s geoprocesos/exports); el timeout
   responde error sin matar el listener.
4. `try/catch` en cada handler: ninguna excepción escapa al message pump de ArcMap.
5. Log a fichero (`C:\MCP_Logs\arcmap-mcp.log`) — sin Visual Studio, el log es el
   debugger.
6. Lo que arcpy cubre mejor que ArcObjects (código arbitrario, Data Driven Pages,
   análisis con Spatial/3D Analyst) se ejecuta **fuera de proceso**: `runner.py`
   (Python 2.7, embebido en la DLL como recurso) sobre una copia temporal del
   documento — sin runtime Python dentro del proceso de ArcMap y sin congelar la GUI.

## Estructura

```
addin/
├── ArcmapMcp.AddIn/
│   ├── Config.xml            ← manifiesto del add-in (Target Desktop 10.5)
│   ├── McpExtension.cs       ← arranque/parada, captura del Dispatcher
│   ├── McpServer.cs          ← TcpListener + dispatch de comandos
│   ├── StaDispatcher.cs      ← InvokeAsync con timeout hacia el hilo STA
│   ├── Buttons.cs            ← barra: Iniciar / Detener / Estado / Acerca de
│   ├── Handlers/             ← implementación de los comandos (ArcObjects + runner)
│   ├── Python/runner.py      ← runner arcpy out-of-process (recurso embebido)
│   └── Images/               ← iconos de la barra
├── build.ps1                 ← build + empaquetado sin Visual Studio
└── dist/arcmap-mcp.esriaddin ← paquete instalable
```

## Build (sin Visual Studio)

```powershell
.\build.ps1   # dotnet build (net45, x86) + empaqueta dist\arcmap-mcp.esriaddin
```

- El `.csproj` referencia los ensamblados ESRI 10.5 del GAC y de `Desktop10.5\bin`
  con `Private=False` (los provee ArcMap en runtime).
  `Microsoft.NETFramework.ReferenceAssemblies` permite compilar net45 sin el
  targeting pack instalado.
- El `.esriaddin` es un ZIP plano (`Config.xml` + `Install\` + `Images\`).

## Reglas de desarrollo

- **Editar la DLL exige reiniciar ArcMap**: el add-in se carga al arrancar y la DLL
  queda bloqueada (instala siempre con ArcMap cerrado).
- **Una sola instancia de ArcMap** durante el desarrollo: con dos abiertas, la segunda
  arranca sin el add-in en silencio (la primera bloquea la DLL del AssemblyCache).
- El protocolo con el servidor es un sobre JSON: éxito `{ok, result}`, error
  `{ok:false, error, traceback}` — un JSON por conexión TCP.
- Probar cada comando **por llamada MCP real** (no solo lógica aislada): los bugs
  suelen vivir en el contrato handler↔socket, no en la lógica ArcObjects.
