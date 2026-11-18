@echo off
REM Stops every recording API server running on this machine, whatever started
REM it and whichever port it listens on; see stop_servers.py. Double-clickable.
REM Arguments are passed through: "stop.cmd -Force" does not ask if a room is
REM recording, and "stop.cmd --list" only shows the servers.

setlocal
set "RUN_PYTHON=%~dp0..\.venv\Scripts\python.exe"
if not exist "%RUN_PYTHON%" set "RUN_PYTHON=python.exe"

"%RUN_PYTHON%" "%~dp0..\stop_servers.py" %*
set EXITCODE=%ERRORLEVEL%

REM Keeps the window open after a double-click, so the result can be read.
echo.
pause
exit /b %EXITCODE%
