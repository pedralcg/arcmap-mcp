# Construye ArcmapMcp.AddIn (sin Visual Studio) y empaqueta el .esriaddin.
# Requisitos: dotnet CLI + ArcMap instalado. Por defecto se compila contra 10.5, que es el
# target declarado en Config.xml; para otra version instalada, sin editar el .csproj:
#   .\build.ps1 -ArcGISVersion 10.8
# Estructura del paquete (ZIP plano, el formato que carga el Add-In Manager de 10.x):
#   config.xml + Images\*.png + Install\ArcmapMcp.AddIn.dll + Install\Newtonsoft.Json.dll
#   + Install\arcmap-mcp-disco.exe(.config)
param(
    [string]$ArcGISVersion = '10.5',
    [string]$ArcGISDesktopDir = ''
)

$ErrorActionPreference = 'Stop'

# Las rutas de los ensamblados ESRI salen de estas dos propiedades (ver los .csproj).
$buildArgs = @('-c', 'Release', ('-p:ArcGISVersion=' + $ArcGISVersion))
if ($ArcGISDesktopDir) { $buildArgs += ('-p:ArcGISDesktopDir=' + $ArcGISDesktopDir) }
if (-not $ArcGISDesktopDir) {
    $ArcGISDesktopDir = "C:\Program Files (x86)\ArcGIS\Desktop$ArcGISVersion"
}

$root = $PSScriptRoot
$proj = Join-Path $root 'ArcmapMcp.AddIn'
$out  = Join-Path $proj 'bin\Release'

dotnet build $proj @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'Build fallido' }
# El ejecutable del modo disco (edit_mxd_lote, ADR-010) reutiliza los handlers de la DLL
# y viaja en el mismo paquete, junto a ella: así nunca va desfasado del add-in.
$disco    = Join-Path $root 'ArcmapMcp.Disco'
$outDisco = Join-Path $disco 'bin\Release'
dotnet build $disco @buildArgs
if ($LASTEXITCODE -ne 0) { throw 'Build del modo disco fallido' }

$dist  = Join-Path $root 'dist'
$stage = Join-Path $dist '_stage'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Join-Path $stage 'Install') | Out-Null
New-Item -ItemType Directory -Force (Join-Path $stage 'Images') | Out-Null

Copy-Item (Join-Path $proj 'Config.xml') (Join-Path $stage 'config.xml')
Copy-Item (Join-Path $proj 'Images\*.png') (Join-Path $stage 'Images')
Copy-Item (Join-Path $out 'ArcmapMcp.AddIn.dll') (Join-Path $stage 'Install')
Copy-Item (Join-Path $out 'Newtonsoft.Json.dll') (Join-Path $stage 'Install')
Copy-Item (Join-Path $outDisco 'arcmap-mcp-disco.exe') (Join-Path $stage 'Install')
Copy-Item (Join-Path $outDisco 'arcmap-mcp-disco.exe.config') (Join-Path $stage 'Install')

$addin = Join-Path $dist 'arcmap-mcp.esriaddin'
$zip   = "$addin.zip"
Remove-Item $addin, $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip
Rename-Item $zip $addin
Remove-Item $stage -Recurse -Force

Write-Host "OK -> $addin"
Write-Host ('Instalar: copiar sobre %USERPROFILE%\Documents\ArcGIS\AddIns\Desktop' + $ArcGISVersion + '\{51f4ce63-...}\ con ArcMap CERRADO.')
