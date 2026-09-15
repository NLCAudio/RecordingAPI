# Recording API

A lightweight HTTP server that starts, stops, and monitors audio recordings — designed for live production environments where a control surface (like [Bitfocus Companion](https://bitfocus.io/companion)) needs to trigger recordings with a button press.

Under the hood it drives [ffmpeg](https://ffmpeg.org/) to capture audio from any system input device and encode it as an MP3. While recording, it streams live audio level (dB) data back to Companion so buttons can display a real-time meter.

---

## Requirements

- **Python 3.12+**
- **ffmpeg** — must be installed and available on your system `PATH`
  - macOS (Homebrew): `brew install ffmpeg`
  - Windows: download from [ffmpeg.org](https://ffmpeg.org/download.html) or via `winget install ffmpeg`
  - Linux: `sudo apt install ffmpeg` (or your distro's equivalent)

Install Python dependencies:

```bash
pip install -e .
```

---

## Configuration

Edit `config.yaml` before starting the server:

```yaml
framework: avfoundation          # audio capture framework — see note below
inputDevice: "MacBook Air Microphone"  # exact name of the audio device to record from
bitrate: 128k                    # MP3 quality (128k is CD-quality stereo)
outputPath: ./recordings         # folder where recordings are filed
companionBaseUrls:               # every Bitfocus Companion instance to push status to
  - http://127.0.0.1:8001/
  - http://192.168.1.50:8001/
statusPushRefreshHz: 15          # how often per second to push status to Companion
```

`companionBaseUrls` takes a list even when there is only one address. Each
address is pushed to from its own thread, so a Companion machine that is
switched off or unreachable does not hold up the others — the failure is logged
once and again when it recovers.

**`framework` by OS:**

| OS | Value |
|----|-------|
| macOS | `avfoundation` |
| Windows | `dshow` |
| Linux | `alsa` or `pulse` |

To list available device names on your system, run:
```bash
ffmpeg -f <framework> -list_devices true -i ""
```

---

## Running

**Development** (auto-reloads on code changes):
```bash
fastapi dev main.py
```

**Production:**
```bash
fastapi run main.py
```

The server listens on `http://localhost:8000` by default.

---

## Running unattended on Windows

For a machine that should record whenever it is powered on, with nobody there to start anything. Three pieces have to line up: the BIOS powers the machine back on, Windows reaches a desktop by itself, and the server starts in that desktop session.

### 1. BIOS — power on after a power loss

On an HP EliteDesk, press `F10` during boot, then find **After Power Loss** (under *Advanced → Built-In Device Options*, or *Power Management Options* depending on the model) and set it to **Power On**. The default is *Power Off*, which leaves the machine dark after an outage.

### 2. Windows — log on by itself

The server has to run **inside a logged-on desktop session**, not as a background service. Two reasons, both of which bite silently rather than loudly:

- **Audio capture.** A task set to run "at startup" runs in session 0, which has no desktop. DirectShow capture from Dante Virtual Soundcard is not reliable there.
- **OneDrive.** OneDrive is per-user and does not run in session 0 at all, so recordings written to a OneDrive folder would sit there unsynced.

So set the machine to log on automatically. Use [Sysinternals Autologon](https://learn.microsoft.com/sysinternals/downloads/autologon) rather than `netplwiz` — it stores the password as an encrypted LSA secret instead of in plain text in the registry:

```
Autologon.exe <username> <domain-or-machine-name> <password>
```

Be deliberate about this: the machine is then unlocked at the console after every boot, so it relies on the rack or room being physically secure. Set the power plan to never sleep while you are there (`Control Panel → Power Options → High performance`), and make sure Dante Virtual Soundcard is set to start automatically too.

### 3. The server — start it at log on

From the project folder, in PowerShell:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\windows\install-task.ps1
```

That registers a Task Scheduler entry called `RecordingAPI` which runs `run_server.py` with `pythonw.exe` (no console window) 30 seconds after log on, restarts it up to three times if it fails, and — importantly — has **no execution time limit**. Task Scheduler's default is to stop a task after three days, which would take the server down mid-week.

Start it once without rebooting, then confirm:

```powershell
Start-ScheduledTask -TaskName RecordingAPI
curl http://127.0.0.1:8000/status
```

If nothing answers, `server.log` in the project folder has the reason — `run_server.py` sends uvicorn's own output there precisely because `pythonw.exe` has no console to print it to.

**Test the whole chain before you rely on it.** Cut power at the wall, restore it, and check that `/status` answers without anyone touching the machine.

### Restarting after a code change

```
windows\restart.cmd
```

Double-click it, or run it from a prompt. It stops any recording **through the API** so each MP3 is finalised with an accurate duration header, kills the server as a process tree, starts it again, and waits until it answers — so a syntax error in what you just changed is reported rather than leaving you with a server that quietly never came up.

If a room is recording it asks before stopping it. Pass `-Force` to skip the prompt.

```
windows\restart.cmd -Force
```

Killing the server as a *tree* is the part that matters: ffmpeg runs as a child process, and an orphaned ffmpeg keeps the Dante device open, so the next recording fails to start. Stopping the server from Task Manager instead will leave one behind.

---

## API Endpoints

All endpoints accept a JSON body. Every field has a default, so you only need to include what differs.

```json
{
  "room_name": "sanctuary",
  "service_name": "9am",
  "left_input_channel": 0,
  "right_input_channel": 1
}
```

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/recording/start` | Start recording for a room |
| `POST` | `/recording/stop` | Stop recording for a room |
| `POST` | `/recording/toggle` | Start if stopped, stop if running |
| `GET` | `/status` | Return status of all active recordings |

Recordings are filed under a folder per room, and a folder per service inside it. Any folder that does not exist yet is created when the recording starts:

```
recordings/                      # outputPath
  LR FOH/                        # room_name
    Sunday Service/              # service_name
      LR FOH_Sunday Service_2026-09-08_09-30-00.mp3
      LR FOH_Sunday Service_2026-09-08_11-00-00.mp3
    Prayer/
      LR FOH_Prayer_2026-09-08_08-25-00.mp3
```

The room and service stay in the filename as well, so a file still says what it is once it is copied out of its folder. Characters that cannot appear in a folder name (`/`, `\`, `:`, `<`, `>`, `"`, `|`, `?`, `*`) are replaced with `_` and the substitution is logged; the recording still goes ahead.

### Surviving a power cut

A recording that is interrupted — the machine loses power, the process is killed — is left as a playable MP3 of everything captured up to roughly the last five seconds. Nothing has to be repaired afterwards and the file opens in any player, because MP3 has no index or trailer that a clean stop is required to write.

Two things make that hold. ffmpeg is told to write each MP3 frame to the file as it is encoded rather than buffering 256KB of them first, and the file is flushed from the operating system's cache onto the disk every five seconds. Without the first, an interrupted recording loses up to sixteen seconds at 128k — and one cut short before it reaches that first full buffer is left as an empty file.

A recording stopped the normal way, through `/recording/stop` or `/recording/toggle`, is unaffected: ffmpeg still finalises it with an accurate duration header.

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

## Bitfocus Companion Integration

The server continuously pushes its status JSON to the custom variable `recording_status` on every Companion instance in `companionBaseUrls`, at the rate set by `statusPushRefreshHz`. You can read this variable in Companion to show recording state, elapsed time, or audio levels on your button panel.
