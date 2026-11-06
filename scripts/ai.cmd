@echo off
rem Windows launcher for the ai command (run from a source checkout)
setlocal
set "NoDefaultCurrentDirectoryInExePath=1"
if defined PYTHONPATH (set "PYTHONPATH=%~dp0..;%PYTHONPATH%") else (set "PYTHONPATH=%~dp0..")
where py >nul 2>nul && (py -3 -P -m omni_ai %*) || (python -P -m omni_ai %*)
exit /b %ERRORLEVEL%
