@echo off
cd /d "%~dp0"
set "PY=C:\Users\11651\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"
title Live Remote Server
"%PY%" server.py
echo.
echo Server stopped. Press any key to close this window.
pause >nul
