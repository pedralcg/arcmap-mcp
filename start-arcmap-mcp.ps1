<#
.SYNOPSIS
    Levanta y vigila el tunel del MCP de ArcMap "siempre que sea necesario".

.DESCRIPTION
    El "tunel" es el socket entre el servidor MCP externo (Py3) y el puente que
    corre DENTRO de ArcMap (arcmap_bridge.py, Py2.7). Este script:
      1. Prepara el entorno Py3 (.venv + dependencias) si falta.
      2. Sondea el puente (127.0.0.1:27179) y, si no esta, espera con reintentos
         mostrando las instrucciones para arrancarlo dentro de ArcMap.
      3. Cuando el puente responde, hace un ping y muestra la version de ArcGIS.
      4. Con -Server, arranca ademas el servidor MCP externo (modo standalone /
         pruebas). En uso normal con Claude Code, el servidor lo lanza el cliente
         MCP via .mcp.json, asi que basta con que el puente este vivo.

.PARAMETER Server
    Arranca el servidor MCP externo tras confirmar el puente.

.PARAMETER BridgeHost
    Host del puente (default 127.0.0.1; para otra maquina via Tailscale/VPN: <IP-de-tu-equipo>).

.PARAMETER Port
    Puerto del puente (default 27179).

.PARAMETER TimeoutSeconds
    Segundos maximos esperando al puente (0 = infinito). Default 0.

.EXAMPLE
    .\start-arcmap-mcp.ps1
    Vigila hasta que el puente este vivo y deja el tunel confirmado.

.EXAMPLE
    .\start-arcmap-mcp.ps1 -Server
    Igual, y ademas arranca el servidor MCP externo para pruebas locales.
#>
[CmdletBinding()]
param(
    [switch] $Server,
    [string] $BridgeHost = $env:ARCMAP_BRIDGE_HOST,
    [int]    $Port = 0,
    [int]    $TimeoutSeconds = 0
)

$ErrorActionPreference = "Stop"
$RepoDir = $PSScriptRoot
$SrcDir  = Join-Path $RepoDir "src"   # arcmap_bridge.py + arcmap_mcp_server.py viven en src/
if (-not $BridgeHost) { $BridgeHost = "127.0.0.1" }
if ($Port -le 0) { $Port = if ($env:ARCMAP_BRIDGE_PORT) { [int]$env:ARCMAP_BRIDGE_PORT } else { 27179 } }

$BridgeScript = Join-Path $SrcDir "arcmap_bridge.py"

function Write-Section($txt) { Write-Host "`n=== $txt ===" -ForegroundColor Cyan }

# --- 1. Entorno Py3 -------------------------------------------------------- #
function Resolve-Python {
    # El venv vive FUERA de Drive (local, no sincronizado): son miles de
    # archivos regenerables que romperian la sincronizacion.
    $venvDir = Join-Path $env:LOCALAPPDATA "arcmap-mcp\venv"
    $venvPy = Join-Path $venvDir "Scripts\python.exe"
    if (Test-Path $venvPy) { return $venvPy }
    Write-Section "Preparando entorno Python 3 (venv local en $venvDir)"
    $py = (Get-Command py -ErrorAction SilentlyContinue) ?? (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $py) { throw "No encuentro Python 3 en PATH. Instalalo o ajusta el script." }
    # El flag -3 solo existe en el launcher py.exe, no en python.exe
    if ($py.Name -eq "py.exe") { & $py.Source -3 -m venv $venvDir }
    else { & $py.Source -m venv $venvDir }
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r (Join-Path $RepoDir "requirements.txt")
    Write-Host "Entorno listo." -ForegroundColor Green
    return $venvPy
}

# --- 2. Sondeo del puente (sin dependencias, TcpClient .NET) ---------------- #
function Send-Bridge($cmdType) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect($BridgeHost, $Port)
    } catch {
        $client.Dispose(); return $null
    }
    try {
        $stream = $client.GetStream()
        $payload = "{`"type`":`"$cmdType`",`"params`":{}}"
        $bytes = [Text.Encoding]::UTF8.GetBytes($payload)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $buf = New-Object byte[] 16384
        $client.ReceiveTimeout = 5000
        Start-Sleep -Milliseconds 250
        $n = $stream.Read($buf, 0, $buf.Length)
        return [Text.Encoding]::UTF8.GetString($buf, 0, $n)
    } catch {
        return $null
    } finally {
        $client.Dispose()
    }
}

function Show-BridgeInstructions {
    Write-Host @"

  El puente NO esta activo en ${BridgeHost}:${Port}.
  Para levantarlo (dentro de ArcMap 10.5):
    1. Abre tu .mxd en ArcMap.
    2. Geoprocessing > Python  (la ventana de Python).
    3. Pega esta UNICA linea y Enter:

       execfile(r"$BridgeScript")

    (Debe responder: [arcmap-bridge] escuchando en ...)
"@ -ForegroundColor Yellow
}

# --- 3. Bucle de vigilancia ------------------------------------------------- #
Write-Section "Tunel MCP ArcMap  ->  ${BridgeHost}:${Port}"
$venvPy = Resolve-Python

$deadline = if ($TimeoutSeconds -gt 0) { (Get-Date).AddSeconds($TimeoutSeconds) } else { $null }
$instructionsShown = $false
while ($true) {
    $resp = Send-Bridge "ping"
    if ($resp -and $resp.Contains('"pong"')) {
        Write-Host "`nPuente VIVO. Respuesta ping:" -ForegroundColor Green
        Write-Host "  $resp"
        break
    }
    if (-not $instructionsShown) { Show-BridgeInstructions; $instructionsShown = $true }
    if ($deadline -and (Get-Date) -gt $deadline) {
        throw "Timeout: el puente no aparecio en $TimeoutSeconds s."
    }
    Write-Host "  esperando al puente... (Ctrl+C para cancelar)" -ForegroundColor DarkGray
    Start-Sleep -Seconds 3
}

# --- 4. Arranque opcional del servidor MCP externo -------------------------- #
if ($Server) {
    Write-Section "Arrancando servidor MCP externo (stdio)"
    Write-Host "Para uso con Claude Code, en su lugar registra en .mcp.json:" -ForegroundColor DarkGray
    Write-Host "  command: $venvPy" -ForegroundColor DarkGray
    Write-Host "  args:    [`"$(Join-Path $SrcDir 'arcmap_mcp_server.py')`"]" -ForegroundColor DarkGray
    & $venvPy (Join-Path $SrcDir "arcmap_mcp_server.py")
} else {
    Write-Section "Tunel confirmado"
    Write-Host "Listo. El cliente MCP ya puede usar las herramientas." -ForegroundColor Green
    Write-Host "Registro en .mcp.json:" -ForegroundColor DarkGray
    Write-Host "  command: $venvPy"
    Write-Host "  args:    [`"$(Join-Path $SrcDir 'arcmap_mcp_server.py')`"]"
}
