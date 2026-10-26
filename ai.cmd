@echo off
rem Windows launcher for the ai command
where py >nul 2>nul && (py -3 "%~dp0smart_ai.py" %*) || (python "%~dp0smart_ai.py" %*)
exit /b %ERRORLEVEL%
