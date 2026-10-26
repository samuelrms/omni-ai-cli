@echo off
rem Windows launcher for the ai command (run from a source checkout)
setlocal
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
where py >nul 2>nul && (py -3 -m omni_ai %*) || (python -m omni_ai %*)
exit /b %ERRORLEVEL%
