@echo off
rem Lanzador de Hefesto para Windows nativo (cmd y PowerShell).
setlocal
set "HEFESTO_ROOT=%~dp0.."
if defined PYTHONPATH (set "PYTHONPATH=%HEFESTO_ROOT%;%PYTHONPATH%") else (set "PYTHONPATH=%HEFESTO_ROOT%")
python -m hefesto %*
exit /b %ERRORLEVEL%
