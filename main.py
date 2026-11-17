"""
Recording API
=============

A small web server that controls audio recording on this machine. Other
software, such as Bitfocus Companion on a control surface, drives it with HTTP
requests:

  POST /recording/start  — begin recording a room
  POST /recording/stop   — stop recording a room
  POST /recording/toggle — start if stopped, stop if running
  GET  /status           — what is recording right now

ffmpeg encodes each recording as an MP3, filed under a folder per room and per
service. The live audio level of each recording is read back for a meter, and
the status is pushed continuously to a Companion custom variable, so button
panels show what is happening without polling.

Settings (audio device, output folder, Companion address, ...) live in
config.yaml next to this file.

This file holds only the HTTP layer. The work behind it lives in:

  config.py     — what config.yaml may contain
  recording.py  — recording sessions and the ffmpeg processes behind them
  companion.py  — the status push loop
  backends/     — how the audio device is read on each operating system
"""

import ipaddress
import logging
import time
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
from companion import start_status_push, variable_name
from config import DEFAULT_LOG_PATH, Config, load_config
from recording import RecordingRequest, SessionRegistry

# The application log: recording starts and stops, every request that failed,
# and everything ffmpeg prints on stderr except the level lines, which would
# flood it. The place to look when a recording went wrong.
logger = logging.getLogger("recording_api")

# Every room that is currently recording. It does its own locking, so the
# request threads and the push threads need no coordination here.
registry = SessionRegistry()

# The config the server started with (None until then). Access is decided by
# its acceptRequestsFrom list, not by whatever config.yaml says when a request
# arrives, so the list cannot change under a running service by accident.
startup_config: Config | None = None

# How often a refused address is logged. A machine retrying many times a second
# would otherwise bury everything else.
REFUSAL_LOG_INTERVAL_S = 60.0

# When each refused address was last logged.
refusals_logged_at: dict[str, float] = {}


def setup_logging(log_path: Path) -> None:
    # Rotate at 5 MB, keeping 3 old files, so the log cannot grow without bound
    # on a machine that is rarely looked at.
    handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    # A second call (a reload, or a retried start-up) would otherwise write
    # every line once per handler.
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # run_server.py gives the root logger a handler on server.log; without this
    # every line would go to both files.
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        config = load_config()
    except Exception:
        # A broken config.yaml does not say where the log belongs, so use the
        # default path; otherwise the server would just never come up, silently.
        setup_logging(DEFAULT_LOG_PATH)
        logger.exception("startup failed: could not read config.yaml")
        raise

    setup_logging(config.log_path)
    global startup_config
    startup_config = config
    if config.accept_requests_from:
        logger.info(
            "Accepting requests from this machine and from %s",
            ", ".join(str(network) for network in config.accept_requests_from),
        )
    elif accepts_other_machines(config.listen_host):
        logger.warning(
            "listenHost %s accepts requests from other machines and "
            "acceptRequestsFrom is empty, so any machine that can reach port %d "
            "can start and stop recordings",
            config.listen_host,
            config.listen_port,
        )
    # One status-push loop per configured Companion address, in the background.
    start_status_push(registry, config)
    logger.info(
        "Audio level push started to companion at %s as custom variable %s",
        ", ".join(config.companion_base_urls),
        variable_name(config.server_name),
    )
    yield
    # Shutting down (the machine is restarting, or someone is logging out):
    # stop what is recording so each MP3 is finalised rather than cut off.
    stopped = registry.stop_all()
    if stopped:
        logger.info("server shutting down; stopped recording in %s", ", ".join(stopped))


app = FastAPI(lifespan=lifespan)


def accepts_other_machines(listen_host: str) -> bool:
    """Whether a server listening on this address can be reached from outside."""
    if listen_host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(listen_host).is_loopback
    except ValueError:
        return True  # some other name; assume it resolves to a real address


@app.middleware("http")
async def refuse_unlisted_addresses(request: Request, call_next):
    """Turn away requests from addresses not listed under acceptRequestsFrom.

    The one place access is decided: for every endpoint, and the same way on
    macOS and Windows, independent of either machine's firewall.
    """
    client = request.client.host if request.client else None
    server = request.scope.get("server")
    if startup_config is None or startup_config.allows(
        client, server[0] if server else None
    ):
        return await call_next(request)

    log_refusal(client, request)
    return JSONResponse(
        status_code=403,
        content={"detail": "This address is not allowed to use the recording API"},
    )


def log_refusal(client: str | None, request: Request) -> None:
    now = time.monotonic()
    address = client or "an unknown address"
    last_logged = refusals_logged_at.get(address)
    if last_logged is not None and now - last_logged < REFUSAL_LOG_INTERVAL_S:
        return

    if len(refusals_logged_at) >= 1000:
        # Only reachable if far more machines can reach the server than should;
        # keeps this from growing for as long as it runs.
        refusals_logged_at.clear()
    refusals_logged_at[address] = now
    logger.warning(
        "refused %s %s from %s, which is not listed under acceptRequestsFrom (further "
        "requests from it are not logged for the next %.0f s)",
        request.method,
        request.url.path,
        address,
        REFUSAL_LOG_INTERVAL_S,
    )


class LoggedHTTPException(HTTPException):
    """An HTTP error that has already been written to the log.

    http_errors() logs the failures it recognises itself, with the room name
    and a severity; this type keeps log_http_exception(), which logs every
    other HTTP error, from logging them a second time.
    """


@contextmanager
def http_errors(room_name: str):
    """Translate the failures a recording can have into HTTP responses.

    Every branch logs before raising: the caller is a Companion button, which
    shows nothing more than a failed press.
    """
    try:
        yield
    except (DeviceNotFoundError, ChannelNotAvailableError) as e:
        # The device or channel asked for is not there: the request is wrong,
        # or Dante Virtual Soundcard is not running.
        logger.warning("[%s] 422 %s", room_name, e.message)
        raise LoggedHTTPException(status_code=422, detail=e.message)
    except OSError as e:
        # Most likely ffmpeg is missing or the output folder is not writable.
        # Retrying will not help; someone has to fix the machine, hence an error.
        logger.error("[%s] 400 %s", room_name, e)
        raise LoggedHTTPException(status_code=400, detail=str(e))


@app.exception_handler(StarletteHTTPException)
async def log_http_exception(request: Request, exc: StarletteHTTPException):
    """Log every HTTP error response, including ones we did not raise.

    Covers the 404s and 405s of a misconfigured Companion button (a mistyped
    URL, the wrong method), which never pass through http_errors() and would
    otherwise leave no trace in this log.
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

    Otherwise the traceback goes only to uvicorn's logger and server.log, and
    this log would show a start that produced no recording and no reason why.
    """
    logger.exception(
        "%s %s -> 500 unhandled error", request.method, request.url.path, exc_info=exc
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


@app.post("/recording/start")
def start_recording(request: RecordingRequest):
    # Config is re-read per request, so edits apply without a restart.
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
    # Not "ask whether it is recording, then start or stop": the registry
    # decides and acts under one lock, so two toggles arriving together cannot
    # both find the room idle and both start.
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
