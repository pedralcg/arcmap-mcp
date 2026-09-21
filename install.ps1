<#
.SYNOPSIS
    Instalador end-to-end de arcmap-mcp: entorno del servidor, add-in dentro de
    ArcMap y registro en los clientes IA.

.DESCRIPTION
    Idempotente: se puede ejecutar tantas veces como haga falta (para actualizar
    el add-in tras un rebuild, por ejemplo). Detecta la version de ArcMap y el
    Python 2.7 de ArcGIS por registro, prepara el venv del servidor MCP, instala
    el add-in en la carpeta que ArcMap lee de verdad y registra el servidor en
    los clientes que le indiques, respetando el resto de su configuracion.

.PARAMETER Clientes
    Clientes IA a configurar: auto, claude-code, claude-desktop, gemini,
    antigravity, opencode, todos, ninguno. Por defecto 'auto': configura los que
    encuentre instalados en este equipo.

.PARAMETER SoloVerificar
    No instala nada: comprueba el estado y hace un ping real al puente.

.PARAMETER Desinstalar
    Retira el add-in, el venv y las entradas 'arcmap' de los clientes indicados.

.PARAMETER InstalarPython
    Si no hay ningun Python 3 utilizable, lo instala con winget sin preguntar.

.EXAMPLE
    .\install.ps1
    Instalacion tipica: venv + add-in + registro en los clientes IA detectados.

.EXAMPLE
    .\install.ps1 -Clientes claude-desktop
    Igual, registrando el servidor solo en Claude Desktop.

.EXAMPLE
    .\install.ps1 -SoloVerificar
    Diagnostico: que hay instalado y si el puente responde.
#>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'claude-code', 'claude-desktop', 'gemini', 'antigravity', 'opencode', 'todos', 'ninguno')]
    [string[]] $Clientes = @('auto'),
    [switch] $SoloVerificar,
    [switch] $Desinstalar,
    [switch] $SinVenv,
    [switch] $SinAddIn,
    [switch] $InstalarPython
)

$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot
$VenvDir = Join-Path $env:LOCALAPPDATA 'arcmap-mcp\venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$ServerPy = Join-Path $Repo 'src\arcmap_mcp_server.py'
# Mismo criterio que el add-in y el servidor: ARCMAP_BRIDGE_PORT manda si esta puesta.
$Puerto = if ($env:ARCMAP_BRIDGE_PORT) { [int]$env:ARCMAP_BRIDGE_PORT } else { 27179 }

# --------------------------------------------------------------------------- #
# Salida por consola
# --------------------------------------------------------------------------- #

