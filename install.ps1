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
$Puerto = 27179

# --------------------------------------------------------------------------- #
# Salida por consola
# --------------------------------------------------------------------------- #

function Write-Paso  { param($m) Write-Host "`n== $m" -ForegroundColor Cyan }
function Write-Ok    { param($m) Write-Host "   [OK] $m" -ForegroundColor Green }
function Write-Aviso { param($m) Write-Host "   [!]  $m" -ForegroundColor Yellow }
function Write-Info  { param($m) Write-Host "        $m" -ForegroundColor DarkGray }

# --------------------------------------------------------------------------- #
# Deteccion del entorno
# --------------------------------------------------------------------------- #

<# ArcMap se busca por registro (la clave la escribe el instalador de Esri) y,
   como respaldo, por la carpeta de add-ins del usuario: en equipos donde el
   registro esta a medias, esa carpeta sigue delatando la version. #>
function Find-ArcMap {
    foreach ($v in @('10.8', '10.7', '10.6', '10.5', '10.4')) {
        $claves = @(
            "HKLM:\SOFTWARE\WOW6432Node\ESRI\Desktop$v",
            "HKLM:\SOFTWARE\ESRI\Desktop$v"
        )
        foreach ($k in $claves) {
            if (Test-Path $k) {
                $dir = (Get-ItemProperty $k -ErrorAction SilentlyContinue).InstallDir
                return [pscustomobject]@{ Version = $v; InstallDir = $dir; Origen = 'registro' }
            }
        }
    }
    foreach ($v in @('10.8', '10.7', '10.6', '10.5', '10.4')) {
        if (Test-Path (Join-Path $env:USERPROFILE "Documents\ArcGIS\AddIns\Desktop$v")) {
            return [pscustomobject]@{ Version = $v; InstallDir = $null; Origen = 'carpeta de add-ins' }
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
    Get-ChildItem 'C:\Python27' -Directory -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object { $p = Join-Path $_.FullName 'python.exe'; if (Test-Path $p) { return $p } }
    return $null
}

<# Version minima del host MCP: el paquete 'mcp' pide Python 3.10+. Un Python
   demasiado viejo (el 3.9 de QGIS 3.28, por ejemplo) se descarta explicitamente
   en vez de fallar luego con un error de pip incomprensible. #>
$PythonMinimo = [Version]'3.10'

function Test-Python3 {
    param([string]$Exe)
    if (-not $Exe -or -not (Test-Path $Exe)) { return $null }
    try {
        $v = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
        $ver = [Version]$v
        if ($ver -lt $PythonMinimo) { return $null }
        return [pscustomobject]@{ Exe = $Exe; Version = $ver }
    }
    catch { return $null }
}

<# Busca un Python 3 utilizable SIN exigir que este en el PATH: mucha gente del
   mundo GIS solo tiene el que vino con QGIS o con ArcGIS Pro, y no sabe donde
   esta. Orden: lanzador py, PATH, instalaciones de python.org, QGIS/OSGeo4W y
   ArcGIS Pro. Se devuelve siempre la RUTA del ejecutable, nunca un alias. #>
function Find-Python3 {
    $candidatos = New-Object System.Collections.Generic.List[string]

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($exe) { $candidatos.Add($exe) }
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
    Write-Info 'Al abrir ArcMap veras la barra "arcmap-mcp" (si no: Customize > Toolbars > arcmap-mcp).'
    Write-Info 'Marca "Autoarranque" una vez y el puente se levantara solo en las siguientes sesiones.'
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

# --------------------------------------------------------------------------- #
# Paso 3 - registro en los clientes IA
# --------------------------------------------------------------------------- #

function Set-Prop {
    param($Obj, [string]$Nombre, $Valor)
    if ($Obj.PSObject.Properties[$Nombre]) { $Obj.$Nombre = $Valor }
    else { $Obj | Add-Member -NotePropertyName $Nombre -NotePropertyValue $Valor }
}

function Read-Json {
    param([string]$Ruta)
    if (-not (Test-Path $Ruta)) { return [pscustomobject]@{} }
    $texto = Get-Content $Ruta -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($texto)) { return [pscustomobject]@{} }
    return $texto | ConvertFrom-Json
}

<# Se escribe sin BOM: algunos clientes leen su config con parsers estrictos que
   se atragantan con el BOM que PowerShell 5.1 pone por defecto. #>
function Write-Json {
    param([string]$Ruta, $Objeto)
    $dir = Split-Path $Ruta -Parent
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    if (Test-Path $Ruta) {
        $backup = "$Ruta.bak"
        Copy-Item $Ruta $backup -Force
        Write-Info "Copia de seguridad: $backup"
    }
    $json = $Objeto | ConvertTo-Json -Depth 64
    [System.IO.File]::WriteAllText($Ruta, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Get-BloqueServidor {
    [pscustomobject]@{
        command = ($VenvPython -replace '\\', '/')
        args    = @(($ServerPy -replace '\\', '/'))
    }
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
    if ($Quitar) {
        & claude mcp remove arcmap --scope user 2>&1 | Out-Null
        Write-Ok 'Claude Code: entrada arcmap retirada'
        return
    }
    & claude mcp remove arcmap --scope user 2>&1 | Out-Null   # idempotencia: re-registra limpio
    & claude mcp add arcmap --scope user -- $VenvPython $ServerPy 2>&1 | Out-Null
    Write-Ok 'Claude Code: servidor arcmap registrado (scope user)'
}

function Register-ClienteJson {
    param(
        [string]$Nombre,
        [string]$Ruta,
        [string]$ClaveRaiz = 'mcpServers',
        [ValidateSet('estandar', 'opencode')] [string]$Formato = 'estandar',
        [switch]$Quitar
    )

    $cfg = Read-Json $Ruta
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
        Write-Info 'Repasa, en este orden: 1) ArcMap abierto; 2) barra arcmap-mcp visible;'
        Write-Info '3) boton "Iniciar MCP" pulsado (o casilla "Autoarranque" marcada);'
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
    if ($ArcMap) { $txtArcMap = $ArcMap.Version + ' (detectado por ' + $ArcMap.Origen + ')' }

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
    Write-Info "Log del add-in:  C:\MCP_Logs\arcmap-mcp.log"
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
        if (Test-Path $destino) { Write-Ok "Add-in instalado en $destino" }
        else { Write-Aviso 'El add-in NO esta instalado' }
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
    Write-Host "`nDesinstalacion completada.`n" -ForegroundColor White
    return
}

if (-not $arcmap) {
    Write-Aviso 'No se detecta ninguna instalacion de ArcMap 10.4-10.8.'
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
Write-Host "`nSiguiente paso: abre ArcMap, pulsa 'Iniciar MCP' (o marca 'Autoarranque')" -ForegroundColor White
Write-Host "y comprueba con:  .\install.ps1 -SoloVerificar`n" -ForegroundColor White
