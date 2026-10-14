@echo off
rem Starts the map mover and opens it in your browser. Close this window to stop it.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% -m mapmover.server --browser %*
if errorlevel 1 pause
