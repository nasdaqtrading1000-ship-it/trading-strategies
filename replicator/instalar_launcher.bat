@echo off
setlocal
cd /d "%~dp0"
python launcher.py --install
if errorlevel 1 (
  echo.
  echo No se pudo instalar el launcher.
  pause
  exit /b 1
)
echo.
echo Instalacion completada. Ya puedes usar el boton Abrir Replicator.
pause
