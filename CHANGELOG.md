# Changelog — arcmap-mcp

Formato inspirado en [Keep a Changelog](https://keepachangelog.com/es/); versionado
[SemVer](https://semver.org/lang/es/).

## [2.8.3] - 2026-08-25

Actualizar deja de ser un misterio.

### Anadido
- **`ACTUALIZAR.bat`** en la raiz, espejo de `INSTALAR.bat`: con ArcMap cerrado, un doble
  clic baja los cambios (`git pull`) y reinstala las dos piezas. Detecta si la carpeta no
  es un repositorio git (caso de quien bajo el ZIP) y explica que hacer en vez de fallar
  con un error de git, y aborta sin reinstalar nada si el `git pull` no sale limpio.
- **`LEEME.txt` en la raiz**, en texto plano y sin acentos a proposito: quien extrae el ZIP
  y hace doble clic en `README.md` se encuentra markdown en crudo o el dialogo de "como
  quieres abrir este archivo"; un `.txt` lo abre el Bloc de notas sin preguntar. Contiene
  solo las dos recetas, los cuatro problemas frecuentes y a donde escribir. Deliberadamente
  minimo: sin numeros de version ni detalle por cliente, para que no se desincronice.
- **Seccion `4. Actualizar` en el `README`**, con la tabla git / ZIP y el aviso de la barra.
- **Seccion `## Actualizar` en `docs/INSTALL.md`**, que no existia: toda la guia estaba
  escrita como *una vez*, y la unica pista de como actualizar era un inciso a media
  pagina. Cubre la receta, por que el servidor Python se actualiza solo con el `git pull`
  y el add-in .NET no, que hacer sin git, y como forzar la comprobacion de version.

### Cambiado
- **El aviso de version nueva dice que hacer y lleva a las instrucciones**, en vez de
  abrir la portada del repositorio y dejar al usuario adivinando que el paquete vive en
  `addin/dist/`. Ahora nombra `ACTUALIZAR.bat`, avisa de que hay que cerrar ArcMap y
  explica por que no puede instalarse solo: mientras ArcMap esta abierto mantiene cargada
  la DLL del add-in. El badge de *Acerca de* apunta al mismo sitio.

  Salio del primer uso real del aviso, el 2026-08-25: funciono, pero dejaba al usuario a
  medio camino.

## [2.8.2] — 2026-08-25

La barra deja de imponerse en cada arranque.

### Corregido
- **La barra `arcmap-mcp` ya no se muestra sola en cada sesión** (`showInitially="false"`
  en el `<Toolbar>` de `Config.xml`). Con `showInitially="true"`, ArcMap forzaba la barra
  visible en **todos** los arranques y recolocaba el resto de barras de herramientas en
  bucle: el layout se descolocaba solo, sesión tras sesión, sin causa aparente. El
  diagnóstico costó una sesión entera porque el síntoma (barras que se mueven) no apunta
  a un add-in, y porque `Normal.mxt` solo se escribe al salir limpio de ArcMap, así que
  la corrección manual del layout se perdía en cada cierre anómalo.

  **Al actualizar, la barra no aparecerá tras instalar.** Actívala una vez en
  *Customize ▸ Toolbars ▸ arcmap-mcp* y ArcMap recordará su sitio.

## [2.8.1] — 2026-07-29

El runner dice en qué fase está, `execute_arcpy` falla con motivo, y la extensión deja
de pelearse consigo misma.

### Añadido
- **Traza de fase del runner.** Desde el add-in, un runner atascado abriendo un
  documento y uno reclasificando un ráster de 4 GB son el mismo `python.exe` que no
  termina: el reloj de pared no distingue colgado de trabajando. Ahora el runner anota
  su fase (`importando arcpy`, `leyendo job`, `abriendo documento` con el tamaño del
  mxd, `ejecutando codigo`, `serializando salida`, `terminado`) en un fichero al lado
  del `out`, y el add-in lo sondea cada segundo: cada cambio de fase queda en el log con
  su marca de tiempo. Si salta el timeout, el error ya no es un «timeout» mudo sino
  «atascado en la fase 'abriendo documento (5,9 MB)' desde hace 412 s», y el `job_*.json`
  y la traza se conservan como evidencia. La escritura de la fase no puede romper el job:
  falla en silencio si falla.

### Corregido
- **`execute_arcpy` podía tardar media hora en dar señales de vida.** El tope del
  subproceso arcpy era de 1800 s para *todas* las operaciones del runner, incluida
  `execute_code`, que es interactiva: el cliente MCP se rinde a los 120 s, así que el
  fallo no llegaba nunca en forma de error sino de cuelgue mudo, y dejaba un
  `python.exe` huérfano vivo que además impedía cerrar ArcMap. Ahora `execute_code`
  tiene su propio tope (**`ARCMAP_EXEC_TIMEOUT`, 900 s** por defecto) y el resto de
  operaciones del runner (DDP, hidrología, índices, que sí son legítimamente largas)
  conserva el amplio (`ARCMAP_SUBPROCESS_TIMEOUT`, 1800 s). Al agotarse, el subproceso
  se mata, se conserva el `job_*.json` como evidencia y el error dice en qué fase se
  quedó y qué hacer. Del lado del servidor MCP, `execute_arcpy` deja de usar `GP_TIMEOUT`
  y espera 930 s, deliberadamente **por encima** del tope del add-in, para que gane el
  error con fase en vez de un corte de socket que dejaría el runner huérfano vivo.
  El tope es una red de seguridad contra runners eternos, no un presupuesto de
  rendimiento: 900 s y no 300 porque abrir el mxd de 5,9 MB medido costó 324 s, y un
  tope de 300 mataría un `usar_documento=true` legítimo.
- **La extensión se cargaba 3-4 veces por arranque y todas menos la primera decían que
  el puente no había arrancado.** El servidor vivía en un campo de *instancia*: las
  cargas 2..N veían su propio `_server` a null, intentaban bindear el 27179 y morían con
  `SocketException`, dejando un «Autoarranque activo pero el puente no arrancó» que era
  falso — la primera carga lo había levantado bien. El puente es un recurso del proceso,
  así que su estado pasa a estático, con guard de instancia única en `OnStartup` y
  propiedad explícita: solo la carga que lo arrancó lo para en su `OnShutdown`, para que
  descargar una instancia espuria no tumbe el puente bueno. La comprobación de versión
  en GitHub también corre una sola vez por proceso, y no cuatro.

### Notas técnicas
- Medición del 2026-07-29 sobre un mxd de 5,9 MB: el mismo `RESULT = 2 + 2` tarda
  **6,4 s sin documento y 324,5 s con él** (51x). Abrir el documento es todo el coste;
  el código ejecutado no pinta nada. De ahí que el mensaje de timeout empuje a
  `usar_documento=false`.

## [2.8.0] — 2026-07-24

Aviso de nuevas versiones y reporte de problemas desde la barra.

### Añadido
- **Aviso de nueva versión.** Al abrir ArcMap, el add-in comprueba en segundo plano
  (como mucho una vez al día, sin bloquear el arranque) si hay una versión más reciente
  publicada como tag en GitHub. Si la hay, muestra un aviso **una sola vez** por versión
  y ofrece abrir el repositorio. El estado («al día» / «hay vX disponible») queda visible
  en el botón **Estado** y en **Acerca de**. Sin internet o con límite de la API de
  GitHub, no pasa nada: es silencioso y tolerante a fallo. La comprobación se cachea en
  `HKCU\Software\pedralcg\arcmap-mcp`.
- **Botón «Reportar».** Abre un formulario con el diagnóstico de la sesión
  (versiones, entorno, contadores) ya preparado, y permite reportar por **issue de
  GitHub** o por **email**, ambos pre-rellenados. El diagnóstico es **no sensible**: no
  incluye nombres de capas, título del documento ni rutas. El **log** (que sí puede
  contener datos de proyecto) nunca se adjunta solo: se prepara aparte en el Escritorio,
  con un aviso para revisarlo antes de compartirlo.

### Notas técnicas
- La comprobación de versión fuerza **TLS 1.2** (`ServicePointManager.SecurityProtocol`):
  .NET Framework 4.5 no lo habilita por defecto y la API de GitHub lo exige. La petición
  envía `User-Agent` (obligatorio para la API) y usa un timeout corto.

## [2.7.0] — 2026-07-23

Simbología graduada nativa sobre la capa viva.

### Añadido
- **`set_graduated_symbology`**: aplica colores graduados (class breaks) a una capa
  de la sesión por un campo numérico, in-process y persistente (se guarda con
  `save_mxd`). Cubre el hueco entre las dos vías que había: `execute_arcpy` no puede
  cambiar el renderer de la sesión —opera sobre una copia del documento y lo
  descarta— y `apply_symbology_from_layer` exige un `.lyr` plantilla. Ahora se
  construye el `IClassBreaksRenderer` directamente. Parámetros: `campo`, `num_clases`
  (2-32, defecto 5), `metodo` (natural_breaks | quantile | equal_interval |
  geometrical_interval | standard_deviation), `color_desde`/`color_hasta` (RGB) y
  `tamano`. Símbolo según geometría (línea, relleno o marcador). El histograma se
  calcula sobre todos los valores del campo en la fuente (no honra definition query
  ni selección; para un subconjunto, fíjalo con una definition query permanente).

## [2.6.1] — 2026-07-23

Arreglo del autoarranque, que no llegaba a guardarse.

### Corregido
- **La casilla «Autoarranque» revertía a «No» sola, ~2 s después de ponerla en «Sí».**
  El `MessageBox` de confirmación se mostraba *dentro* de `OnSelChange` del ComboBox; al
  cerrarse un diálogo modal, ArcMap revierte la selección del combo al primer ítem y
  vuelve a disparar `OnSelChange` (esta vez con «No»), que reescribía la preferencia
  recién guardada. La escritura al registro nunca falló —el log mostraba «guardado como
  SI (relectura: SI)» e inmediatamente «guardado como NO (relectura: NO)»—: el problema
  era ese re-disparo espurio. Ahora el aviso se pospone al *message pump*
  (`StaDispatcher.Post`, prioridad Background) para que corra cuando el combo ya ha
  consolidado su selección, y el guard `_sincronizando` ignora cualquier re-disparo que
  llegue entretanto.

## [2.6.0] — 2026-07-22

`execute_arcpy` deja de copiar el documento cuando no hace falta. En sesiones
grandes esa copia bloqueaba ArcMap durante minutos y el puente parecía caído.

### Cambiado
- **`execute_arcpy` solo copia el documento si el código lo usa.** Antes hacía un
  `SaveAsDocument` del mxd en el hilo de ArcMap *siempre*; con decenas de capas y
  ortofotos eso tarda minutos, deja la interfaz sin responder y hace que cualquier
  petición posterior (incluido `ping`) parezca un puente muerto — cuando el código
  típico (geoprocesar un ráster, recorrer una tabla) no toca el documento para nada.
  Ahora se decide leyendo si el código menciona `mxd`, `df` o `mapping`, y el nuevo
  parámetro **`usar_documento`** fuerza la decisión en cualquiera de los dos sentidos.
  Sin documento, `mxd` y `df` no existen dentro del código.
- **La copia del documento ya no se serializa desde ArcMap: se copia el `.mxd` del
  disco.** `SaveAsDocument` era la única parte de `execute_arcpy` que corría *dentro*
  del proceso de ArcMap, y serializar un documento con capas pesadas o fuentes rotas
  podía **tumbar ArcMap entero** (visto en un proyecto de 84 capas con ortofotos ECW).
  Copiar un fichero no puede hacer eso, y además es instantáneo. La contrapartida —los
  cambios sin guardar de la sesión no se reflejan— se avisa en la respuesta, y
  `serializar_sesion=True` recupera el comportamiento anterior cuando se necesite.
- El tiempo máximo para serializar el documento sube de 60 s a 600 s: en sesiones
  pesadas, 60 s se agotaban antes de terminar una copia legítima.

### Añadido
- El log registra el inicio, la duración y el tamaño de cada copia del documento, y
  la preferencia de autoarranque que se lee al arrancar y la que se guarda al
  cambiarla (con relectura de comprobación).

## [2.5.0] — 2026-07-22

Instalación de un solo comando, autoarranque opcional del puente y el arreglo de un
fallo que dejaba `execute_arcpy` inservible en la mitad de las llamadas.

### Añadido
- **`install.ps1`**: instalador end-to-end e idempotente. Detecta la versión de ArcMap
  y el Python 2.7 de ArcGIS, prepara el entorno del servidor, instala el add-in en la
  ruta que ArcMap lee de verdad y registra el servidor en los clientes indicados
  (`-Clientes todos`) respetando el resto de su configuración. Incluye
  `-SoloVerificar` (diagnóstico con ping real al puente) y `-Desinstalar`.
- **Casilla «Autoarranque»** en la barra del add-in: con ella marcada, el puente se
  levanta solo al abrir ArcMap. La preferencia se guarda por usuario en el registro
  (`HKCU\Software\pedralcg\arcmap-mcp`), así que sobrevive a las actualizaciones.

### Corregido
- **`execute_arcpy` fallaba con `Error reading JObject … line 0, position 0`** siempre
  que el resultado era ASCII puro. En Python 2, `json.dumps(ensure_ascii=False)`
  devuelve `str` cuando no hay ningún carácter no-ASCII, y escribir ese `str` en un
  fichero abierto con codificación explícita lanza `TypeError` **después** de haber
  truncado el fichero de salida: el add-in recibía cero bytes. Bastaba una tilde en la
  respuesta para que la misma llamada funcionara, de ahí que el fallo pareciera
  aleatorio. Ahora la salida se serializa entera antes de abrir el fichero, se escribe
  de forma atómica y se coacciona a texto Unicode de forma explícita.
- Salida ilegible o vacía del proceso arcpy: el add-in devuelve un error accionable
  (código de salida, `stderr` y primeros bytes) en vez de propagar la excepción del
  parser, y **conserva** los ficheros del trabajo fallido para poder diagnosticarlo.
- **`start-arcmap-mcp.ps1` no funcionaba con la PowerShell que trae Windows**: usaba el
  operador `??`, que solo existe en PowerShell 7. Ahora es compatible con la 5.1.
- Los scripts se distribuyen como UTF-8 **con BOM** y sin tipografía fuera de ASCII:
  Windows PowerShell 5.1 lee los ficheros sin BOM como cp1252, y ahí un guion largo se
  descompone en caracteres entre los que aparece una comilla tipográfica, que PowerShell
  acepta como delimitador de cadena y rompe el script entero. `empaquetar.ps1` verifica
  ahora que todos los scripts parseen con la 5.1 antes de generar el paquete.
- Los flujos de salida y de error del proceso arcpy se leen ambos en asíncrono: con
  más de unos pocos kilobytes de mensajes, la tubería se llenaba y el proceso quedaba
  colgado hasta agotar el tiempo de espera.

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
