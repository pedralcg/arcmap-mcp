@echo off
REM Actualizador de arcmap-mcp para quien no usa la terminal: doble clic aqui.
REM Baja los cambios del repo y reinstala las dos piezas (servidor MCP + add-in .NET).
REM Admite los mismos parametros que el instalador:  ACTUALIZAR.bat -Clientes claude-desktop
cd /d "%~dp0"
echo.
echo  arcmap-mcp - actualizador
echo  Cierra ArcMap antes de continuar: el add-in no se puede reemplazar en caliente.
echo.

REM --- Paso 1: bajar los cambios -------------------------------------------
REM El orden importa: sin git instalado, 'git rev-parse' tambien falla, y sin
REM comprobar antes el ejecutable diriamos "no es un repositorio" a quien si lo tiene.
where git >nul 2>&1
if errorlevel 1 goto SinGitInstalado

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 goto SinRepo

echo  == Bajando cambios del repositorio
git pull
if errorlevel 1 (
    echo.
    echo  [!] 'git pull' fallo. Revisa el mensaje de arriba: lo mas comun es tener
    echo      cambios locales sin guardar. Nada se ha reinstalado.
    echo.
    pause
    exit /b 1
)
goto Instalar

:SinGitInstalado
echo  [!] git no esta instalado en este equipo (o no esta en el PATH).
echo.
echo      No hace falta para usar arcmap-mcp: actualiza a mano en tres pasos.
echo        1. Baja el ZIP: https://github.com/pedralcg/arcmap-mcp/archive/refs/heads/main.zip
echo        2. Clic derecho en el ZIP ^> Propiedades ^> Desbloquear, y extraelo
echo           donde quieras (por ejemplo, en Descargas).
echo        3. Doble clic en el INSTALAR.bat de lo extraido (con ArcMap cerrado):
echo           actualiza C:\mcp\arcmap-mcp.
echo.
echo      Si prefieres que esto sea un solo clic en el futuro, instala git desde
echo      https://git-scm.com/download/win y vuelve a clonar el repositorio.
echo.
pause
exit /b 1

:SinRepo
echo  [!] git esta instalado, pero esta carpeta no es un repositorio
echo      (seguramente la extrajiste de un ZIP en vez de clonarla).
echo.
echo      Para actualizar ahora: baja el ZIP nuevo, clic derecho ^> Propiedades ^>
echo      Desbloquear, extraelo donde quieras y ejecuta su INSTALAR.bat
echo.
echo      Para que en adelante sea un solo clic, clona el repositorio:
echo        git clone https://github.com/pedralcg/arcmap-mcp C:\mcp\arcmap-mcp
echo.
pause
exit /b 1

REM --- Paso 2: reinstalar las dos piezas ------------------------------------
:Instalar
echo.
echo  == Reinstalando servidor MCP y add-in
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 (
    echo.
    echo  [!] La instalacion fallo. Si dice que ArcMap esta abierto, cierralo y repite.
    echo.
    pause
    exit /b 1
)

echo.
echo  Listo. Al abrir ArcMap, la barra arcmap-mcp NO aparecera sola (es lo normal
echo  desde la 2.8.2): activala una vez en Customize ^> Toolbars ^> arcmap-mcp.
echo.
pause
