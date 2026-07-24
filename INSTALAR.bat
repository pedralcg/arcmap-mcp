@echo off
REM Instalador de arcmap-mcp para quien no usa la terminal: doble clic aqui.
REM Llama a install.ps1 saltandose la politica de ejecucion de PowerShell, que en
REM un Windows recien instalado impide ejecutar scripts .ps1 descargados.
REM Admite los mismos parametros:  INSTALAR.bat -Clientes claude-desktop
cd /d "%~dp0"
echo.
echo  arcmap-mcp - instalador
echo  Cierra ArcMap antes de continuar.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
