# Changelog — arcmap-mcp

Formato inspirado en [Keep a Changelog](https://keepachangelog.com/es/); versionado
[SemVer](https://semver.org/lang/es/).

## [2.13.0] - 2026-09-23 (SIN PUBLICAR)

Lo que destapó el trabajo real de dos días con series de planos (lotes de 88 y de 149
documentos). El hilo común: **respuestas que dicen `ok` sin describir lo que ha pasado**.

Verificado el mismo día en sesión viva: 10 comprobaciones propias de esta versión (documento
«Sin título», cabecera coding, `result`, stdout en el error, aviso de capas rotas, `documento`
en el export, rechazo de `mxd=`) y **106/106** en la regresión general, en un documento con
servicios web. 38 casos del runner y 50 tests sin ArcMap. **No verificado:** el reintento por
E_PENDING, porque el fallo no apareció en ninguno de los 5 pases de la regresión.

La regresión corrige de paso su propia aserción de capas rotas, que comparaba el RECUENTO de
arcpy con el de ArcObjects: no cuentan lo mismo (arcpy da por rota una capa de servicio web
sin `dataSource`; ArcObjects cuenta tablas que `ListLayers` no ve). Ahora, si el recuento no
cuadra, decide por nombre y solo con capas con fuente en disco.

### Añadido
- **`export_mxd_lote`**: exporta a JPG o PDF una lista de .mxd del disco, **un proceso
  `python.exe` por documento**. Es la única vía fiable: un proceso arcpy que ya ha
  exportado un layout no vuelve a exportar otro (medido el 2026-09-22 sobre planos de
  94-141 capas: sale el primero y fallan los siguientes, haya guardado o no). No usa el
  puente y funciona con ArcMap abierto. Exporta a un temporal y mueve al final; devuelve
  bytes y `sobrescrito` por documento, y no se fía del `ok` del hijo: comprueba que el
  fichero existe, tiene tamaño y es de esta pasada. Probado sobre tres planos entregados:
  salen **idénticos byte a byte** a los del 22-sep.

### Corregido
- 🔴 **El instalador ya no deja los clientes IA apuntando a la carpeta del ZIP.** Registraba
  la ruta desde la que se ejecutaba: quien descomprimía el ZIP en Descargas y luego borraba
  esa carpeta se quedaba con un servidor que no arrancaba, y el fallo salía en una sesión
  posterior como `CONNECTION_CLOSED`. Hasta ahora solo había un aviso. Ahora, instalando
  desde un ZIP, el paquete se **copia a `C:\mcp\arcmap-mcp`** y se registra esa ruta; la
  carpeta del ZIP se puede borrar. `-Destino` elige otra ubicación. Un clon git se usa donde
  esté, y si `C:\mcp\arcmap-mcp` ya es un clon git, un ZIP no lo pisa: se para con un
  mensaje claro. Probado bajo PowerShell 5.1: copia, protección, y verificación con la
  carpeta del ZIP ya borrada. README, INSTALL, LEEME, `ACTUALIZAR.bat` y el aviso de versión
  nueva del add-in lo explican ya así: ese aviso mandaba a `ACTUALIZAR.bat` (git pull), que a
  quien descargó el ZIP no le sirve.
- 🔴 **Argumentos desconocidos se rechazan en TODAS las tools.** FastMCP los tiraba en
  silencio: `export_jpg(salida=..., mxd=<otro plano>, resolucion=230)` exportó el
  documento abierto —otro monte, otro plano— con `ok: true`. Ahora es un error que nombra
  los argumentos sobrantes y los admitidos, y el schema anuncia `additionalProperties:
  false`. Toca internos de FastMCP (`_tool_manager`, `fn_metadata`); un test lo vigila.
- **Los export dicen QUÉ han exportado** (`documento` en la respuesta de `export_pdf`,
  `export_jpg` y `export_view_png`).
- **`execute_arcpy` devuelve el `stdout` también cuando falla.** Un bucle que moría en el
  5º documento no decía que los cuatro primeros ya estaban guardados. Si el fallo es un
  `UnicodeError`, la respuesta trae una `pista` (casi siempre `str(ex)` sobre un mensaje
  con tildes).
- **`execute_arcpy` tolera `# -*- coding: utf-8 -*-`** en las dos primeras líneas (antes:
  `SyntaxError: encoding declaration in Unicode string` sin ejecutar nada). La línea se
  blanquea, no se borra, para que los números de línea del traceback sigan casando.
- **`execute_arcpy` arranca con un documento «Sin título»**: `df` vale `None` en vez de
  morir en el preámbulo con `NameError` y `ping` en verde.
- **`result` en minúscula se devuelve**, con `aviso_result`. Es la convención del MCP de
  ArcGIS Pro, y volvía `null` con el valor calculado y tirado.
- **El aviso de capas rotas ya no habla de otro documento.** Salía en las 20 llamadas de
  una sesión que editaba otros .mxd por ruta, nombrando las capas rotas del documento
  abierto —que no se tocó—. Dos cambios: `MAP`/`mapping` ya no fuerzan la copia del
  documento (el runner los inyecta siempre, así que no indicaban nada), y el runner omite
  el aviso si el código no usó la copia (`mxd`/`df` no nombrados, o reasignados). Cuando
  sale, nombra el .mxd y lista 5 capas en vez de 10.
- **`set_raster_symbology` dice cuándo ha calculado estadísticas.** Desde la 2.12.0 las
  calcula si faltan (estirado y clasificado), y eso deja huella en el fichero: los
  **píxeles no cambian** (checksum de GDAL idéntico), pero ArcMap guarda al liberar el
  ráster mínimo/máximo/media/desviación como metadatos DENTRO del TIFF y el histograma en
  un `.aux.xml`. En un TIFF de 2 GB son ~50 s la primera vez y +269 KB; después es
  instantáneo, y en una carpeta sin escritura se recalculan cada vez. Se mantiene el
  comportamiento (es información derivada e invariable, no una reclasificación), pero ya no
  es mudo: la respuesta trae `estadisticas_calculadas` con bandas y segundos.
- **E_PENDING en los export** (`0x8000000A`, el mapa sigue dibujando): hasta 3 intentos
  bombeando mensajes de ArcMap entre medias, y si no, un error que dice qué hacer. ⚠️
  **Esto TAPA EL SÍNTOMA, no explica la causa**: el disparador sigue sin identificarse
  (tres hipótesis probadas y refutadas el 2026-09-21). Que nadie lo lea como entendido.

## [2.12.0] - 2026-09-20 (SIN PUBLICAR)

Revisión completa del código. Un defecto grave y silencioso, una veintena de tamaño
medio y una pasada de documentación. Se escribió **con ArcMap cerrado** y se probó en
sesión viva el **2026-09-21**: el bloque «revisión 2026-09-20» de
`tests/regresion_sesion_viva.py` dio 75 de 77. De los dos fallos, uno era de la propia
aserción del test (comparaba las capas rotas de la copia contra cero, no contra las que
la sesión ya tenía rotas de origen) y el otro, el del multivalor, queda corregido abajo.
Probar en vivo destapó además el defecto de las **workspace factories**, que no era nuevo
pero sí invisible. La regresión creció a **105 comprobaciones**, y la aserción defectuosa
quedó corregida midiendo la línea base de capas rotas de la sesión en vez de exigir cero.

El arreglo grave está además verificado **donde se manifestaba**: sobre un mxd de producción
real de 36 capas con *Store relative pathnames*, `list_ddp`, `goto_ddp_page` y `export_ddp`
dan `snapshot_via: disco_junto_al_original`, **cero capas rotas añadidas**, un PDF de 2 MB con
datos dentro —páginas con cuadrículas de coordenadas distintas, o sea atlas paginado de
verdad— y ningún `~arcmap-mcp-snap_*.mxd` residual. Eso vive en el nuevo
`tests/regresion_ddp.py` (14 comprobaciones), separado del otro script a propósito: el atlas
exige un documento de producción y la regresión general **modifica** el documento abierto.

Verificado en vivo: el arreglo de las rutas relativas (`snapshot_via:
disco_junto_al_original`, cero capas rotas *por la copia*, sin `~arcmap-mcp-snap_*.mxd`
residual), la simbología, los exports, `save_mxd_as`, el desempate de capas homónimas y
los avisos de `execute_arcpy`. **Siguen sin probar en vivo** el cajetín agrupado, el join
con tabla unida, ESC en un export, el CRS cruzado en `zoom_to_layer` y el modo estirado
de ráster.

### Corregido — grave
- **La copia del `.mxd` rompía los documentos con rutas relativas.** Desde la 2.6.0
  `execute_arcpy`, `list_ddp`, `goto_ddp_page` y `export_ddp` trabajan sobre una copia del
  documento hecha en `%TEMP%`. Un `.mxd` con *Store relative pathnames* copiado a otra
  carpeta abre con **todas sus capas rotas** (reproducido en aislado con arcpy: con rutas
  relativas `ListBrokenDataSources` devuelve la lista entera; con absolutas, ninguna). Y no
  fallaba: `export_ddp` sacaba el atlas con su leyenda, sus marcos y ni un dato dentro.
  Ahora se lee `IMxDocument.RelativePaths`; con rutas relativas la copia se deja **junto al
  original** (`~arcmap-mcp-snap_<guid>.mxd`, oculta, borrada al terminar), y si esa carpeta
  no deja escribir se serializa desde ArcMap — **nunca** se cae a `%TEMP%`. Si la casilla
  no se puede leer se asume «relativas», que es el lado seguro. La respuesta trae
  `snapshot_via` y `capas_rotas_en_copia` (con `aviso_capas_rotas` si no es cero), para
  que si el fallo vuelve por otro camino no vuelva a ser mudo.
  - **Efecto lateral nuevo, a sabiendas:** con rutas relativas se escribe un fichero
    efímero en la carpeta de trabajo del usuario (y, si está sincronizada, viaja un
    instante a la nube). Los restos de una sesión que muriera a mitad se barren solos:
    más de 1 día junto al documento, más de 7 en `%TEMP%\arcmap-mcp`.
  - De paso: la copia de un `.mxd` de **solo lectura** heredaba el atributo y no se podía
    borrar, así que se acumulaba una por llamada. Pasaba también antes, en `%TEMP%`.
- **`audit_folder` no servía en carpetas con ñ o tilde.** `auditor_mxd.py` recibía la ruta
  en cp1252 (Python 2 en Windows), `json.dumps` la leía como UTF-8 y reventaba **fuera** del
  `try`: stdout vacío y un `"sin salida"` por documento. El «nunca lanza» del docstring era
  falso; ahora es cierto. Los errores de arcpy salen además legibles, no como `repr()`.
- **Los cursores COM no se liberaban** en `get_layer_features`, `get_unique_values`,
  `count_features` ni en el zoom a la selección. `get_layer_features` salía al llegar al
  límite con el cursor abierto a mitad de tabla. El lock duraba hasta que pasara el GC.
- **El relay cortaba antes que el add-in.** `export_pdf`, `export_jpg`, `export_view_png` y
  las tres de Data Driven Pages esperaban 60 s en el relay mientras el add-in les daba
  1800: un plano de un minuto largo recibía un «puente ocupado» falso con el export en
  marcha. Y `ARCMAP_GP_TIMEOUT` (1800) **empataba** con los topes del add-in, así que ganaba
  el corte mudo de socket: justo lo que el par 930/900 de `execute_arcpy` evita. Regla
  única ahora: el relay espera siempre más que el add-in, y un test lee los topes del `.cs`
  y falla si alguien los empata. Variables nuevas: `ARCMAP_FONDO_TIMEOUT` (2760, DDP y
  ambientales), `ARCMAP_SAVE_TIMEOUT` (630) y `ARCMAP_EXEC_SESION_TIMEOUT_CLIENTE` (1560,
  `execute_arcpy` con `serializar_sesion`). `ARCMAP_GP_TIMEOUT` pasa a 1860. En el add-in,
  guardar el documento pasa de 60 a 600 s y `calculate_geometry` entra en los comandos largos.

### Corregido
- `execute_arcpy`: mezclar `print "Número"` con un `print` de unicode perdía el resultado de
  un código que había ido bien (`StringIO` de Py2); `sys.exit()` dejaba la llamada sin
  salida; un `RESULT` con cadenas en cp1252 (`os.listdir`) no se podía serializar.
- `export_ddp` descartaba **en silencio** los `valores` que no casaban con ninguna página:
  un plano que falta sin aviso. Ahora vuelven en `valores_no_encontrados`.
- `run_geoprocessing`: una lista en `params` llegaba al geoproceso como texto JSON
  (ERROR 000732 en Merge/Union). Ahora es un multivalor. Un objeto JSON es error claro.
- **Las workspace factories se instanciaban con `new XxxWorkspaceFactoryClass()`**, que lanza
  `InvalidCastException` («no se puede convertir un objeto de tipo `System.__ComObject` al
  tipo `...WorkspaceFactoryClass`») cuando el RCW no puede castear el objeto COM registrado.
  Se llevaba por delante **todo** lo que abre un workspace —`add_layer`, `describe_data`,
  `list_feature_classes`, `set_workspace`— y, una vez roto, hasta cerrar ArcMap.
  - **No es un defecto nuevo.** Medido el 2026-09-21 sobre la **2.11.0** compilada del commit
    `714865f`: aparecen igual las dos caras, `FileGDBWorkspaceFactory` y
    `ShapefileWorkspaceFactory`. Afecta por tanto a **todas las versiones anteriores**.
  - **Lo dispara el trabajo acumulado en la sesión, no una llamada concreta.** En una sesión
    recién abierta las dos factories funcionan; empiezan a fallar tras un pase completo de la
    regresión, y cuál de las dos cae depende de lo que se haya usado. Probadas de una en una,
    seis hipótesis —geoproceso correcto, geoproceso fallido, `execute_arcpy`, un pase del
    script, número de operaciones, ruta con tildes— salieron todas limpias, así que no hay un
    disparador único identificado.
  - No se había visto porque la regresión **no cubría geodatabases** y porque nunca se
    lanzaba dos veces en la misma sesión. El primer caso de gdb se añadió ese día, al probar
    el multivalor, y saltó enseguida.
  - Ahora la factory se activa **por ProgID** (`Type.GetTypeFromProgID` + `Activator`), que
    pide el objeto por su interfaz y no pasa por ese cast. Por ProgID y no por CLSID a pelo
    para no fijar en el código GUIDs que solo viven en el registro de ArcGIS Desktop.
  - Verificado: **siete pases** de la regresión en una sola sesión sin un solo cast, y
    `add_layer` y `describe_data` de shapefile y de geodatabase vivos al terminar. Los tres
    casos de geodatabase están ya en la regresión.
- **Una capa de la TOC dentro de un multivalor se resolvía a una ruta sin extensión**, así
  que seguía dando el mismo ERROR 000732 que el arreglo anterior venía a quitar: el
  `IDatasetName.Name` de un shapefile es `parcelas`, no `parcelas.shp`, y
  `Merge C:\dir\a;C:\dir\b` no abre nada. Lo destapó la regresión en vivo del 2026-09-21
  —el único defecto real de los dos fallos— y estaba aislado en cuatro pruebas: el
  multivalor y el entrecomillado de rutas con espacios funcionaban, y una lista de rutas
  extensionless fallaba igual **sin** pasar por la resolución, que es lo que señaló a
  `DataAccess.RutaFuente`. Ahora la ruta lleva la extensión que el dataset tiene de verdad
  en disco, y solo si está: una feature class de geodatabase, un ráster (cuyo nombre ya
  trae el `.tif`), un GRID de ESRI y una capa **rota** se devuelven intactos.
  - Afectaba a `Merge`, `Union`, `Intersect` y a cualquier geoproceso multivalor **con
    capas del mapa**; con rutas escritas a mano no, que es como se había usado hasta ahora.
  - `list_layers` y `get_layer_info` informaban esa misma ruta sin extensión, o sea que
    copiar la `fuente` y pegarla en `params` reproducía el fallo. Ya la dan abrible, y las
    dos pasan por la misma función en vez de componer la ruta cada una por su lado.
  - Una ruta sin extensión **escrita a mano** sigue fallando, a propósito: es la entrada
    explícita de quien llama, y completarla sería adivinar.
- Los textos **dentro de un grupo** del layout (el cajetín agrupado) no se listaban ni se
  podían cambiar con `set_text_element`.
- Un data frame **vacío** hacía fallar `list_layers` con un error COM, y `run_geoprocessing`
  aunque todos sus argumentos fueran rutas.
- `set_raster_symbology` validaba `transparencia` **después** de cambiar el renderer y sin
  avisar a la leyenda: el bug de la 2.4.3. El modo estirado no aseguraba estadísticas.
- `set_unique_values_symbology` ignoraba la definition query, traía geometría y ordenaba
  1, 10, 11, 2. Las dos simbologías vectoriales no encontraban campos de una tabla unida.
- `list_broken_data_sources` recorría todos los data frames y `repair_data_source` solo el
  activo: una capa rota de un data frame secundario se listaba y no se podía reparar.
- El zoom a una capa o a su selección no reproyectaba al CRS del data frame.
- Exports: cancelar con ESC **borraba el fichero que ya existía**; `plano.jpeg` acababa en
  `.jpeg.jpg`; `dpi: null` reventaba y no había techo.
- Al vencer un timeout se mataba al runner pero no a su árbol de procesos (el `Kill()` de
  net45 deja vivo al nieto; comprobado). `runner.py` se extraía siempre al mismo fichero, y
  dos versiones del add-in se lo pisaban.
- `add_layer` enmascaraba el error real de una feature class con el de un ráster inexistente.
- **Instalador:** el stderr de `claude mcp remove` tumbaba la instalación en PowerShell 5.1;
  se anunciaba «registrado» sin mirar el código de salida; un JSON con comentarios abortaba
  todo (o perdía los comentarios en pwsh 7); el `.bak` único se pisaba a la segunda pasada;
  la desinstalación dejaba registro y temporales; aceptaba ArcMap 10.4, donde no carga.
- El Python 2.7 estaba fijo a `C:\Python27\ArcGIS10.5` en tres sitios del add-in: en
  10.6–10.8 `execute_arcpy` fallaba sin `ARCMAP_PYTHON27`. Ahora se busca por el registro.

### Cambia — léelo antes de actualizar
- **Un nombre de capa ambiguo es ERROR**, no «la primera que encuentre» (también en
  `remove_layer`). `list_layers` da la `ruta` de grupo de cada capa (`Grupo/Capa`) y esa
  ruta vale como identificador en cualquier tool. Dos capas homónimas en el **mismo**
  grupo se numeran (`Parcelas#1`, `Parcelas#2`). Lo mismo con dos textos de layout
  homónimos: `set_text_element` los lista numerados y se desempata con `grupo` o `indice`.
  - Estos dos desempates **no estaban en el primer arreglo**: los exigió la verificación
    adversarial, que demostró que sin ellos dos capas (o dos textos) idénticos quedaban
    inoperables para todas las tools, `remove_layer` incluida. La misma pasada cazó que
    una fecha ISO en `run_geoprocessing` caía en el error nuevo de «parámetro no
    admitido» (Newtonsoft la convierte en token `Date`), y que matar el árbol de procesos
    de un runner exige comprobar antes que el PID sigue siendo suyo (hora de arranque),
    porque Windows los reutiliza.
- **`save_mxd_as` ya no sobrescribe por defecto** (`sobrescribir=False`). Al regenerar una
  serie de `.mxd` hay que pasarlo. Los export sí pisan por defecto, y lo dicen
  (`sobrescrito`).
- **La rampa por defecto de `set_graduated_symbology` pasa de HSV a CIE Lab**, como en
  ráster desde el 2026-09-04. Una serie antigua reexportada saldrá con otros colores; para
  reproducirla, `algoritmo="hsv"`.
- Un color mal formado es error (antes caía a la rampa por defecto sin decirlo). Se admite
  `"#RRGGBB"`.
- `get_unique_values` corta a `max_valores` (1000) y lo dice con `truncado`;
  `get_layer_features` exige `limite` entre 1 y 5000.
- Las cinco ambientales no pisan una salida existente salvo `sobrescribir=true`, y lo
  comprueban **antes** de gastar el geoproceso.

### Añadido
- `ping` dice `estado: "ocupado_tras_timeout"` cuando un comando venció su espera y sigue
  corriendo en ArcMap, en vez de informar «libre» y encolarse detrás.
- El log rota a los 5 MB. «Acerca de» deriva el número de herramientas del código (57).
- `tests/test_runner_py27.py`: el runner y el auditor bajo el Python 2.7 de ArcGIS, sin
  importar arcpy. La suite pasa de 15 a 41 tests.

### Documentación
- Fuera las 48 herramientas (son 57), el parámetro `paginas` de `export_ddp` que nunca
  existió, los modos ADD/REMOVE de `select_by_attribute`, la «IP del otro equipo» como
  acceso remoto y la nota de que no había bookmarks. Timeouts documentados como son.

## [2.11.0] - 2026-09-11 (SIN PUBLICAR)

Instalada y verificada contra ArcMap vivo el mismo dia: 40/40 en
`tests/regresion_sesion_viva.py` —con el caso negativo (`modo unico` sin `valores`)
fallando como debe— y el `.lyr` de grupo comprobado a ojo, con sus dos subgrupos,
nombres, colores y transparencias en una sola llamada.

### Corregido
- **La version del `.csproj` iba por detras de la del `config.xml`** (2.10.1 vs 2.11.0), y
  como `ping` reporta la del ensamblado, el puente decia 2.10.1 con el DLL nuevo cargado.
  Es la trampa de siempre: la version que responde no prueba que build corre. Ahora van
  juntas; hasta reinstalar el paquete, la prueba buena es de comportamiento (si
  `add_layer` devuelve `es_grupo`, es el nuevo).

### Anadido
- **`apply_symbology_from_layer` acepta capas RASTER.** Hasta ahora las rechazaba de plano
  ("Solo capas de entidades admiten simbologia desde .lyr"), y eso dejaba sin camino a los
  renderers que ninguna tool sabe construir: valores unicos, colormap, RGB compuesto.
  `set_raster_symbology` solo clasifica o estira, y sobre un raster **categorico** —una mascara
  0/1/2, una reclasificacion, un `paletted` traido de QGIS— la clasificacion falla con `E_FAIL`
  porque no hay histograma que cortar. Caso que lo destapo: las seis cuencas visuales de un
  estudio paisajistico (2026-09-11), que hubo que aplicar rodeando el add-in por
  `run_geoprocessing("management.ApplySymbologyFromLayer")`.
  - El renderer se **clona y se reencaja** en el raster de destino (`Raster` + `Update()`): un
    `IRasterRenderer` lleva dentro el raster sobre el que se construyo, y sin eso el clon
    pintaria contra el dato del `.lyr`.
  - **La transparencia del `.lyr` viaja tambien** (en entidades se sigue copiando solo el
    renderer). En un raster de visibilidad la opacidad es lo que deja ver la ortofoto debajo.
  - La respuesta dice `tipo`, `renderer` y la `transparencia` aplicada.

- **`set_raster_symbology` gana el modo `unico`** (valores unicos). Es el modo de un raster
  CATEGORICO —mascara de visibilidad, FCC binario, reclasificacion, `paletted` de QGIS—, donde
  el clasificado falla con `E_FAIL` porque no hay histograma que cortar y donde clasificar
  seria mentir sobre el dato.
  - `valores` es obligatorio: no se deducen del raster. Un raster grande no tiene por que
    tener tabla de atributos, y adivinarlos seria inventar la leyenda.
  - `transparentes` pinta esos valores SIN color (NullColor) y sin entrada en la leyenda. Es
    la traduccion exacta del `paletted` de QGIS, donde lo que no esta en la paleta queda
    transparente. **Sin esto el valor de fondo tapa el mapa**, y en una mascara de visibilidad
    el fondo es el 90 % del raster.
  - Sin `colores`, paleta categorica por el circulo cromatico (distinguir, no ordenar), misma
    decision que en `set_unique_values_symbology`.

- **`transparencia` (0-100) en cualquier modo de `set_raster_symbology`.** La opacidad 0,6 de
  QGIS es `transparencia=40`. En un raster tematico la opacidad es lo que deja ver la ortofoto
  debajo, no un adorno.

- **`add_layer` acepta `.lyr`, incluido el de GRUPO**, y un parametro `nombre` para la capa.
  Los dos huecos obligaban a rodear el puente: el nombre, por `MakeRasterLayer` via
  `run_geoprocessing`; el grupo, arrastrando el `.lyr` a mano, porque no hay tool que cree
  grupos. Un `.lyr` de grupo reproduce de una vez el arbol de un proyecto de QGIS con sus
  nombres, colores, transparencias y visibilidades por rama.

- Simbologia raster de las clases: paleta explicita por clase, etiquetas de leyenda y algoritmo
  de interpolacion de rampa (trabajo del 2026-09-04, ver comentarios de
  `RasterSymbologyHandlers.cs`).

### Sin arreglo posible (documentado, no pendiente)
- **La cabecera de la leyenda de un raster queda en `Value`.** ArcObjects la fija en el
  `Update()` del renderer y despues es de solo lectura: `Heading[0] = "..."` devuelve
  `E_INVALIDARG` y por `ILegendInfo.LegendGroup[0].Heading` tampoco. Se quita en el elemento
  de leyenda del layout.

## [2.10.1] - 2026-08-28 (SIN PUBLICAR)

El ArcMap zombi del 2026-08-27, diagnosticado de verdad. **Tampoco se etiqueta.**

### Corregido
- **`WaitForExit()` sin argumentos podia colgar el puente para siempre**
  (`PythonHandlers.RunJob`, camino de exito). Esa llamada no espera a que muera el hijo —ahi
  ya murio— sino al **EOF de los dos pipes**, y un NIETO que heredo los descriptores y sigue
  vivo lo impide indefinidamente; arcpy lanza procesos auxiliares, asi que el nieto no es
  hipotetico. El handler no volvia, el `finally` que suelta el gate no llegaba, y el puente
  quedaba inservible. **Reproducido en aislado** con `cmd` y un nieto por `start /b`: sobre
  .NET Framework 4.8 el hijo murio a los 0,0 s y `WaitForExit()` bloqueo 12,1 s, exactamente
  la vida del nieto. (Sobre .NET 8 NO se reproduce: el add-in es net45, asi que la prueba
  solo vale sobre .NET Framework.) Ahora se acota a 10 s y, si no hay EOF, se sigue avisando
  en el log: perder unas lineas de stderr es barato, colgar el puente no.
- **`Stop()` no liberaba el puerto.** Cerraba el listener pero **no las conexiones ya
  aceptadas**, y una conexion viva mantiene ocupado el 127.0.0.1:27179. El 2026-08-27 eso
  dejo el puerto cogido por un ArcMap que ya habia descargado su extension, y las instancias
  siguientes fallaban con "Solo se permite un uso de cada direccion de socket". Ahora se
  cierran, lo que ademas desbloquea a los clientes que esperaban una respuesta que no iba a
  llegar (los `CloseWait` que se vieron en el puerto eran eso, no una fuga aparte).
- **Al parar el puente se terminan los runners arcpy vivos.** Un runner vivo impide que
  ArcMap acabe de salir, y de ahi nace el zombi: proceso en memoria, sin ventana, con el
  puerto cogido. Se registran por PID y no por objeto `Process`, porque el `Process` muere
  con su `using` y el registro tiene que sobrevivirle.

### Cambiado
- **Docstring de `ping` puesto al dia.** Seguia diciendo que "mientras ArcMap esta ocupado,
  `ping` TAMPOCO puede responder", y eso dejo de ser cierto en 2.9.0: `ping` no pasa por el
  candado y contesta al instante con `comando_en_curso` y `ocupado_desde_s`.

### Nota sobre el diagnostico anterior
Lo que se habia escrito del incidente —"el timeout mata al runner pero no libera la bandera"—
**era falso, y el log del dia lo desmiente**: `ARCMAP_EXEC_TIMEOUT` son 900 s y la extension se
descargo a los 9 min 22 s, o sea que el timeout **nunca llego a actuar**, y no hay ninguna linea
de "supero el timeout". El `busy` de aquellos nueve minutos era el candado funcionando sobre un
runner atascado. El zombi no nacia de una bandera mal liberada, sino de un cierre que no cerraba
ni las conexiones ni el runner.

## [2.10.0] - 2026-08-28 (SIN PUBLICAR)

El puerto deja de estar clavado y la documentacion deja de prometer un bind que no
existe. **Tampoco se etiqueta**, por la misma decision que la 2.9.0.

### Anadido
- **`ARCMAP_BRIDGE_PORT` en el add-in.** El puerto era una constante compilada
  (`Port = 27179`), y eso tenia una consecuencia que no se vio hasta sufrirla: cuando una
  instancia de ArcMap se queda **zombi** —viva y respondiendo, pero sin ventana principal
  que cerrar— sigue sujetando el puerto, y **sin alternativa ningun ArcMap nuevo puede
  levantar el puente**. El sistema entero se quedaba sin via de escape hasta matar el
  proceso por PID. Ahora el add-in lee la variable del entorno del usuario (definirla
  ANTES de abrir ArcMap) con el **mismo nombre que ya usaba el servidor Python**, de modo
  que una sola variable mueve los dos extremos. Se admite 1024-65535; un valor invalido se
  ignora, se avisa en el log y se vuelve al 27179. Cambiar el puerto **no saca nada de
  local**: se sigue escuchando solo en loopback.
- El error de "no se pudo arrancar el servidor" ahora **nombra las dos salidas** (otro
  puerto, o matar el ArcMap zombi) en vez de preguntar si el puerto esta ocupado y dejar
  ahi al usuario.

### Corregido
- **El instalador dice que ruta registra, y avisa si es efimera.** `install.ps1` graba en los
  clientes MCP la ruta desde la que se ejecuta (`$PSScriptRoot`), que es lo correcto desde el
  repo y una trampa desde una copia de usar y tirar: **una prueba secuestra la configuracion
  global de verdad**. El 2026-08-27 se instalo desde el ZIP de `empaquetar.ps1` extraido en el
  Escritorio; al borrar esa carpeta, el servidor MCP quedo apuntando al vacio, arrancaba y moria
  al instante, y el sintoma no aparecio hasta la sesion siguiente como `CONNECTION_CLOSED`,
  lejisimos de su causa. Antes solo se anunciaba "servidor arcmap registrado", sin la ruta.
  Ahora se imprime siempre, tambien en el resumen y en `-SoloVerificar`, y salta un aviso si el
  repo esta bajo Escritorio, Descargas o Temp, o si la carpeta se llama `<algo>-main`/`-master`
  (el nombre que pone GitHub al "Download ZIP"). Probar un ZIP sigue siendo legitimo: lo que hay
  que recordar es **reinstalar desde la ubicacion estable despues**.

### Cambiado
- **El bind se queda en `IPAddress.Loopback` como decision explicita, no como pendiente.**
  Se evaluo abrirlo (el README describia acceso remoto y el add-in no lo permitia) y se
  descarto: `execute_arcpy` es ejecucion de codigo arbitrario sin autenticacion, sin
  usuarios y sin TLS, asi que el loopback es la unica barrera que hay. Y sobre todo es
  **innecesario**: un tunel SSH o Tailscale con reenvio de puerto termina en el
  `127.0.0.1` del destino, o sea que ya alcanza este listener sin abrir nada, y encima
  cifra y autentica. Queda escrito en el codigo para que no se "arregle" mas adelante.
- **Documentacion corregida donde prometia lo que no hacia.** El comentario de
  `src/arcmap_mcp_server.py` decia que para un ArcMap remoto se exportara
  `ARCMAP_BRIDGE_HOST=<IP-del-equipo>`, cosa que **no puede funcionar** porque en esa
  interfaz no escucha nadie. Ahora dice lo que hay: tunel al loopback del destino, y
  `ARCMAP_BRIDGE_HOST` solo si el extremo local del tunel no es 127.0.0.1. Actualizados
  tambien README, `docs/INSTALL.md` y `.env.example`.
- `install.ps1` y los dos scripts de `tests/` dejan de clavar el 27179 y siguen la
  variable, para no dar "conexion rechazada" contra un puente que si esta vivo.

## [2.9.0] - 2026-08-27 (SIN PUBLICAR)

Simbologia de raster, auditoria de carpetas y errores que enseñan. **Nada de esto se
etiqueta en GitHub hasta cerrar el resto de la cola**, por decision explicita.

### Anadido
- **`set_raster_symbology`**: simbologia de raster, clasificada (2-32 clases) o estirada,
  con rampa de color. Era el hueco mas pegado a los flujos reales, donde casi todo el
  producto es raster (NDVI, FCC, P95, pendientes) y NO habia ninguna via: la graduada
  exige capas de entidades y `execute_arcpy` opera sobre una copia, asi que descarta los
  cambios de renderer. Calcula solo las estadisticas de banda si faltan, que es la causa
  numero uno del `E_FAIL` mudo de COM al clasificar.
- **`set_unique_values_symbology`**: simbologia categorica por valores unicos. La paleta
  por defecto reparte tonos por el circulo cromatico (en categorico hace falta DISTINGUIR,
  no ordenar) y es reproducible, para que reexportar una serie de planos no cambie los
  colores. Tope de 100 categorias, NULL omitidos.
- **`describe_mxd`**: version declarada de un `.mxd` **sin abrirlo**, leyendo el stream
  `Version` del compound document. Milisegundos, sin arcpy, sin puente y sin licencia, o
  sea que funciona sobre documentos que ArcMap se niega a abrir. Su veredicto sirve tanto
  para acusar ("se declara mas nuevo que tu ArcMap") como para DESCARTAR ("misma version,
  la causa es otra").
- **`audit_folder`**: inventario de todos los `.mxd` de una carpeta con version, capas,
  fuentes rotas y definition queries. No necesita ArcMap ni el puente. Abre cada documento
  en un **proceso aparte con timeout**, asi que uno colgado no arrastra a los demas ni al
  sistema. Anuncia siempre lo que trunca.
- **Marcadores espaciales**: `get_bookmarks`, `add_bookmark`, `remove_bookmark`,
  `goto_bookmark`. Un nombre repetido reemplaza en vez de duplicar.
- **Suite de tests automaticos** (`tests/test_client_protocol.py`) que corre **sin ArcMap**,
  con un puente falso en un socket local. Cubre defectos que ya ocurrieron: respuesta
  troceada a mitad de caracter multibyte, payload de 400 KB, cierre sin datos, respuesta
  no-JSON, y el invariante de que ninguna tool hable al socket por su cuenta.
- **Guia rapida "tu primer plano en 5 pasos"** en el README.

### Cambiado
- **`ping` deja de pasar por el candado del puente.** Si ArcMap esta ocupado responde igual,
  sin tocar el hilo STA, con `estado`, `comando_en_curso` y `ocupado_desde_s`. Un chequeo de
  salud que solo contesta cuando todo va bien no sirve para nada. El resto de comandos
  siguen dando `busy`, pero ahora dicen QUE comando y cuantos segundos.
- **Puente caido y puente ocupado se nombran con todas las letras** (campo `estado`:
  `puente_caido` / `puente_ocupado`) en las tools que pasan por el cliente. Antes los dos
  estados eran indistinguibles desde fuera y llevaban a relanzar geoprocesos que seguian vivos.
- **`execute_arcpy` avisa cuando el codigo toca simbologia** (`aviso_simbologia`) y redirige
  a la herramienta que si muta la sesion viva. El limite estaba documentado y la herramienta
  no lo mencionaba al devolver un resultado que no habia surtido efecto.
- **El coste de `execute_arcpy` deja de tener una cifra unica.** Lo que domina no es el
  tamano del `.mxd` sino si sus capas apuntan a datos alcanzables: con las fuentes vivas son
  segundos; con una fuente muerta puede no terminar nunca. Los 324,5 s que figuraban como
  coste normal eran ese segundo caso.
- **El README explicaba mal la instancia unica**: culpaba al bloqueo de la DLL cuando lo que
  manda es el puerto, que agarra el primer ArcMap donde se pulse *Iniciar*.
- `tests/test_bridge.py` pasa a llamarse **`tests/sonda_puente.py`**: nunca fue una suite de
  tests sino una sonda manual, y `unittest discover` la ejecutaba al importarla, tumbando la
  suite entera en cualquier maquina sin ArcMap.

### Arreglado
- **Fuga de sockets en el cliente**: al fallar `connect()`, el socket no se cerraba porque el
  `finally` colgaba del otro `try`. Con el puente caido se reintenta en bucle, asi que los
  descriptores se acumulaban. Lo destapo un `ResourceWarning` de la suite nueva, que ahora
  corre con `-W error::ResourceWarning`.
- **Techo de seguridad para los handlers de fondo** (45 min). No compite con sus timeouts
  internos, que suman 40 min en el peor caso legitimo; salta solo si el handler NO VUELVE.
  Sin el, un handler colgado dejaba el puente inservible: ArcMap vivo, sin ventana y sujetando
  el puerto, recuperable solo matandolo por PID.

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
