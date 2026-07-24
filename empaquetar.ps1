<#
.SYNOPSIS
    Genera el ZIP de distribucion, identico al que produce "Code > Download ZIP"
    de GitHub, para probar la instalacion en otro equipo SIN publicar nada.

.DESCRIPTION
    Copia el arbol del repo excluyendo lo que no se publica (.git, legacy/,
    PENDIENTE.local.md, addin/tools/, artefactos de build) y lo comprime bajo una
    carpeta raiz con el nombre del proyecto, como hace GitHub. El resultado se
    lleva al equipo de prueba por Drive, red o USB, se desbloquea y se extrae.

    Sirve para iterar tantas veces como haga falta: cada correccion, un ZIP nuevo.
    Cuando el resultado convenza, se hace el commit y el push de verdad.

.PARAMETER Destino
    Carpeta donde dejar el ZIP. Por defecto, la carpeta 'dist' del repo.

.EXAMPLE
    .\empaquetar.ps1
    Genera dist\arcmap-mcp-<version>.zip, tomando la version de Config.xml.

.EXAMPLE
    .\empaquetar.ps1 -Destino 'D:\Compartido'
    Deja el ZIP en otra carpeta (util si se sincroniza o se comparte en red).
#>
[CmdletBinding()]
param(
    [string] $Destino
)

$ErrorActionPreference = 'Stop'
$Repo = $PSScriptRoot

# Mismo criterio que .gitignore: lo que no va al repo publico tampoco va al ZIP.
$Excluidos = @(
    '.git', '.github', 'legacy', 'PENDIENTE.local.md',
    'bin', 'obj', '__pycache__', '.venv', '.env'
)
$RutasExcluidas = @(
    'addin\tools',
    'addin\dist\_stage',
    'dist'          # los ZIP de ejecuciones anteriores: si no, cada paquete engorda
)

function Test-Excluido {
    param([string]$RutaRelativa)
    foreach ($r in $RutasExcluidas) {
        if ($RutaRelativa -eq $r -or $RutaRelativa.StartsWith($r + '\')) { return $true }
    }
    foreach ($parte in $RutaRelativa.Split('\')) {
        if ($Excluidos -contains $parte) { return $true }
    }
    return ($RutaRelativa -like '*.pyc')
}

<# Salvaguarda: INSTALAR.bat lanza los scripts con "powershell", que en Windows es
   SIEMPRE la 5.1, no la 7. La 5.1 lee los .ps1 sin BOM como cp1252 y acepta las
   comillas tipograficas como delimitador, asi que un simple guion largo puede
   romper el fichero entero. Se comprueba aqui, antes de empaquetar, y no se
   distribuye nada que la 5.1 no sepa leer. #>
function Test-CompatiblePowerShell5 {
    $ps5 = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path $ps5)) {
        Write-Host "AVISO: no se encuentra Windows PowerShell 5.1; no se ha podido verificar." -ForegroundColor Yellow
        return
    }
    foreach ($script in @('install.ps1', 'empaquetar.ps1', 'start-arcmap-mcp.ps1', 'addin\build.ps1')) {
        $ruta = Join-Path $Repo $script
        if (-not (Test-Path $ruta)) { continue }

        $bytes = [System.IO.File]::ReadAllBytes($ruta)
        $tieneBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
        $soloAscii = -not [regex]::IsMatch([System.IO.File]::ReadAllText($ruta), '[^\x00-\x7F]')
        if (-not $tieneBom -and -not $soloAscii) {
            throw "$script tiene caracteres no ASCII y NO lleva BOM: PowerShell 5.1 lo leera mal. Guardalo como UTF-8 con BOM."
        }

        $salida = & $ps5 -NoProfile -Command "`$e = `$null; [void][System.Management.Automation.Language.Parser]::ParseFile('$ruta', [ref]`$null, [ref]`$e); if (`$e) { `$e[0].Message }"
        if ($salida) { throw "$script no parsea con PowerShell 5.1: $salida" }
    }
    Write-Host "Verificado: los scripts parsean con Windows PowerShell 5.1." -ForegroundColor DarkGray
}

Test-CompatiblePowerShell5

$version = ([xml](Get-Content (Join-Path $Repo 'addin\ArcmapMcp.AddIn\Config.xml') -Raw)).'ESRI.Configuration'.Version
$nombre = "arcmap-mcp-$version"

if (-not $Destino) { $Destino = Join-Path $Repo 'dist' }
New-Item -ItemType Directory -Force $Destino | Out-Null

$stage = Join-Path ([System.IO.Path]::GetTempPath()) "arcmap-mcp-pack\$nombre"
Remove-Item (Split-Path $stage -Parent) -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $stage | Out-Null

$copiados = 0
Get-ChildItem $Repo -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($Repo.Length + 1)
    if (Test-Excluido $rel) { return }
    $destinoFichero = Join-Path $stage $rel
    $dir = Split-Path $destinoFichero -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    Copy-Item $_.FullName $destinoFichero
    $script:copiados++
}

$zip = Join-Path $Destino "$nombre.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Split-Path $stage -Parent | Join-Path -ChildPath $nombre) -DestinationPath $zip
Remove-Item (Split-Path $stage -Parent) -Recurse -Force

$mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
Write-Host ""
Write-Host "OK -> $zip  ($copiados ficheros, $mb MB)" -ForegroundColor Green
Write-Host ""
Write-Host "En el equipo de prueba:" -ForegroundColor White
Write-Host "  1. Clic derecho en el ZIP > Propiedades > Desbloquear > Aceptar"
Write-Host "  2. Extraer donde quieras (por ejemplo C:\mcp)"
Write-Host "  3. Cerrar ArcMap y doble clic en INSTALAR.bat"
Write-Host ""