function Write-Paso  { param($m) Write-Host "`n== $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "   [OK] $m" -ForegroundColor Green }
function Write-Aviso { param($m) Write-Host "   [!]  $m" -ForegroundColor Yellow }
function Write-Info  { param($m) Write-Host "        $m" -ForegroundColor DarkGray }

# --------------------------------------------------------------------------- #
# Llamadas a ejecutables externos
# --------------------------------------------------------------------------- #

<# Windows PowerShell 5.1 convierte en ErrorRecord cada linea que un ejecutable
   NATIVO escribe en stderr cuando esa salida se redirige (2>&1 o 2>$null), y con
   $ErrorActionPreference='Stop' eso LANZA aunque el programa haya terminado con
   exito. PowerShell 7 no lo hace, asi que el fallo solo aparece en el equipo del
   usuario, que es donde INSTALAR.bat arranca la 5.1.

   Consecuencias reales que esto evitaba mal:
     - `claude mcp remove arcmap` de una entrada que todavia no existe escribe en
       stderr: en la PRIMERA instalacion el script moria ahi, con el add-in ya
       instalado y ningun cliente registrado.
     - Un `py -3` sin Python 3, o un python.exe que escupe un DeprecationWarning,
       abortaban la deteccion en vez de pasar al siguiente candidato.

   Aqui se baja $ErrorActionPreference a 'Continue' SOLO dentro de la funcion (el
   global sigue en 'Stop'), se captura la salida combinada y se devuelve el codigo
   de salida real para que decida quien llama. #>
function Invoke-Nativo {
    param(
        [Parameter(Mandatory = $true)] [string] $Exe,
        [string[]] $Argumentos = @()
    )
    $ErrorActionPreference = 'Continue'   # local a esta funcion, no toca el global
    $salida = ''
    $codigo = -1
    try {
        $salida = (& $Exe @Argumentos 2>&1 | Out-String)
        $codigo = $LASTEXITCODE
    }
    catch {
        # El ejecutable no existe o no se pudo lanzar: no es un fallo del instalador.
        $salida = $_.Exception.Message
        $codigo = -1
    }
    if ($null -eq $codigo) { $codigo = -1 }
    return [pscustomobject]@{ Codigo = $codigo; Salida = $salida }
}

<# De la salida combinada (stdout + stderr) de un ejecutable, la primera linea que
   encaje con el patron. Hace falta porque con 2>&1 los avisos se mezclan con el
   dato que se buscaba. #>
function Select-LineaSalida {
    param([string]$Salida, [string]$Patron)
    foreach ($linea in ($Salida -split "`r?`n")) {
        $t = $linea.Trim()
        if ($t -and $t -match $Patron) { return $t }
    }
    return $null
}

# --------------------------------------------------------------------------- #
# Deteccion del entorno
# --------------------------------------------------------------------------- #

<# Versiones que se BUSCAN y version minima que el add-in puede cargar de verdad.
   El manifiesto declara <Target version="10.5"> y la DLL enlaza los ensamblados
   ESRI 10.5: en 10.4 no carga, por mucho que el instalador la detecte. Se sigue
   buscando la 10.4 para poder DECIRLO, no para instalar encima. #>
$VersionesArcMap = @('10.8', '10.7', '10.6', '10.5', '10.4')
$ArcMapMinimo = [Version]'10.5'

<# ArcMap se busca por registro (la clave la escribe el instalador de Esri) y,
   como respaldo, por la carpeta de add-ins del usuario: en equipos donde el
   registro esta a medias, esa carpeta sigue delatando la version. #>
function Find-ArcMap {
    foreach ($v in $VersionesArcMap) {
        $claves = @(
            "HKLM:\SOFTWARE\WOW6432Node\ESRI\Desktop$v",
            "HKLM:\SOFTWARE\ESRI\Desktop$v"
        )
        foreach ($k in $claves) {
            if (Test-Path $k) {
                $dir = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallDir
                return [pscustomobject]@{
                    Version = $v; InstallDir = $dir; Origen = 'registro'
                    Soportada = ([Version]$v -ge $ArcMapMinimo)
                }
            }
        }
    }
    foreach ($v in $VersionesArcMap) {
        if (Test-Path (Join-Path $env:USERPROFILE "Documents\ArcGIS\AddIns\Desktop$v")) {
            return [pscustomobject]@{
                Version = $v; InstallDir = $null; Origen = 'carpeta de add-ins'
                Soportada = ([Version]$v -ge $ArcMapMinimo)
            }
        }
    }
    return $null
}

function Get-AddInsDir { param($Version) Join-Path $env:USERPROFILE "Documents\ArcGIS\AddIns\Desktop$Version" }

<# El Python 2.7 de ArcGIS solo lo necesitan las herramientas arcpy
   (execute_arcpy, Data Driven Pages y analisis ambiental): su ausencia es aviso,
   no error de instalacion. #>
function Find-Python27 {
    if ($env:ARCMAP_PYTHON27 -and (Test-Path $env:ARCMAP_PYTHON27)) { return $env:ARCMAP_PYTHON27 }
    $k = 'HKLM:\SOFTWARE\WOW6432Node\Python\PythonCore\2.7\InstallPath'
    if (Test-Path $k) {
        $p = Join-Path (Get-ItemProperty $k).'(default)' 'python.exe'
        if (Test-Path $p) { return $p }
    }
    # Con ForEach-Object, `return` sale SOLO del bloque de script del cmdlet, no de
    # la funcion: la busqueda seguia y Find-Python27 devolvia un array cuyo ultimo
    # elemento era el $null del `return` final. Con foreach, `return` sale de la
    # funcion, que es lo que se pretendia.
    $carpetas = Get-ChildItem 'C:\Python27' -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending
    foreach ($carpeta in $carpetas) {
        $p = Join-Path $carpeta.FullName 'python.exe'
        if (Test-Path $p) { return $p }
    }
    return $null
}

<# Version minima del host MCP: el paquete 'mcp' pide Python 3.10+. Un Python
   demasiado viejo (el 3.9 de QGIS 3.28, por ejemplo) se descarta explicitamente
   en vez de fallar luego con un error de pip incomprensible. #>
$PythonMinimo = [Version]'3.10'

function Test-Python3 {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    # Por Invoke-Nativo: un interprete que escribe un aviso en stderr (un
    # DeprecationWarning de sitecustomize, tipico en los Python empaquetados con
    # QGIS) quedaba descartado como "no valido" en 5.1, porque ese aviso lanzaba.
    $r = Invoke-Nativo $Exe @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")
    if ($r.Codigo -ne 0) { return $null }
    $v = Select-LineaSalida $r.Salida '^\d+\.\d+$'
    if (-not $v) { return $null }
    try { $ver = [Version]$v } catch { return $null }
    if ($ver -lt $PythonMinimo) { return $null }
    return [pscustomobject]@{ Exe = $Exe; Version = $ver }
}

<# Busca un Python 3 utilizable SIN exigir que este en el PATH: mucha gente del
   mundo GIS solo tiene el que vino con QGIS o con ArcGIS Pro, y no sabe donde
   esta. Orden: lanzador py, PATH, instalaciones de python.org, QGIS/OSGeo4W y
   ArcGIS Pro. Se devuelve siempre la RUTA del ejecutable, nunca un alias. #>
function Find-Python3 {
    $candidatos = New-Object System.Collections.Generic.List[string]

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        # Un py.exe presente pero SIN ningun Python 3 registrado escribe el error en
        # stderr y sale con codigo != 0: antes eso abortaba la busqueda entera en vez
        # de pasar al siguiente candidato.
        $r = Invoke-Nativo $py.Source @('-3', '-c', 'import sys; print(sys.executable)')
        if ($r.Codigo -eq 0) {
            $exe = Select-LineaSalida $r.Salida '(?i)python(w)?\.exe$'
            if ($exe) { $candidatos.Add($exe) }
        }
    }
    $enPath = Get-Command python -ErrorAction SilentlyContinue
    # El "python" de la Microsoft Store es un stub que abre la tienda: se ignora.
    if ($enPath -and $enPath.Source -notlike '*WindowsApps*') { $candidatos.Add($enPath.Source) }

    $patrones = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        'C:\Python3*\python.exe',
        'C:\Program Files\Python3*\python.exe',
        'C:\Program Files\QGIS *\apps\Python3*\python.exe',
        'C:\OSGeo4W\apps\Python3*\python.exe',
        'C:\OSGeo4W64\apps\Python3*\python.exe',
        'C:\Program Files\ArcGIS\Pro\bin\Python\envs\arcgispro-py3\python.exe'
    )
    foreach ($p in $patrones) {
        Get-ChildItem $p -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            ForEach-Object { $candidatos.Add($_.FullName) }
    }

    foreach ($c in $candidatos) {
        $ok = Test-Python3 $c
        if ($ok) { return $ok }
    }
    return $null
}

<# Ultimo recurso: instalar Python con winget (viene de serie en Windows 10 21H2+
   y en Windows 11). Se pregunta antes; si no hay consola interactiva, se limita a
   explicar el comando. #>
