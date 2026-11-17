@echo off
REM Double-clickable wrapper around restart.ps1, so restarting after a code
REM change needs no PowerShell prompt or execution policy change. Arguments
REM are passed through, so "restart.cmd -Force" works like the switch.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1" %*
set EXITCODE=%ERRORLEVEL%

REM Keeps the window open after a double-click, so the result can be read.
echo.
pause
exit /b %EXITCODE%
