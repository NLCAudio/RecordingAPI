<#
.SYNOPSIS
    Register the scheduled task that starts the recording API on this machine.

.DESCRIPTION
    Creates (or replaces) a Task Scheduler entry that runs run_server.py with
    pythonw.exe every time the given user logs on.

    The trigger is "at log on" rather than "at startup" on purpose. A task set
    to run at startup runs in session 0, without a desktop, and two things this
    server needs do not reliably work there: DirectShow capture from Dante
    Virtual Soundcard, and OneDrive, which is per-user and would leave the
    recordings folder unsynced. Pair this with automatic log on so an
    unattended machine reaches a desktop by itself after a power cut --
    README.md explains how.

.PARAMETER TaskName
    Name to register the task under. Re-running with the same name replaces it.

.PARAMETER UserId
    The account whose log on starts the server. Defaults to the account running
    this script, which is normally the right one.

.PARAMETER DelaySeconds
    How long after log on to wait before starting, giving Dante Virtual
    Soundcard and the network time to come up first.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install-task.ps1
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'RecordingAPI',
    [string] $UserId = "$env:USERDOMAIN\$env:USERNAME",
    [int]    $DelaySeconds = 30
)

$ErrorActionPreference = 'Stop'

# This script lives in <project>\windows, so the project is one level up.
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $ProjectDir 'run_server.py'

if (-not (Test-Path -LiteralPath $Launcher)) {
    throw "run_server.py not found at $Launcher. Run this script from the copy of the project you want to serve."
}

# pythonw.exe rather than python.exe: no console window appears when the task
# starts, which is what run_server.py is written for. The project's own virtual
# environment comes first so the task uses the dependencies installed for it.
$venvPythonw = Join-Path $ProjectDir '.venv\Scripts\pythonw.exe'
if (Test-Path -LiteralPath $venvPythonw) {
    $Pythonw = $venvPythonw
}
else {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $found) {
        throw "pythonw.exe not found. Create the virtual environment first (py -m venv .venv, then .\.venv\Scripts\pip install -e .) or add Python to PATH."
    }
    $Pythonw = $found.Source
    Write-Warning "No .venv found; using $Pythonw. The task will fail unless this Python has the project's dependencies."
}

$action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}"' -f $Launcher) -WorkingDirectory $ProjectDir

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
if ($DelaySeconds -gt 0) {
    # Set as a property because New-ScheduledTaskTrigger has no -Delay for
    # logon triggers. The format is an ISO 8601 duration.
    $trigger.Delay = 'PT{0}S' -f $DelaySeconds
}

# An ExecutionTimeLimit of zero means "no limit". Without it Task Scheduler
# applies its default of three days and stops the server mid-week.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)

# Interactive, and deliberately not RunLevel Highest: the server binds a
# loopback port and reads an audio device, neither of which needs admin.
$principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Starts the recording API (run_server.py) so Companion can trigger recordings.' `
    -Force | Out-Null

Write-Host ""
Write-Host "Registered scheduled task '$TaskName'" -ForegroundColor Green
Write-Host "  runs      : $Pythonw `"$Launcher`""
Write-Host "  working in: $ProjectDir"
Write-Host "  trigger   : at log on of $UserId, after $DelaySeconds seconds"
Write-Host ""
Write-Host "Start it now with:  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "Then check:         curl http://127.0.0.1:8000/status"
Write-Host "If it does not come up, read server.log in the project folder."
Write-Host ""
Write-Host "This only starts the server once someone is logged on. For an" -ForegroundColor Yellow
Write-Host "unattended machine, set up automatic log on as well -- see README.md." -ForegroundColor Yellow
