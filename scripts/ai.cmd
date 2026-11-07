@echo off
rem Windows launcher for the ai command (run from a source checkout)
setlocal
set "NoDefaultCurrentDirectoryInExePath=1"
if defined PYTHONPATH (set "PYTHONPATH=%~dp0..;%PYTHONPATH%") else (set "PYTHONPATH=%~dp0..")
rem Choose the Python first and run once: a request that fails must not run again with another Python
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul || goto python
py -3 -P -m omni_ai %*
exit /b %ERRORLEVEL%
:python
python -P -m omni_ai %*
exit /b %ERRORLEVEL%
