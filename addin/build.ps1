# Construye ArcmapMcp.AddIn (sin Visual Studio) y empaqueta el .esriaddin.
# Requisitos: dotnet CLI + ArcMap instalado (rutas de DLL en el .csproj).
# Estructura del paquete (ZIP plano, el formato que carga el Add-In Manager de 10.x):
#   config.xml + Images\*.png + Install\ArcmapMcp.AddIn.dll + Install\Newtonsoft.Json.dll
#   + Install\arcmap-mcp-disco.exe(.config)
#
# Versión de ArcGIS: por defecto 10.5 (el target declarado en Config.xml). Para compilar
# contra otra instalada, sin editar el .csproj:
#   .\build.ps1 -ArcGISVersion 10.8
param(
    [string]$ArcGISVersion = '10.5',
    [string]$ArcGISDesktopDir = ''
)

$ErrorActionPreference = 'Stop'

# Las rutas de los ensamblados ESRI salen de estas dos propiedades (ver el .csproj).
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

# El .exe.config que genera `dotnet build` es un esqueleto de ~174 B: el proyecto no
# tiene App.config, así que MSBuild no tiene de dónde copiar los bindingRedirect de los
# ensamblados ESRI que el modo disco necesita para arrancar. Hasta ahora se colaba el
# fichero que hubiera quedado de una compilación anterior en bin\Release —con lo que en
# la máquina de quien lo hubiera compilado antes "funcionaba"— y en un clon limpio el
# modo disco no arrancaba. Se generan AQUÍ a partir del ArcMap.exe.config de la máquina,
# de modo que la versión que se compila es la que queda declarada en los redirects.
$configDestino = Join-Path $stage 'Install\arcmap-mcp-disco.exe.config'
$arcmapConfig  = Join-Path $ArcGISDesktopDir 'bin\ArcMap.exe.config'
if (Test-Path $arcmapConfig) {
    $origen   = [xml](Get-Content $arcmapConfig -Raw)
    $bloques  = $origen.SelectNodes('//*[local-name()="dependentAssembly"]')
    $lineas   = New-Object System.Collections.Generic.List[string]
    $redirs   = 0
    $lineas.Add('<?xml version="1.0" encoding="utf-8"?>')
    $lineas.Add('<!-- Generado por build.ps1 a partir de ArcMap.exe.config: son los')
    $lineas.Add('     bindingRedirect de ESRI que el exe del modo disco necesita y que')
    $lineas.Add('     `dotnet build` no puede copiar porque el proyecto no tiene App.config. -->')
    $lineas.Add('<configuration>')
    $lineas.Add('  <startup>')
    $lineas.Add('    <supportedRuntime version="v4.0" sku=".NETFramework,Version=v4.5" />')
    $lineas.Add('  </startup>')
    $lineas.Add('  <runtime>')
    $lineas.Add('    <assemblyBinding xmlns="urn:schemas-microsoft-com:asm.v1">')
    foreach ($b in $bloques) {
        $identidad = $b.assemblyIdentity
        $redirect  = $b.bindingRedirect
        if ($null -eq $identidad -or $null -eq $redirect) { continue }
        $lineas.Add('      <dependentAssembly>')
        $lineas.Add(('        <assemblyIdentity name="{0}" publicKeyToken="{1}" culture="{2}" />' -f $identidad.name, $identidad.publicKeyToken, $identidad.culture))
        $lineas.Add(('        <bindingRedirect oldVersion="{0}" newVersion="{1}" />' -f $redirect.oldVersion, $redirect.newVersion))
        $lineas.Add('      </dependentAssembly>')
        $redirs++
    }
    $lineas.Add('    </assemblyBinding>')
    $lineas.Add('  </runtime>')
    $lineas.Add('</configuration>')
    Set-Content -Path $configDestino -Value $lineas -Encoding UTF8
    Write-Host ("config del modo disco: {0} bindingRedirect desde {1}" -f $redirs, 'ArcMap.exe.config')
}
else {
    # Sin ArcMap.exe.config se copia el esqueleto: el add-in funciona, el modo disco no.
    Copy-Item (Join-Path $outDisco 'arcmap-mcp-disco.exe.config') $configDestino
    Write-Warning ("No se encontró $arcmapConfig; el modo disco irá sin bindingRedirect.")
}

$addin = Join-Path $dist 'arcmap-mcp.esriaddin'
$zip   = "$addin.zip"
Remove-Item $addin, $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip
Rename-Item $zip $addin
Remove-Item $stage -Recurse -Force

Write-Host "OK -> $addin"
Write-Host "Instalar: copiar sobre %USERPROFILE%\Documents\ArcGIS\AddIns\Desktop$ArcGISVersion\{51f4ce63-...}\ con ArcMap CERRADO."