function Install-Python3ConWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $null }

    if (-not $InstalarPython) {
        Write-Aviso 'Se puede instalar Python automaticamente con winget.'
        $respuesta = $null
        try { $respuesta = Read-Host '        ¿Instalar Python 3.12 ahora? [S/N]' } catch { }
        if ($respuesta -notmatch '^[SsYy]') {
            Write-Info 'Instalacion cancelada. Puedes hacerlo tu con:'
            Write-Info '  winget install -e --id Python.Python.3.12'
            return $null
        }
    }

    Write-Info 'Instalando Python 3.12 con winget (puede tardar un par de minutos)...'
    & winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    # winget no refresca el PATH de esta sesion: se busca de nuevo por rutas conocidas.
    return Find-Python3
}

function Test-ArcMapAbierto { [bool](Get-Process ArcMap -ErrorAction SilentlyContinue) }

<# Windows marca todo lo que llega de internet (el ZIP del repo, por ejemplo) y
   ese marcado sobrevive a la extraccion: ArcMap puede negarse a cargar un add-in
   "bloqueado" sin decir por que. Se desbloquea el repo entero al empezar; en una
   copia clonada con git no hay marcas y esto no hace nada. #>
function Unblock-Repo {
    try {
        Get-ChildItem $Repo -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
    }
    catch { }
}

function Get-AddInId {
    $cfg = Join-Path $Repo 'addin\ArcmapMcp.AddIn\Config.xml'
    if (-not (Test-Path $cfg)) { throw "No se encuentra $cfg (¿repo incompleto?)." }
    ([xml](Get-Content $cfg -Raw)).'ESRI.Configuration'.AddInID
}

function Get-AddInVersion {
    $cfg = Join-Path $Repo 'addin\ArcmapMcp.AddIn\Config.xml'
    ([xml](Get-Content $cfg -Raw)).'ESRI.Configuration'.Version
}

# --------------------------------------------------------------------------- #
# Paso 1 - venv del servidor MCP
# --------------------------------------------------------------------------- #

function Install-Venv {
    Write-Paso 'Entorno Python 3 del servidor MCP'
    if (Test-Path $VenvPython) {
        Write-Ok "venv ya presente: $VenvPython"
    }
    else {
        $py = Find-Python3
        if (-not $py) {
            Write-Aviso "No se encuentra ningun Python $PythonMinimo o superior en este equipo."
            Write-Info  'Se ha buscado en el PATH, en las instalaciones de python.org, en QGIS/OSGeo4W'
            Write-Info  'y en ArcGIS Pro. (El Python 2.7 de ArcMap NO sirve para esto: es el del servidor.)'
            $py = Install-Python3ConWinget
        }
        if (-not $py) {
            throw "Hace falta Python $PythonMinimo o superior (64 bits). Instalalo desde " +
                  'https://python.org/downloads/windows marcando "Add python.exe to PATH", y repite.'
        }
        Write-Info "Python encontrado: $($py.Exe)  (version $($py.Version))"
        & $py.Exe -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "Fallo creando el venv con $($py.Exe). Si es el Python de QGIS o de ArcGIS Pro, " +
                  'instala uno de python.org y repite.'
        }
        Write-Ok "venv creado: $VenvDir"
    }
    Write-Info 'Instalando dependencias (mcp)...'
    & $VenvPython -m pip install --quiet --upgrade pip
    & $VenvPython -m pip install --quiet -r (Join-Path $Repo 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Fallo instalando requirements.txt.' }
    Write-Ok 'Dependencias del servidor instaladas'
}

# --------------------------------------------------------------------------- #
# Paso 2 - add-in dentro de ArcMap
# --------------------------------------------------------------------------- #

<# ArcMap carga el add-in desde el FICHERO .esriaddin que hay en la carpeta del
   GUID, y ademas necesita su contenido extraido al lado. Dejar solo una de las
   dos cosas hace que el add-in no cargue en silencio: por eso se ponen ambas. #>
function Install-AddIn {
    param($ArcMap)

    Write-Paso "Add-in .NET dentro de ArcMap $($ArcMap.Version)"

    # Detectar 10.4 e instalar igualmente era mentir dos veces: se anunciaba
    # "[OK] instalado" y luego ArcMap no cargaba nada, sin decir por que. El
    # manifiesto declara Target 10.5 y la DLL enlaza ensamblados 10.5.
    if (-not $ArcMap.Soportada) {
        Write-Aviso ('ArcMap ' + $ArcMap.Version + ' NO esta soportado: el add-in se compila contra 10.5.')
        Write-Info  'El manifiesto declara <Target version="10.5"> y la DLL enlaza ensamblados ESRI 10.5,'
        Write-Info  'asi que en esta version ArcMap no lo cargaria (y no explica por que).'
        Write-Info  'Para usarlo aqui hay que RECOMPILAR el add-in contra tu version:'
        Write-Info  '  1) cambia el <Target> de addin\ArcmapMcp.AddIn\Config.xml,'
        Write-Info  '  2) apunta las referencias del .csproj a tus ensamblados ESRI,'
        Write-Info  '  3) ejecuta addin\build.ps1 y repite este instalador.'
        Write-Info  'El servidor MCP y el registro en los clientes IA si se han preparado.'
        return
    }

    $paquete = Join-Path $Repo 'addin\dist\arcmap-mcp.esriaddin'
    if (-not (Test-Path $paquete)) {
        throw "No existe $paquete. Construyelo con addin\build.ps1 (necesita el dotnet CLI)."
    }
    if (Test-ArcMapAbierto) {
        throw 'ArcMap esta abierto. Cierralo por completo y repite: el add-in no se puede reemplazar en caliente.'
    }

    $destino = Join-Path (Get-AddInsDir $ArcMap.Version) (Get-AddInId)
    if (Test-Path $destino) {
        Remove-Item $destino -Recurse -Force   # instalacion limpia: sin restos de versiones previas
    }
    New-Item -ItemType Directory -Force $destino | Out-Null

    Copy-Item $paquete (Join-Path $destino 'arcmap-mcp.esriaddin') -Force
    $tmpZip = Join-Path $env:TEMP 'arcmap-mcp-install.zip'
    Copy-Item $paquete $tmpZip -Force          # Expand-Archive exige extension .zip
    Expand-Archive $tmpZip -DestinationPath $destino -Force
    Remove-Item $tmpZip -Force
    # Un add-in marcado como "procedente de internet" no carga y ArcMap no lo explica.
    Get-ChildItem $destino -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

    Write-Ok "Add-in $(Get-AddInVersion) instalado en $destino"
    # La barra NO se muestra sola: el add-in declara showInitially="false" desde la
    # 2.8.2, a proposito (con "true" ArcMap recolocaba el resto de barras en cada
    # arranque). Hay que activarla UNA vez y ArcMap recuerda la posicion.
    Write-Info 'Al abrir ArcMap, activa la barra una vez en: Customize > Toolbars > arcmap-mcp.'
    Write-Info 'En esa barra, pon el desplegable "Autoarranque" en SI y el puente se levantara'
    Write-Info 'solo en las siguientes sesiones.'
}

