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
3. Timeout por tipo de comando: 60 s estándar, 600 s guardar el documento, 1800 s
   geoprocesos nativos y exports, y un techo de 2700 s para los handlers de fondo (que
   por dentro llevan sus propios topes de subproceso). El timeout responde error sin
   matar el listener. El relay Python espera SIEMPRE un poco más que el add-in en cada
   grupo, para que gane el error con diagnóstico y no el corte mudo de socket; un test
   (`TestNoEmpatarConElAddIn`) lee estos números del `.cs` y falla si alguien los empata.
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
│   ├── Buttons.cs            ← barra (6 controles): Iniciar · Detener · Estado ·
│   │                            Autoarranque (desplegable) · Reportar · Acerca de
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
- **Una sola instancia de ArcMap** durante el desarrollo. No porque la segunda se
  quede sin add-in —se carga en todas—, sino porque el **puerto es único**: lo agarra
  el primer ArcMap donde se pulse *Iniciar*, y en el resto `Start()` falla al bindear.
  `McpExtension.OnStartup` lo trata como caso normal (lo registra en el log y deja
  seguir trabajando), así que el síntoma es sutil: crees estar hablando con la ventana
  que tienes delante y estás hablando con la otra. Para dos puentes simultáneos,
  `ARCMAP_BRIDGE_PORT` distinto en cada proceso (leída del entorno al arrancar).
  Ojo aparte: ArcMap instancia la extensión **varias veces por arranque** (3-4), por
  eso el servidor vive en un campo **estático** con guard de instancia única.
- El protocolo con el servidor es un sobre JSON: éxito `{ok, result}`, error
  `{ok:false, error, traceback}` — un JSON por conexión TCP.
- Probar cada comando **por llamada MCP real** (no solo lógica aislada): los bugs
  suelen vivir en el contrato handler↔socket, no en la lógica ArcObjects.
