# Recording API

A lightweight HTTP server that starts, stops and monitors audio recordings, for live production where a control surface such as [Bitfocus Companion](https://bitfocus.io/companion) triggers recordings with a button press.

It uses [ffmpeg](https://ffmpeg.org/) to encode the audio as MP3. On Windows ffmpeg also reads the audio device; on macOS the server reads it itself, because ffmpeg loses audio doing so there. While recording, it sends live audio levels (dB) to Companion so buttons can show a meter.

It runs on macOS and Windows.

---

## Requirements

- **Python 3.12+**
- **ffmpeg**, installed and on your system `PATH`
  - macOS (Homebrew): `brew install ffmpeg`
  - Windows: download from [ffmpeg.org](https://ffmpeg.org/download.html) or via `winget install ffmpeg`

Install Python dependencies:

```bash
pip install -r requirements.txt
```

---

## Configuration

Copy `config.example.yaml` to `config.yaml` and edit the copy. `config.yaml` is each machine's own and is not in the repository, so updating the code never touches it.

```yaml
framework: auto                  # how the device is read — see note below
inputDevice: "Dante Virtual Soundcard"  # name of the audio device to record from
bitrate: 128k                    # MP3 bitrate
outputPath: ./recordings         # folder where recordings are filed
companionBaseUrls:               # every Bitfocus Companion instance to push status to
  - http://127.0.0.1:8000/
  - http://192.168.1.50:8000/
statusPushRefreshHz: 15          # how often per second to push status to Companion
serverName: mac                  # optional — only when two servers share one Companion
listenHost: 127.0.0.1            # 0.0.0.0 to accept requests from other machines
listenPort: 8080
acceptRequestsFrom:              # optional — the other machines that may use the API
  - 192.168.1.50
audioBufferMs: 200               # optional — see note below
```

A setting the server does not know is an error, so a mistyped name is reported at start-up instead of silently never applying.

`companionBaseUrls` is a list even for one address. Each address is pushed to from its own thread, so a Companion that is off or unreachable does not hold up the others; the failure is logged once, and again when it recovers.

**`framework`** is best left at `auto`, which picks `audioqueue` on macOS, where the server reads the device itself through Apple's Audio Queue Services, and `dshow` on Windows. ffmpeg's own macOS capture, `avfoundation`, can still be selected by name, but it loses audio (about a tenth in a test on a MacBook's microphone) and has no setting to prevent that.

`audioBufferMs` is how much audio the capture may hold before any is lost. On Windows it is ffmpeg's capture buffer: leave it unset unless recordings drop audio, then raise it. On macOS the capture always holds at least 2000; if the log reports `lost … ms of audio`, set it higher.

`acceptRequestsFrom` lists the machines that may use the API, as addresses (`192.168.1.50`) or ranges (`192.168.1.0/24`). The API has no log-in, so on a server other machines can reach, this list is what stops just any of them starting and stopping recordings: other addresses get a `403`, noted in `recording_api.log`. The machine the server runs on is always allowed. Left out, every address is accepted, and a server listening on the network says so in its log at start-up.

`inputDevice`, `framework`, `bitrate`, `audioBufferMs` and `outputPath` are read again for every recording, so a change applies to the next one. Everything else is read once, at start-up, and needs a restart.

To list the device names on your system:
```bash
ffmpeg -f dshow -list_devices true -i ""          # Windows
ffmpeg -f avfoundation -list_devices true -i ""   # macOS
```

A recording with a device name that does not exist is refused, and the error lists the devices found.

---

## Running

```bash
python run_server.py
```

The Windows task and the macOS agent described below run the same thing. The server listens where `listenHost` and `listenPort` say, `http://127.0.0.1:8080` by default. The port is 8080 because Companion's own default is 8000.

Two logs are written to the project folder. `recording_api.log` is the one to read when a recording went wrong: starts and stops, everything ffmpeg said, lost audio, and every failed request. `server.log` has the web server's start-up messages and one line per request.

While working on the code, `uvicorn main:app --reload` restarts the server whenever a file changes. It takes `--host` and `--port` instead of reading them from `config.yaml`.

---

## Running unattended on Windows

For a machine that should record whenever it is powered on, with nobody there. Three things have to line up: the BIOS powers the machine back on, Windows reaches a desktop by itself, and the server starts in that session.

### 1. BIOS — power on after a power loss

On an HP EliteDesk, press `F10` during boot, find **After Power Loss** (under *Advanced → Built-In Device Options* or *Power Management Options*, depending on the model) and set it to **Power On**. The default, *Power Off*, leaves the machine dark after an outage.

### 2. Windows — log on by itself

The server has to run **inside a logged-on desktop session**, not as a background service, for two reasons that fail silently:

- **Audio capture.** A task set to run "at startup" runs in session 0, which has no desktop, and DirectShow capture from Dante Virtual Soundcard is not reliable there.
- **OneDrive** is per-user and does not run in session 0, so recordings in a OneDrive folder would sit unsynced.

So set the machine to log on automatically. Use [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon) rather than `netplwiz`: it stores the password as an encrypted LSA secret instead of plain text in the registry.

```
Autologon.exe <username> <domain-or-machine-name> <password>
```

The machine is then unlocked at the console after every boot, so it relies on the rack or room being physically secure. Also set the power plan to never sleep (`Control Panel → Power Options → High performance`), and make sure Dante Virtual Soundcard starts automatically.

### 3. The server — start it at log on

From the project folder, in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install-task.ps1
```

That registers a Task Scheduler entry, `RecordingAPI`, which runs `run_server.py` with `pythonw.exe` (no console window) 30 seconds after log on, restarts it up to three times if it fails, and has **no execution time limit**. Task Scheduler's default would stop it after three days, mid-week.

Start it once without rebooting, then confirm:

```powershell
Start-ScheduledTask -TaskName RecordingAPI
curl http://127.0.0.1:8080/status
```

If nothing answers, `server.log` in the project folder has the reason; `pythonw.exe` has no console to print it to.

**Test the whole chain before you rely on it.** Cut power at the wall, restore it, and check that `/status` answers without anyone touching the machine.

### Restarting after a code change

```
windows\restart.cmd
```

Double-click it or run it from a prompt. It stops any recording **through the API**, so each MP3 is finalised with an accurate duration header, kills the server as a process tree, starts it again and waits until it answers, so a syntax error in what you changed is reported instead of leaving a server that never came up.

If a room is recording it asks first; `-Force` skips the prompt:

```
windows\restart.cmd -Force
```

Killing the server as a *tree* matters: ffmpeg is a child process, and an orphaned ffmpeg keeps the Dante device open, so the next recording fails to start. Stopping the server from Task Manager leaves one behind.

---

## Running unattended on macOS

The same three things as on Windows: the Mac powers itself back on, logs in by itself, and the server starts in that session.

### 1. Power — come back on, and never sleep

```bash
sudo pmset -a autorestart 1        # power on again when power returns
sudo pmset -a sleep 0 disksleep 0  # never sleep; a sleeping Mac stops recording
```

`autorestart` is the same setting as *System Settings → Energy → Start up automatically after a power failure*. Also turn off automatic installation of macOS updates (*General → Software Update → Automatic Updates*), or the Mac will restart at a moment of its own choosing.

### 2. macOS — log in by itself

*System Settings → Users & Groups → Automatically log in as*. The option is unavailable while **FileVault** is on, so FileVault has to be off. As on Windows, the machine is then unlocked after every boot and relies on being physically secure.

The server has to run in that logged-in session rather than as a system daemon: microphone permission is per user and only honoured in that user's session, and OneDrive only runs there.

### 3. The server — start it at log in

```bash
macos/install-agent.sh
```

That writes a launchd agent to `~/Library/LaunchAgents/local.recording-api.plist` and loads it. The server starts at every log in and is started again if it ever exits. The agent's `PATH` includes ffmpeg's folder, because launchd's default `PATH` leaves Homebrew out and every recording would otherwise fail with ffmpeg not found.

**Start one test recording while sitting at the Mac.** The first time, macOS asks on screen whether to allow microphone access (and possibly access to the project's folder). Until that is answered, recordings fail.

`macos/install-agent.sh --remove` takes the agent out again; `--print` shows the plist without installing it. If the server does not come up, look at `server.log` and `launchd.log` in the project folder.

### Restarting after a code change

```bash
macos/restart.sh           # asks first if a room is recording
macos/restart.sh --force   # restarts without asking
```

Like the Windows script, it stops recordings through the API so each MP3 is finalised, restarts the server, and waits until it answers.

---

## Running two servers for redundancy

Two machines (a Mac and a Windows PC, say) can record the same rooms and report to the same Companion. Three settings make that work:

1. **Give each server a name** in its `config.yaml`:

   ```yaml
   serverName: mac        # "windows" on the other machine
   ```

   Each server then pushes to its own custom variable (`recording_status_mac`, `recording_status_windows`) instead of both overwriting `recording_status`. Create both variables in Companion first: a push to a variable that does not exist is answered with a 404 and logged.

2. **Point both at Companion.** `companionBaseUrls` on each server needs the address of the machine Companion runs on; `127.0.0.1` only works on that machine itself.

3. **Let Companion reach both, and nothing else.** By default the server listens on `127.0.0.1`, which only accepts requests from the same machine. On a server Companion reaches over the network, open it up, name the Companion machine as the one that may use it, and restart the server:

   ```yaml
   listenHost: 0.0.0.0
   acceptRequestsFrom:
     - 192.168.1.50        # the machine Companion runs on
   ```

   The API has no log-in, so without `acceptRequestsFrom` whatever can reach the port can start and stop recordings. The server checks the list itself, so it works the same on macOS and Windows and does not depend on either firewall.

   On Windows the port also has to be let through the firewall. In an elevated PowerShell:

   ```powershell
   New-NetFirewallRule -DisplayName "Recording API" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow
   ```

Redundancy is strongest when **each button press starts both servers** (two HTTP actions on one button) rather than switching to the second server after the first has failed. A fail-over only helps the next recording; the one that was running is cut off at the failure. Both machines need the same Dante channels routed to them.

---

## API Endpoints

The three recording endpoints take a JSON body. `room_name` is required; the other fields have defaults. Channels are numbered from 1, as in Dante Controller.

```json
{
  "room_name": "sanctuary",
  "service_name": "9am",
  "left_input_channel": 1,
  "right_input_channel": 2
}
```

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/recording/start` | Start recording for a room |
| `POST` | `/recording/stop` | Stop recording for a room |
| `POST` | `/recording/toggle` | Start if stopped, stop if running |
| `GET` | `/status` | Return status of all active recordings |

A request for a channel or device that is not there is refused, and the answer says what was found. A request from an address not listed under `acceptRequestsFrom` gets a `403`, whichever endpoint it was for.

### Status response example

```json
{
  "status": "online",
  "sessions": {
    "sanctuary": {
      "service_name": "9am",
      "recording": true,
      "elapsed_s": 142,
      "elapsed_str": "00:02:22",
      "path": "recordings/sanctuary/9am/sanctuary_9am_2026-06-29_09-00-00.mp3",
      "audio_input_level_left": -18.4,
      "audio_input_level_right": -19.1
    }
  }
}
```

---

## Where recordings go

Recordings are filed under a folder per room and, inside it, a folder per service. Folders are created when the recording starts:

```
recordings/                      # outputPath
  LR FOH/                        # room_name
    Sunday Service/              # service_name
      LR FOH_Sunday Service_2026-09-08_09-30-00.mp3
      LR FOH_Sunday Service_2026-09-08_11-00-00.mp3
    Prayer/
      LR FOH_Prayer_2026-09-08_08-25-00.mp3
```

The room and service are in the filename too, so a file still says what it is once copied out of its folder. Characters that cannot appear in a folder name (`/`, `\`, `:`, `<`, `>`, `"`, `|`, `?`, `*`) are replaced with `_`, which is logged; the recording still goes ahead.

---

## Surviving a power cut

An interrupted recording (power lost, process killed) is left as a playable MP3 of everything captured up to roughly the last five seconds. Nothing has to be repaired: MP3 has no index or trailer that a clean stop is required to write.

Two things make that hold. ffmpeg writes each MP3 frame to the file as it is encoded instead of buffering 256KB first, and the file is flushed from the operating system's cache to the disk every five seconds. Without the first, an interrupted recording loses up to sixteen seconds at 128k, and one shorter than that first buffer is left as an empty file.

A recording stopped the normal way, through `/recording/stop` or `/recording/toggle`, is finalised by ffmpeg with an accurate duration header. So is whatever is recording when the server itself is asked to stop, by a restart script or by a Mac shutting down or logging out. Windows gives a background program no such warning at shutdown, so a recording still running then is left as an interrupted one.

---

## Bitfocus Companion Integration

The server continuously pushes its status JSON to the custom variable `recording_status` (`recording_status_<serverName>` with `serverName` set) on every Companion instance in `companionBaseUrls`, at the rate set by `statusPushRefreshHz`. Read the variable in Companion to show recording state, elapsed time or audio levels on your buttons.

What is pushed is the `/status` response plus three fields:

```json
{
  "status": "online",
  "sessions": { },
  "server": "mac",
  "recording_count": 0,
  "pushed_at": 1794519087
}
```

| Field | Meaning |
|-------|---------|
| `server` | This server's `serverName`, or `null` if none is set |
| `recording_count` | How many rooms are recording right now |
| `pushed_at` | When this value was pushed, in seconds since the Unix epoch |

### Showing whether a server is reachable

A custom variable keeps its last value forever, so a server that has lost power still reads `"status": "online"`. What tells a live server from a dead one is the age of `pushed_at`: a running server refreshes it many times a second, so a value more than a few seconds old means it has stopped pushing.

A button text expression along these lines shows one server's state:

```
$(internal:time_unix) - jsonparse($(custom:recording_status_mac))['pushed_at'] > 5
  ? 'MAC\nDOWN'
  : jsonparse($(custom:recording_status_mac))['recording_count'] > 0
    ? 'MAC\nREC'
    : 'MAC\nREADY'
```

Give each variable the default value `{"pushed_at":0,"recording_count":0}` when creating it, so the expression has something to parse, and reads as DOWN, before the first push arrives.

The same conditions work as *feedback* expressions to colour the button, and a second copy with `recording_status_windows` covers the other server. The comparison uses the clocks of two machines, so the server and the Companion machine must both keep their time synchronised (the default on macOS and Windows).

---

## Tests

```bash
python -m unittest
```

The tests need no audio device. Those that make real recordings have ffmpeg record a generated tone, and are skipped where ffmpeg is not installed.

One check uses a real device, and is the one to run on a machine before trusting it with a service. It records for twenty seconds through the same code a button press does, and fails if the recording is shorter than the time it ran for, which is how lost audio shows:

```bash
RECORDING_API_TEST_DEVICE="Dante Virtual Soundcard" python -m unittest tests.test_device -v
```

In Windows PowerShell, set the variable first: `$env:RECORDING_API_TEST_DEVICE = "Dante Virtual Soundcard"`. The top of `tests/test_device.py` lists the other settings, such as which channels to record.