function Uninstall-AddIn {
    param($ArcMap)
    Write-Paso 'Retirando el add-in'
    if (Test-ArcMapAbierto) { throw 'ArcMap esta abierto. Cierralo y repite.' }
    $destino = Join-Path (Get-AddInsDir $ArcMap.Version) (Get-AddInId)
    if (Test-Path $destino) {
        Remove-Item $destino -Recurse -Force
        Write-Ok "Eliminado $destino"
    }
    else { Write-Aviso 'El add-in no estaba instalado' }
}

<# Version del add-in REALMENTE instalado, leida del config.xml que el instalador de
   Esri deja junto al .esriaddin en la carpeta del GUID. No sirve el Config.xml del
   repo: ese dice lo que hay en el codigo fuente, no lo que ArcMap va a cargar, que
   es justo la diferencia que se quiere ver al verificar tras una actualizacion. #>
function Get-AddInVersionInstalada {
    param([string]$Destino)
    foreach ($nombre in @('config.xml', 'Config.xml')) {
        $cfg = Join-Path $Destino $nombre
        if (Test-Path $cfg) {
            try { return ([xml](Get-Content $cfg -Raw)).'ESRI.Configuration'.Version }
            catch { return $null }
        }
    }
    return $null
}

<# Restos que la desinstalacion dejaba atras: la preferencia de autoarranque en el
   registro y el runner extraido en %TEMP%. Lo que NO se borra —logs y copias de
   seguridad de las configuraciones— se LISTA con su ruta, porque borrar el diario
   de lo que paso y la unica copia de la config del usuario no le toca decidirlo a
   un desinstalador. #>
