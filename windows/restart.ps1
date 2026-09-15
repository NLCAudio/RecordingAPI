<#
.SYNOPSIS
    Restart the recording API, for after a change to the code.

.DESCRIPTION
    Stops anything that is recording, stops the server, starts it again, and
    waits until it answers.

    Recordings are stopped through the API rather than by killing ffmpeg, so
    each MP3 is finalised with an accurate duration header. Anything still
    running after that is killed as a process tree: ffmpeg is a child of the
    server, and an orphaned ffmpeg holds the Dante device open, which makes the
    next recording fail to start.

.PARAMETER TaskName
    The scheduled task registered by install-task.ps1. If no task by that name
    exists, the server is started directly instead.

.PARAMETER BaseUrl
    Where the server listens. Must match HOST and PORT in run_server.py.

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
    [string] $BaseUrl = 'http://127.0.0.1:8000',
    [switch] $Force
)

$ErrorActionPreference = 'Stop'

$ProjectDir = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $ProjectDir 'run_server.py'

# --- 1. Ask the running server what it is doing -------------------------------

# A server that is not running is not an error here: restarting one that is
# already down is just starting it.
$status = $null
try {
    $status = Invoke-RestMethod -Uri "$BaseUrl/status" -TimeoutSec 5
}
catch {
    Write-Host "No server answering on $BaseUrl (it may already be stopped)."
}

$rooms = @()
if ($null -ne $status -and $null -ne $status.sessions) {
    # Filtered to NoteProperty, and not the shorter ".Properties.Name", because
    # on an idle server "sessions" is {} and reading .Name straight off the
    # empty property collection yields a single $null rather than nothing --
    # which reads as one room recording and asks to stop a room called null.
    $rooms = @($status.sessions.PSObject.Properties |
            Where-Object { $_.MemberType -eq 'NoteProperty' } |
            Select-Object -ExpandProperty Name)
}

# --- 2. Stop recordings properly, before anything is killed -------------------

if ($rooms.Count -gt 0) {
    Write-Warning "Recording right now: $($rooms -join ', ')"
    if (-not $Force) {
        $answer = Read-Host "Stop these recordings and restart? [y/N]"
        if ($answer -notmatch '^[Yy]') {
            Write-Host "Nothing changed; the server is still running." -ForegroundColor Green
            exit 1
        }
    }
    foreach ($room in $rooms) {
        Write-Host "Stopping recording in $room ..."
        $body = @{ room_name = $room } | ConvertTo-Json -Compress
        try {
            $result = Invoke-RestMethod -Method Post -Uri "$BaseUrl/recording/stop" `
                -ContentType 'application/json' -Body $body -TimeoutSec 30
            Write-Host "  saved $($result.file)"
        }
        catch {
            # Worth continuing: the restart is still wanted, and the flush
            # settings mean a killed recording is left playable regardless.
            Write-Warning "  could not stop $room cleanly: $($_.Exception.Message)"
        }
    }
}

# --- 3. Stop the server, and any ffmpeg underneath it -------------------------

$procs = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*run_server.py*' })

foreach ($proc in $procs) {
    Write-Host "Stopping server process $($proc.ProcessId) ..."
    # /T takes the whole tree, so no ffmpeg is left behind holding the device.
    & taskkill.exe /PID $proc.ProcessId /T /F 2>&1 | Out-Null
}
if ($procs.Count -eq 0) {
    Write-Host "No server process was running."
}

# Give the socket a moment to be released, or the new server fails to bind.
Start-Sleep -Seconds 2

# --- 4. Start it again --------------------------------------------------------

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

# --- 5. Wait until it actually answers ----------------------------------------

# Starting the task only means Windows launched it. A syntax error in the code
# just changed would still exit immediately, so confirm rather than assume.
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
