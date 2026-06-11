# Construye ArcmapMcp.AddIn (sin Visual Studio) y empaqueta el .esriaddin.
# Requisitos: dotnet CLI + ArcMap 10.5 instalado (rutas de DLL en el .csproj).
# Estructura del paquete (ZIP plano, el formato que carga el Add-In Manager de 10.x):
#   config.xml + Images\*.png + Install\ArcmapMcp.AddIn.dll + Install\Newtonsoft.Json.dll
$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$proj = Join-Path $root 'ArcmapMcp.AddIn'
$out  = Join-Path $proj 'bin\Release'

dotnet build $proj -c Release
if ($LASTEXITCODE -ne 0) { throw 'Build fallido' }

$dist  = Join-Path $root 'dist'
$stage = Join-Path $dist '_stage'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force (Join-Path $stage 'Install') | Out-Null
New-Item -ItemType Directory -Force (Join-Path $stage 'Images') | Out-Null

Copy-Item (Join-Path $proj 'Config.xml') (Join-Path $stage 'config.xml')
Copy-Item (Join-Path $proj 'Images\*.png') (Join-Path $stage 'Images')
Copy-Item (Join-Path $out 'ArcmapMcp.AddIn.dll') (Join-Path $stage 'Install')
Copy-Item (Join-Path $out 'Newtonsoft.Json.dll') (Join-Path $stage 'Install')

$addin = Join-Path $dist 'arcmap-mcp.esriaddin'
$zip   = "$addin.zip"
Remove-Item $addin, $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip
Rename-Item $zip $addin
Remove-Item $stage -Recurse -Force

Write-Host "OK -> $addin"
Write-Host 'Instalar: copiar sobre %USERPROFILE%\Documents\ArcGIS\AddIns\Desktop10.5\{51f4ce63-...}\ con ArcMap CERRADO.'