function Remove-RestosLocales {
    Write-Paso 'Restos locales'

    $clave = 'HKCU:\Software\pedralcg\arcmap-mcp'
    if (Test-Path $clave) {
        try {
            Remove-Item $clave -Recurse -Force -ErrorAction Stop
            Write-Ok 'Preferencias del add-in eliminadas (HKCU\Software\pedralcg\arcmap-mcp)'
        }
        catch { Write-Aviso "No se pudo borrar $clave : $($_.Exception.Message)" }
    }
    # La clave padre se queda si tiene mas cosas dentro: no es nuestra.
    $padre = 'HKCU:\Software\pedralcg'
    if ((Test-Path $padre) -and -not (Get-ChildItem $padre -ErrorAction SilentlyContinue)) {
        Remove-Item $padre -Force -ErrorAction SilentlyContinue
    }

    $runner = Join-Path $env:TEMP 'arcmap-mcp'
    if (Test-Path $runner) {
        try {
            Remove-Item $runner -Recurse -Force -ErrorAction Stop
            Write-Ok "Runner y snapshots temporales eliminados: $runner"
        }
        catch { Write-Aviso "No se pudo borrar $runner (¿ArcMap abierto?): $($_.Exception.Message)" }
    }

    $log = 'C:\MCP_Logs\arcmap-mcp.log'
    if (Test-Path $log) {
        Write-Aviso 'El log NO se borra (puede hacer falta para diagnosticar):'
        Write-Info  "  $log"
    }

    $configs = @(
        (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json'),
        (Join-Path $env:USERPROFILE '.gemini\settings.json'),
        (Join-Path $env:USERPROFILE '.gemini\antigravity\mcp_config.json'),
        (Join-Path $env:USERPROFILE '.config\opencode\opencode.json')
    )
    $backups = @()
    foreach ($cfg in $configs) {
        $dir = Split-Path $cfg -Parent
        if (-not (Test-Path $dir)) { continue }
        $hoja = Split-Path $cfg -Leaf
        $backups += Get-ChildItem $dir -Filter "$hoja*.bak" -File -ErrorAction SilentlyContinue
    }
    if ($backups) {
        Write-Aviso 'Las copias de seguridad de las configuraciones NO se borran:'
        foreach ($b in $backups) { Write-Info "  $($b.FullName)" }
    }
}

# --------------------------------------------------------------------------- #
# Paso 3 - registro en los clientes IA
# --------------------------------------------------------------------------- #

function Set-Prop {
    param($Obj, [string]$Nombre, $Valor)
    if ($Obj.PSObject.Properties[$Nombre]) { $Obj.$Nombre = $Valor }
    else { $Obj | Add-Member -NotePropertyName $Nombre -NotePropertyValue $Valor }
}

<# Varios clientes (OpenCode, Antigravity, VS Code y derivados) admiten comentarios
   en su config: JSONC, no JSON. Eso rompe de dos maneras DISTINTAS y las dos son
   malas:
     - PowerShell 5.1: ConvertFrom-Json lanza, y con $ErrorActionPreference='Stop'
       se llevaba por delante la instalacion entera, incluidos los clientes que aun
       no se habian tocado.
     - PowerShell 7: parsea los comentarios... y al reescribir el fichero los BORRA,
       en silencio. Se pierde documentacion del usuario sin que nadie se entere.
   Asi que un fichero con comentarios NO se reescribe: se avisa y se imprime el
   bloque a pegar a mano.

   La deteccion es razonable, no perfecta: recorre el texto llevando la cuenta de si
   esta dentro de una cadena (con escapes) y busca // o /* fuera de ella. Una URL
   "https://..." dentro de una cadena no cuenta, que es el falso positivo que
   importaba. #>
function Test-JsonConComentarios {
    param([string]$Texto)
    $enCadena = $false
    $escape = $false
    for ($i = 0; $i -lt $Texto.Length; $i++) {
        $c = $Texto[$i]
        if ($enCadena) {
            if ($escape) { $escape = $false }
            elseif ($c -eq '\') { $escape = $true }
            elseif ($c -eq '"') { $enCadena = $false }
            continue
        }
        if ($c -eq '"') { $enCadena = $true; continue }
        if ($c -eq '/' -and ($i + 1) -lt $Texto.Length) {
            $sig = $Texto[$i + 1]
            if ($sig -eq '/' -or $sig -eq '*') { return $true }
        }
    }
    return $false
}

function Read-Json {
    param([string]$Ruta)
    if (-not (Test-Path $Ruta)) { return [pscustomobject]@{} }
    $texto = Get-Content $Ruta -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($texto)) { return [pscustomobject]@{} }
    if (Test-JsonConComentarios $texto) {
        throw 'el fichero tiene comentarios (// o /* */): es JSONC, no JSON. Reescribirlo los borraria.'
    }
    return $texto | ConvertFrom-Json
}

<# Se escribe sin BOM: algunos clientes leen su config con parsers estrictos que
   se atragantan con el BOM que PowerShell 5.1 pone por defecto. #>
function Write-Json {
    param([string]$Ruta, $Objeto)
    $dir = Split-Path $Ruta -Parent
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    $json = $Objeto | ConvertTo-Json -Depth 64

    if (Test-Path $Ruta) {
        # Instalador idempotente: si el JSON resultante es identico, no se toca el
        # fichero ni se genera un backup. Antes, cada pasada dejaba un .bak nuevo.
        $actual = Get-Content $Ruta -Raw -Encoding UTF8
        if ($actual -eq $json) {
            Write-Info "Sin cambios: $Ruta"
            return
        }
        # Backup con marca de tiempo. El ".bak" unico se pisaba en cada ejecucion: a
        # la segunda pasada el "backup" ya era el fichero modificado, es decir, no
        # habia copia de la configuracion original.
        $backup = $Ruta + '.' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.bak'
        Copy-Item $Ruta $backup -Force
        Write-Info "Copia de seguridad: $backup"
    }

    # Escritura a temporal EN LA MISMA CARPETA + reemplazo atomico. Escribir directo
    # sobre el destino lo trunca antes de tener el contenido nuevo: si algo falla a
    # mitad, el usuario se queda sin config y sin backup util.
    $tmp = $Ruta + '.tmp' + $PID
    [System.IO.File]::WriteAllText($tmp, $json, (New-Object System.Text.UTF8Encoding($false)))
    try {
        if (Test-Path $Ruta) { [System.IO.File]::Replace($tmp, $Ruta, $null) }
        else { Move-Item -LiteralPath $tmp -Destination $Ruta -Force }
    }
    catch {
        # File.Replace exige que origen y destino esten en el mismo volumen y que el
        # destino exista; si por lo que sea no vale, Move-Item -Force hace el trabajo.
        Move-Item -LiteralPath $tmp -Destination $Ruta -Force
    }
    finally {
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
}

function Get-BloqueServidor {
    [pscustomobject]@{
        command = ($VenvPython -replace '\\', '/')
        args    = @(($ServerPy -replace '\\', '/'))
    }
}

<# El registro del MCP graba la ruta DESDE LA QUE SE EJECUTA este script ($PSScriptRoot).
   Eso es lo correcto cuando se instala desde el repo, y una trampa cuando se instala
   desde una copia de usar y tirar: el ZIP de `empaquetar.ps1` extraido en el Escritorio,
   una descarga de GitHub (que se llama `<repo>-main`), o algo en Temp. Al borrar esa
   carpeta la config queda apuntando al vacio, el servidor arranca y muere al instante, y
   el sintoma no aparece hasta la SIGUIENTE sesion, como `CONNECTION_CLOSED`, lejisimos de
   su causa. Paso el 2026-08-27 en Torre. Se avisa una sola vez. #>
$script:AvisoEfimeroDado = $false
function Test-RepoEfimero {
    if ($script:AvisoEfimeroDado) { return }
    $sospechosas = @(
        [Environment]::GetFolderPath('Desktop'),
        (Join-Path $env:USERPROFILE 'Downloads'),
        $env:TEMP
    ) | Where-Object { $_ }

    $motivo = $null
    foreach ($base in $sospechosas) {
        if ($Repo.TrimEnd('\').ToLower().StartsWith($base.TrimEnd('\').ToLower() + '\')) {
            $motivo = "esta dentro de $base"
            break
        }
    }
    # GitHub nombra asi el ZIP de "Download ZIP": carpeta de prueba casi seguro.
    if (-not $motivo -and (Split-Path $Repo -Leaf) -match '-(main|master)$') {
        $motivo = 'tiene el nombre que pone GitHub al ZIP de descarga'
    }
    if (-not $motivo) { return }

    $script:AvisoEfimeroDado = $true
    Write-Aviso "OJO: estas instalando desde una carpeta que parece temporal ($motivo)."
    Write-Info  "  Repo: $Repo"
    Write-Info  '  Los clientes MCP quedan apuntando A ESA RUTA. Si la borras, el servidor'
    Write-Info  '  dejara de arrancar y el fallo saldra en una sesion posterior, sin pistas.'
    Write-Info  '  Para uso real, mueve el repo a una ubicacion estable (p. ej. C:\mcp\arcmap-mcp)'
    Write-Info  '  y vuelve a ejecutar este instalador desde alli.'
}

<# Claude Code guarda en ~/.claude.json mucho mas que los MCP (historial incluido).
   Reescribir ese fichero con ConvertTo-Json seria arriesgado, asi que se delega
   en su propia CLI, que sabe editarlo sin romper nada. #>
function Register-ClaudeCode {
    param([switch]$Quitar)
    $claude = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claude) {
        Write-Aviso 'No se encuentra la CLI "claude" en el PATH; Claude Code no se toca.'
        Write-Info  'Registralo a mano con:'
        Write-Info  "  claude mcp add arcmap --scope user -- `"$VenvPython`" `"$ServerPy`""
        return
    }
    # Ojo: estas llamadas van por Invoke-Nativo. `claude mcp remove` de una entrada
    # inexistente escribe en stderr, y en PowerShell 5.1 eso LANZA con
    # $ErrorActionPreference='Stop': la primera instalacion moria justo aqui, con el
    # add-in ya puesto y ningun cliente registrado.
    if ($Quitar) {
        [void](Invoke-Nativo $claude.Source @('mcp', 'remove', 'arcmap', '--scope', 'user'))
        Write-Ok 'Claude Code: entrada arcmap retirada'
        return
    }
    [void](Invoke-Nativo $claude.Source @('mcp', 'remove', 'arcmap', '--scope', 'user'))  # idempotencia
    $alta = Invoke-Nativo $claude.Source @('mcp', 'add', 'arcmap', '--scope', 'user', '--', $VenvPython, $ServerPy)
    if ($alta.Codigo -ne 0) {
        # Antes se anunciaba "[OK] registrado" pasara lo que pasara: un alta fallida
        # se daba por buena y el usuario no lo descubria hasta que el cliente no veia
        # el servidor.
        Write-Aviso "Claude Code: la CLI devolvio codigo $($alta.Codigo); el servidor NO ha quedado registrado."
        if ($alta.Salida.Trim()) { Write-Info $alta.Salida.Trim() }
        Write-Info 'Registralo a mano con:'
        Write-Info "  claude mcp add arcmap --scope user -- `"$VenvPython`" `"$ServerPy`""
        return
    }
    # Decir SIEMPRE que ruta ha quedado registrada. Antes solo se anunciaba "registrado",
    # y esa omision costo una sesion: el registro apuntaba a un ZIP de prueba borrado
    # despues, y el fallo no salio hasta la sesion siguiente como CONNECTION_CLOSED.
    Write-Ok 'Claude Code: servidor arcmap registrado (scope user)'
    Write-Info "  -> $ServerPy"
    Test-RepoEfimero
}

<# Cuando un fichero de configuracion no se puede reescribir sin riesgo (JSONC, JSON
   invalido, permisos), se imprime el bloque exacto que hay que pegar. Es la
   diferencia entre "no se pudo" y "no se pudo, haz esto". #>
function Show-BloqueManual {
    param([string]$Nombre, [string]$Ruta, [string]$ClaveRaiz, [string]$Formato)
    $py = $VenvPython -replace '\\', '/'
    $sv = $ServerPy -replace '\\', '/'
    Write-Info "Pega este bloque dentro de `"$ClaveRaiz`" en: $Ruta"
    if ($Formato -eq 'opencode') {
        Write-Info '  "arcmap": {'
        Write-Info '    "type": "local",'
        Write-Info "    `"command`": [`"$py`", `"$sv`"],"
        Write-Info '    "enabled": true'
        Write-Info '  }'
    }
    else {
        Write-Info '  "arcmap": {'
        Write-Info "    `"command`": `"$py`","
        Write-Info "    `"args`": [`"$sv`"]"
        Write-Info '  }'
    }
}

