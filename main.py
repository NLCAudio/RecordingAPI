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
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from backends import ChannelNotAvailableError, DeviceNotFoundError
from companion import push_status
from config import DEFAULT_LOG_PATH, load_config
from recording import RecordingRequest, SessionRegistry

# Application log. Everything ffmpeg prints on stderr — except the per-second
# dB level lines, which would flood the file — ends up here, along with
# recording start/stop events and every request that failed. This is the place
# to look for capture problems: ffmpeg reports dropped packets and buffer
# overruns as stderr warnings.
logger = logging.getLogger("recording_api")

# Every room that is currently recording. Owns its own locking, so nothing
# here has to coordinate between the request threads and the push thread.
registry = SessionRegistry()


def setup_logging(log_path: Path) -> None:
    # Rotate at 5 MB and keep 3 old files so the log can't grow without bound
    # on a machine that records every week and is rarely looked at.
    handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    # Calling this twice (a reload, or a failed start-up retried) would
    # otherwise write every line to the file once per handler.
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # run_server.py gives the root logger a handler on server.log; without this
    # every line below would be written to both files.
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        config = load_config()
    except Exception:
        # A broken config.yaml means we don't know where the log belongs, and
        # the operator would otherwise see a server that simply never came up.
        # The default path is the one config.py would have used anyway.
        setup_logging(DEFAULT_LOG_PATH)
        logger.exception("startup failed: could not read config.yaml")
        raise

    setup_logging(config.log_path)
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


class LoggedHTTPException(HTTPException):
    """An HTTP error that has already been written to the log.

    log_http_exception() below is the safety net that logs every HTTP error
    response. The failures http_errors() recognises are logged there instead,
    with the room name and a severity the safety net cannot reconstruct — so
    they carry this type to keep it from logging them a second time.
    """


@contextmanager
def http_errors(room_name: str):
    """Translate the failures a recording can have into HTTP responses.

    Every branch logs before raising: an HTTPException on its own only reaches
    the caller, and the caller is a Companion button that shows nothing more
    than a failed press.
    """
    try:
        yield
    except (DeviceNotFoundError, ChannelNotAvailableError) as e:
        # The device or channel the caller asked for isn't there — the request
        # is wrong, or Dante Virtual Soundcard isn't running.
        logger.warning("[%s] 422 %s", room_name, e.message)
        raise LoggedHTTPException(status_code=422, detail=e.message)
    except OSError as e:
        # Most likely ffmpeg is not installed or the output folder is not
        # writable. Either way the caller cannot fix it by retrying, so this is
        # an error rather than a warning: someone has to go and fix the machine.
        logger.error("[%s] 400 %s", room_name, e)
        raise LoggedHTTPException(status_code=400, detail=str(e))


@app.exception_handler(StarletteHTTPException)
async def log_http_exception(request: Request, exc: StarletteHTTPException):
    """Log every HTTP error response, including ones we didn't raise ourselves.

    Covers 404s from a mistyped URL and 405s from the wrong method — the shape
    a misconfigured Companion button takes — which never pass through
    http_errors() and would otherwise leave no trace in this log at all.
    """
    if not isinstance(exc, LoggedHTTPException):
        logger.warning(
            "%s %s -> %s %s",
            request.method,
            request.url.path,
            exc.status_code,
            exc.detail,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def log_validation_error(request: Request, exc: RequestValidationError):
    """Log the 422s FastAPI raises when the request body is malformed."""
    logger.warning(
        "%s %s -> 422 invalid request body: %s",
        request.method,
        request.url.path,
        exc.errors(),
    )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def log_unhandled_exception(request: Request, exc: Exception):
    """Log anything that got all the way out of a handler, with a traceback.

    Without this the traceback goes only to uvicorn's own logger, which writes
    to server.log — so the recording log would show a start that produced no
    recording and no reason why.
    """
    logger.exception(
        "%s %s -> 500 unhandled error", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.post("/recording/start")
def start_recording(request: RecordingRequest):
    # Config is re-read per request so edits to config.yaml take effect
    # without restarting the server.
    with http_errors(request.room_name):
        session = registry.start(request, load_config())

    if session is None:
        return {"message": f"{request.room_name} is already recording"}
    return {"message": f"recording started for {request.room_name}"}


@app.post("/recording/stop")
def stop_recording(request: RecordingRequest):
    # Stopping talks to a process too, so it can fail the same ways a start can.
    with http_errors(request.room_name):
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
    with http_errors(request.room_name):
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
