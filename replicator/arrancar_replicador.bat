@echo off
title Code Markets Replicator
cd /d "%~dp0"
echo Code Markets Replicator
echo.
echo Abre en el navegador:
echo http://127.0.0.1:5075
echo.
python replicator_app.py
echo.
echo El replicador se ha detenido.
pause