function Register-ClienteJson {
    param(
        [string]$Nombre,
        [string]$Ruta,
        [string]$ClaveRaiz = 'mcpServers',
        [ValidateSet('estandar', 'opencode')] [string]$Formato = 'estandar',
        [switch]$Quitar
    )

    # Un cliente que falla NO debe tumbar a los demas: cada registro va en su propio
    # try. Antes, un opencode.json con comentarios abortaba la instalacion completa.
    try {
        $cfg = Read-Json $Ruta
    }
    catch {
        Write-Aviso "${Nombre}: no se toca su configuracion — $($_.Exception.Message)"
        if (-not $Quitar) { Show-BloqueManual $Nombre $Ruta $ClaveRaiz $Formato }
        else { Write-Info "Quita a mano la entrada `"arcmap`" de `"$ClaveRaiz`" en: $Ruta" }
        return
    }
    if (-not $cfg.PSObject.Properties[$ClaveRaiz]) {
        if ($Quitar) { Write-Aviso "${Nombre}: no habia nada que quitar"; return }
        Set-Prop $cfg $ClaveRaiz ([pscustomobject]@{})
    }
    $raiz = $cfg.$ClaveRaiz

    if ($Quitar) {
        if ($raiz.PSObject.Properties['arcmap']) {
            $raiz.PSObject.Properties.Remove('arcmap')
            Write-Json $Ruta $cfg
            Write-Ok "${Nombre}: entrada arcmap retirada"
        }
        else { Write-Aviso "${Nombre}: no habia entrada arcmap" }
        return
    }

    if ($Formato -eq 'opencode') {
        $bloque = [pscustomobject]@{
            type    = 'local'
            command = @(($VenvPython -replace '\\', '/'), ($ServerPy -replace '\\', '/'))
            enabled = $true
        }
    }
    else {
        $bloque = Get-BloqueServidor
    }
    Set-Prop $raiz 'arcmap' $bloque
    Write-Json $Ruta $cfg
    Write-Ok "${Nombre}: servidor arcmap registrado en $Ruta"
}

<# Detecta que clientes IA hay en el equipo por su fichero de configuracion (o su
   CLI). Es lo que hace 'auto': quien instala no tiene por que saber donde guarda
   cada cliente su configuracion, ni recordar como se llama en este script. #>
