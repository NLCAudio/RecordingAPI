"""
Recording API
=============

This is a small web server that controls audio recording on this machine.
Other software (like Bitfocus Companion running on a control surface) talks
to it by sending HTTP requests — the same kind of requests a web browser
makes. There are three main actions:

  POST /recording/start  — begin recording a room
  POST /recording/stop   — stop recording a room
  POST /recording/toggle — start if stopped, stop if running

The server uses ffmpeg (a free command-line audio/video tool) to do the
actual recording and encodes the result as an MP3 file. While recording it
also reads the live audio level (in dB) so dashboards can display a meter.

A background task runs continuously in a separate thread and pushes the
current status to a Bitfocus Companion "custom variable" so the button panel
always shows what is happening without needing to poll.

Configuration (audio device, output folder, companion URL, etc.) lives in
config.yaml next to this file, so you don't need to touch the code to
change those settings.

This file holds only the HTTP layer. The work behind it lives in:

  config.py     — what config.yaml may contain
  recording.py  — recording sessions and the ffmpeg processes behind them
  companion.py  — the status push loop
  backends/     — how each operating system exposes audio devices to ffmpeg
"""

# TODO: write to a local folder, then push to one drive

import logging
import threading
from contextlib import asynccontextmanager, contextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException

from backends import ChannelNotAvailableError, DeviceNotFoundError
from companion import push_status
from config import Config, load_config
from recording import RecordingRequest, SessionRegistry

# Application log. Everything ffmpeg prints on stderr — except the per-second
# dB level lines, which would flood the file — ends up here, along with
# recording start/stop events. This is the place to look for capture problems:
# ffmpeg reports dropped packets and buffer overruns as stderr warnings.
logger = logging.getLogger("recording_api")

# Every room that is currently recording. Owns its own locking, so nothing
# here has to coordinate between the request threads and the push thread.
registry = SessionRegistry()


def setup_logging(config: Config) -> None:
    # Rotate at 5 MB and keep 3 old files so the log can't grow without bound
    # on a machine that records every week and is rarely looked at.
    handler = RotatingFileHandler(config.log_path, maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    setup_logging(config)
    # When the server starts up, launch the status-push loop in the background.
    # daemon=True means this thread is automatically killed when the main
    # program exits, so we don't need to clean it up manually.
    threading.Thread(
        target=push_status, args=(registry, config), daemon=True
    ).start()
    logger.info(
        "Audio level push started to companion at %s", config.companion_base_url
    )
    yield


app = FastAPI(lifespan=lifespan)


@contextmanager
def http_errors():
    """Translate the failures a recording can have into HTTP responses."""
    try:
        yield
    except (DeviceNotFoundError, ChannelNotAvailableError) as e:
        # The device or channel the caller asked for isn't there — the request
        # is wrong, or Dante Virtual Soundcard isn't running.
        raise HTTPException(status_code=422, detail=e.message)
    except OSError as e:
        # Most likely ffmpeg is not installed or the output folder is not
        # writable. Either way the caller cannot fix it by retrying.
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/recording/start")
def start_recording(request: RecordingRequest):
    # Config is re-read per request so edits to config.yaml take effect
    # without restarting the server.
    with http_errors():
        session = registry.start(request, load_config())

    if session is None:
        return {"message": f"{request.room_name} is already recording"}
    return {"message": f"recording started for {request.room_name}"}


@app.post("/recording/stop")
def stop_recording(request: RecordingRequest):
    session = registry.stop(request.room_name)
    if session is None:
        return {"message": f"{request.room_name} is not recording"}
    return {
        "message": f"recording stopped for {request.room_name}",
        "file": session.path,
    }


@app.post("/recording/toggle")
def toggle_recording(request: RecordingRequest):
    # Deliberately not "ask whether it is recording, then call start or stop":
    # the registry decides and acts under one lock, so two toggles arriving at
    # the same moment cannot both find the room idle and both start.
    with http_errors():
        started, session = registry.toggle(request, load_config())

    if started:
        return {"message": f"recording started for {request.room_name}"}
    return {
        "message": f"recording stopped for {request.room_name}",
        "file": session.path,
    }


@app.get("/status")
def get_status():
    return registry.status()
