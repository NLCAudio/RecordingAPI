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
outputPath: ./                   # folder where MP3 files are saved
companionBaseUrl: http://127.0.0.1:8001/  # address of your Bitfocus Companion instance
statusPushRefreshHz: 15          # how often per second to push status to Companion
```

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

Output files are named `{room_name}_{service_name}_{timestamp}.mp3` and saved to `outputPath`.

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
      "path": "./sanctuary_9am_2026-06-29_09:00:00.mp3",
      "audio_input_level_left": -18.4,
      "audio_input_level_right": -19.1
    }
  }
}
```

---

## Bitfocus Companion Integration

The server continuously pushes its status JSON to Companion's custom variable `recording_status` at the rate set by `statusPushRefreshHz`. You can read this variable in Companion to show recording state, elapsed time, or audio levels on your button panel.
