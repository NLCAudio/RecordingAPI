@echo off
REM Double-clickable wrapper around restart.ps1, so restarting the server after
REM a code change does not need a PowerShell prompt or an execution policy
REM change. Any arguments given here are passed straight through, so
REM "restart.cmd -Force" works the same as the PowerShell switch.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1" %*
set EXITCODE=%ERRORLEVEL%

REM Keeps the window open when this was double-clicked, so the result is
REM readable instead of vanishing with the console.
echo.
pause
exit /b %EXITCODE%