function Find-ClientesInstalados {
    $encontrados = @()
    if (Get-Command claude -ErrorAction SilentlyContinue) { $encontrados += 'claude-code' }
    if (Test-Path (Join-Path $env:APPDATA 'Claude')) { $encontrados += 'claude-desktop' }
    if (Test-Path (Join-Path $env:USERPROFILE '.gemini\settings.json')) { $encontrados += 'gemini' }
    if (Test-Path (Join-Path $env:USERPROFILE '.gemini\antigravity')) { $encontrados += 'antigravity' }
    if (Test-Path (Join-Path $env:USERPROFILE '.config\opencode')) { $encontrados += 'opencode' }
    return $encontrados
}

function Register-Clientes {
    param([string[]]$Lista, [switch]$Quitar)

    if ($Lista -contains 'ninguno') { Write-Aviso 'Registro en clientes omitido'; return }
    if ($Lista -contains 'todos') {
        $Lista = @('claude-code', 'claude-desktop', 'gemini', 'antigravity', 'opencode')
    }
    elseif ($Lista -contains 'auto') {
        $Lista = Find-ClientesInstalados
        if (-not $Lista) {
            Write-Paso 'Registro del servidor en los clientes IA'
            Write-Aviso 'No se ha detectado ningun cliente IA compatible en este equipo.'
            Write-Info  'Instala tu cliente (Claude Desktop, Claude Code, Gemini CLI, Antigravity u OpenCode)'
            Write-Info  'y repite el instalador, o indicalo a mano:  .\install.ps1 -Clientes claude-desktop'
            return
        }
        Write-Info "Clientes detectados: $($Lista -join ', ')"
    }

    Write-Paso 'Registro del servidor en los clientes IA'
    foreach ($c in $Lista) {
        # Red de seguridad por cliente: cualquier imprevisto (permisos, fichero
        # corrupto, CLI que no responde) se queda en ese cliente y los demas siguen.
        try {
            switch ($c) {
                'claude-code' { Register-ClaudeCode -Quitar:$Quitar }
                'claude-desktop' {
                    Register-ClienteJson -Nombre 'Claude Desktop' -Quitar:$Quitar `
                        -Ruta (Join-Path $env:APPDATA 'Claude\claude_desktop_config.json')
                }
                'gemini' {
                    Register-ClienteJson -Nombre 'Gemini CLI' -Quitar:$Quitar `
                        -Ruta (Join-Path $env:USERPROFILE '.gemini\settings.json')
                }
                'antigravity' {
                    Register-ClienteJson -Nombre 'Antigravity' -Quitar:$Quitar `
                        -Ruta (Join-Path $env:USERPROFILE '.gemini\antigravity\mcp_config.json')
                }
                'opencode' {
                    Register-ClienteJson -Nombre 'OpenCode' -Quitar:$Quitar -ClaveRaiz 'mcp' -Formato 'opencode' `
                        -Ruta (Join-Path $env:USERPROFILE '.config\opencode\opencode.json')
                }
            }
        }
        catch {
            Write-Aviso "${c}: no se ha podido registrar — $($_.Exception.Message)"
            Write-Info  'Los demas clientes continuan.'
        }
    }
    if (-not $Quitar) {
        Write-Info 'Reinicia por completo el cliente: el registro MCP se fija al arrancar.'
    }
}

# --------------------------------------------------------------------------- #
# Paso 4 - verificacion
# --------------------------------------------------------------------------- #

<# Ping directo al add-in por TCP, sin pasar por el servidor MCP ni por el
   cliente: si esto responde, lo demas es cuestion de configuracion. #>
function Test-Puente {
    Write-Paso "Verificacion: ping al puente (127.0.0.1:$Puerto)"
    try {
        $cliente = New-Object System.Net.Sockets.TcpClient
        $cliente.Connect('127.0.0.1', $Puerto)
        $stream = $cliente.GetStream()
        $stream.ReadTimeout = 10000
        $peticion = [System.Text.Encoding]::UTF8.GetBytes('{"type":"ping","params":{}}')
        $stream.Write($peticion, 0, $peticion.Length)

        $buffer = New-Object byte[] 8192
        $sb = New-Object System.Text.StringBuilder
        while ($true) {
            $n = $stream.Read($buffer, 0, $buffer.Length)
            if ($n -le 0) { break }
            [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buffer, 0, $n))
        }
        $cliente.Close()

        $r = $sb.ToString() | ConvertFrom-Json
        if ($r.ok) {
            Write-Ok "Puente activo: $($r.result.addin)"
            Write-Info "Documento: $($r.result.document) | mapa: $($r.result.focus_map) | capas: $($r.result.layer_count)"
            return $true
        }
        Write-Aviso "El puente respondio con error: $($r.error)"
        return $false
    }
    catch {
        Write-Aviso 'El puente no responde.'
        Write-Info 'Repasa, en este orden: 1) ArcMap abierto; 2) barra arcmap-mcp activada'
        Write-Info '   (Customize > Toolbars > arcmap-mcp: no aparece sola);'
        Write-Info '3) boton "Iniciar MCP" pulsado (o desplegable "Autoarranque" en SI);'
        Write-Info "4) el log: C:\MCP_Logs\arcmap-mcp.log"
        return $false
    }
}

