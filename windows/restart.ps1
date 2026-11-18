<#
.SYNOPSIS
    Restart the recording API, for after a change to the code or to config.yaml.

.DESCRIPTION
    Stops every recording API server running on this machine, not only the
    scheduled task's, starts the server again, and waits until it answers.

    The stopping is done by stop_servers.py. Recordings are stopped through the
    API, not by killing ffmpeg, so each MP3 is finalised with an accurate
    duration header. What is still running after that is killed as a process
    tree: an orphaned ffmpeg holds the Dante device open, and the next
    recording then fails to start.

.PARAMETER TaskName
    The scheduled task registered by install-task.ps1. If no task by that name
    exists, the server is started directly instead.

.PARAMETER BaseUrl
    Where the restarted server is expected to answer. Left out, that is
    listenHost and listenPort from config.yaml.

.PARAMETER Force
    Restart without asking, even if rooms are recording. For unattended use.

.EXAMPLE
    .\windows\restart.ps1

.EXAMPLE
    .\windows\restart.ps1 -Force
#>
[CmdletBinding()]
param(
    [string] $TaskName = 'RecordingAPI',
    [string] $BaseUrl = '',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $ProjectDir 'run_server.py'

# python.exe, not pythonw.exe, which has no output to read.
$python = Join-Path $ProjectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = 'python.exe' }

# Where the restarted server will answer: listenHost and listenPort from
# config.yaml, worked out by the server's own code, unless -BaseUrl was given.
if (-not $BaseUrl) {
    $answer = $null
    Push-Location $ProjectDir
    try {
        $answer = & $python -c 'from config import load_config; print(load_config().local_url)' 2>$null |
            Select-Object -Last 1
    }
    catch {
        $answer = $null
    }
    finally {
        Pop-Location
    }

    if ($answer -and "$answer".Trim() -like 'http*') {
        $BaseUrl = "$answer".Trim()
    }
    else {
        # The address used when config.yaml sets neither value.
        $BaseUrl = 'http://127.0.0.1:8080'
        Write-Warning "Could not read the address from config.yaml; trying $BaseUrl"
    }
}

# --- 1. Stop every server on this machine -------------------------------------

# stop_servers.py asks first if a room is recording, and ends the scheduled
# task.
$env:RECORDING_API_TASK = $TaskName
$stopArguments = @((Join-Path $ProjectDir 'stop_servers.py'))
if ($Force) { $stopArguments += '--force' }
& $python @stopArguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# --- 2. Start it again --------------------------------------------------------

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $task) {
    Write-Host "Starting scheduled task '$TaskName' ..."
    Start-ScheduledTask -TaskName $TaskName
}
else {
    Write-Warning "No scheduled task '$TaskName'; starting the server directly."
    Write-Warning "Run windows\install-task.ps1 to have it start on its own at log on."

    $venvPythonw = Join-Path $ProjectDir '.venv\Scripts\pythonw.exe'
    if (Test-Path -LiteralPath $venvPythonw) {
        $pythonw = $venvPythonw
    }
    else {
        $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if (-not $found) { throw "pythonw.exe not found; cannot start the server." }
        $pythonw = $found.Source
    }
    Start-Process -FilePath $pythonw -ArgumentList ('"{0}"' -f $Launcher) -WorkingDirectory $ProjectDir
}

# --- 3. Wait until it actually answers ----------------------------------------

# Starting the task only means Windows launched it. A syntax error in the
# changed code would still exit at once, so confirm rather than assume.
$deadline = (Get-Date).AddSeconds(30)
$up = $false
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    try {
        Invoke-RestMethod -Uri "$BaseUrl/status" -TimeoutSec 3 | Out-Null
        $up = $true
        break
    }
    catch {
        # Not up yet; keep waiting until the deadline.
    }
}

Write-Host ""
if ($up) {
    Write-Host "Recording API is back up on $BaseUrl" -ForegroundColor Green
    exit 0
}
else {
    Write-Host "The server did not answer within 30 seconds." -ForegroundColor Red
    Write-Host "Look at $(Join-Path $ProjectDir 'server.log') for the reason." -ForegroundColor Red
    exit 1
}
