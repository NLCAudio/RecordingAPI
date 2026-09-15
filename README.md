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