<# Ojo al escribir aqui: Windows PowerShell 5.1 (el que lanza INSTALAR.bat) NO
   admite comillas dobles anidadas dentro de $( ) en una cadena - PowerShell 7 si,
   y el script entero deja de parsear con un error que apunta al final del fichero.
   Por eso los textos condicionales se calculan ANTES, en variables. #>
function Show-Resumen {
    param($ArcMap, $Py27)

    $txtArcMap = 'NO detectado'
    if ($ArcMap) {
        $txtArcMap = $ArcMap.Version + ' (detectado por ' + $ArcMap.Origen + ')'
        if (-not $ArcMap.Soportada) { $txtArcMap = $txtArcMap + '  [NO SOPORTADO: el add-in se compila contra 10.5]' }
    }

    $txtVenv = $VenvPython
    if (-not (Test-Path $VenvPython)) { $txtVenv = $VenvPython + '  [ausente]' }

    $txtPy27 = 'NO detectado (execute_arcpy y DDP no funcionaran)'
    if ($Py27) { $txtPy27 = $Py27 }

    Write-Paso 'Resumen del entorno'
    Write-Info "Repo:            $Repo"
    Write-Info "ArcMap:          $txtArcMap"
    Write-Info "Add-in (fuente): $(Join-Path $Repo 'addin\dist\arcmap-mcp.esriaddin')"
    Write-Info "venv servidor:   $txtVenv"
    if (-not (Test-Path $VenvPython)) {
        $py3 = Find-Python3
        $txtPy3 = "NO encontrado (hace falta $PythonMinimo o superior)"
        if ($py3) { $txtPy3 = $py3.Exe + '  (version ' + $py3.Version + ')' }
        Write-Info "Python 3 base:   $txtPy3"
    }
    Write-Info "Python 2.7:      $txtPy27"
    Write-Info "Servidor MCP:    $ServerPy"
    Write-Info "Log del add-in:  C:\MCP_Logs\arcmap-mcp.log"
    # Tambien en -SoloVerificar: es justo cuando se va a mirar por que algo no arranca.
    Test-RepoEfimero
}

# --------------------------------------------------------------------------- #
# Orquestacion
# --------------------------------------------------------------------------- #

Write-Host "`narcmap-mcp - instalador" -ForegroundColor White
Write-Host "pedralcg.dev`n" -ForegroundColor DarkGray

Unblock-Repo
$arcmap = Find-ArcMap
$py27 = Find-Python27

if ($SoloVerificar) {
    Show-Resumen $arcmap $py27
    if ($arcmap) {
        $destino = Join-Path (Get-AddInsDir $arcmap.Version) (Get-AddInId)
        if (Test-Path $destino) {
            # La version INSTALADA, no la del repo: tras una actualizacion a medias
            # (ArcMap abierto, copia fallida) son distintas, y ese es el dato que se
            # viene a buscar aqui.
            $vInst = Get-AddInVersionInstalada $destino
            $txtInst = 'version desconocida (no se pudo leer su config.xml)'
            if ($vInst) { $txtInst = 'version ' + $vInst }
            Write-Ok "Add-in instalado ($txtInst) en $destino"
            $vRepo = Get-AddInVersion
            if ($vInst -and $vRepo -and $vInst -ne $vRepo) {
                Write-Aviso "El repo trae la ${vRepo}: cierra ArcMap y repite el instalador para actualizarlo."
            }
        }
        else { Write-Aviso 'El add-in NO esta instalado' }
        if (-not $arcmap.Soportada) {
            Write-Aviso "ArcMap $($arcmap.Version) no esta soportado por este add-in (compilado contra 10.5)."
        }
    }
    [void](Test-Puente)
    return
}

if ($Desinstalar) {
    if ($arcmap) { Uninstall-AddIn $arcmap } else { Write-Aviso 'ArcMap no detectado; no se toca el add-in' }
    Register-Clientes -Lista $Clientes -Quitar
    if (Test-Path $VenvDir) {
        try {
            Remove-Item $VenvDir -Recurse -Force -ErrorAction Stop
            Write-Ok "venv eliminado: $VenvDir"
        }
        catch {
            # Tipico: un cliente IA sigue abierto y su proceso mantiene en uso el
            # python.exe del venv. No es un fallo de la desinstalacion.
            Write-Aviso 'No se ha podido borrar el venv: algun cliente IA sigue abierto usandolo.'
            Write-Info  "Cierralos y borra a mano: $VenvDir"
        }
    }
    Remove-RestosLocales
    Write-Host "`nDesinstalacion completada.`n" -ForegroundColor White
    return
}

if (-not $arcmap) {
    Write-Aviso 'No se detecta ninguna instalacion de ArcMap 10.5-10.8.'
    Write-Info  'Se continuara con el servidor y los clientes, pero el add-in no se instalara.'
}
if (-not $py27) {
    Write-Aviso 'No se encuentra el Python 2.7 de ArcGIS.'
    Write-Info  'Las herramientas arcpy (execute_arcpy, Data Driven Pages, analisis ambiental) no funcionaran.'
    Write-Info  'Si esta en otra ruta, definela en la variable de entorno ARCMAP_PYTHON27.'
}

if (-not $SinVenv) { Install-Venv }
if (-not $SinAddIn -and $arcmap) { Install-AddIn $arcmap }
Register-Clientes -Lista $Clientes

Show-Resumen $arcmap $py27
Write-Host "`nSiguiente paso: abre ArcMap, activa la barra en Customize > Toolbars > arcmap-mcp," -ForegroundColor White
Write-Host "pulsa 'Iniciar MCP' (o pon el desplegable 'Autoarranque' en SI)" -ForegroundColor White
Write-Host "y comprueba con:  .\install.ps1 -SoloVerificar`n" -ForegroundColor White
