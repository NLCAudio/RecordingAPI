"""
Server launcher
===============

The way the recording API is started: by hand with `python run_server.py`, by
the Windows scheduled task, and by the macOS launchd agent. It listens where
listenHost and listenPort in config.yaml say, and notes that address in
server.url for the restart scripts.

Running `uvicorn main:app` from a terminal is fine while developing, but a
server started in the background has two problems this file solves:

  1. It starts in whatever folder the launcher happens to use — for Windows
     Task Scheduler that is C:\\Windows\\System32. The relative paths in
     config.yaml (outputPath, logPath) are resolved against the working
     directory, so without the chdir below the recordings would be written
     somewhere nobody would think to look.

  2. Launched with pythonw.exe there is no console, so anything uvicorn
     prints is thrown away — including the reason it failed to start. The
     log config below sends uvicorn's own output to server.log instead.

server.log holds start/stop and HTTP request lines. Recording problems —
ffmpeg warnings, dropped packets — go to recording_api.log, set by logPath
in config.yaml.
"""

import os
from pathlib import Path

import uvicorn

from config import Config, load_config, local_url

PROJECT_DIR = Path(__file__).parent
SERVER_LOG = PROJECT_DIR / "server.log"

# The address this run of the server is listening on, written at start-up for
# the restart scripts. config.yaml only says where the *next* start will
# listen: once listenHost or listenPort has been edited, the server that is
# still running is somewhere else, and a restart has to find it there to stop
# its recordings cleanly.
SERVER_URL_FILE = PROJECT_DIR / "server.url"

# Same rotation as the recording log in main.py: 5 MB, 3 old files kept, so a
# machine that is never looked at can't fill its disk with log files.
LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "default",
            "filename": str(SERVER_LOG),
            "maxBytes": 5_000_000,
            "backupCount": 3,
        },
    },
    # uvicorn.error propagates up to "uvicorn", so it reaches the file handler
    # too. uvicorn.access does not propagate by default and needs its own.
    "loggers": {
        "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
    },
    # Catches anything logged by a library we didn't name — without it those
    # lines hit logging's "no handler" fallback and are written to a stderr
    # that pythonw.exe discards. main.py opts recording_api out of this, so
    # recording lines aren't written to both files.
    "root": {"handlers": ["file"], "level": "WARNING"},
}


def listen_address() -> tuple[str, int]:
    """Where to listen: listenHost and listenPort from config.yaml."""
    try:
        config = load_config()
    except Exception:
        # A broken config.yaml stops the server either way. Carrying on with
        # the defaults lets uvicorn get as far as main.py's start-up, which
        # writes the actual reason to the log before giving up — failing here
        # instead would say nothing at all under pythonw.exe.
        fields = Config.model_fields
        return fields["listen_host"].default, fields["listen_port"].default
    return config.listen_host, config.listen_port


if __name__ == "__main__":
    os.chdir(PROJECT_DIR)
    host, port = listen_address()
    try:
        SERVER_URL_FILE.write_text(local_url(host, port) + "\n")
    except OSError:
        # Only the restart scripts read it, and they fall back to config.yaml,
        # so this is no reason to keep the server from starting.
        pass
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        log_config=LOG_CONFIG,
        # There is no proxy in front of this server, so the address a request
        # comes from is the address of the machine that sent it — which is
        # what acceptRequestsFrom checks — and never one a header claims.
        proxy_headers=False,
    )
