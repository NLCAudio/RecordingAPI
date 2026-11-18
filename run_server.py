"""
Server launcher
===============

How the recording API is started: by hand with `python run_server.py`, by the
Windows scheduled task, and by the macOS launchd agent. It listens where
listenHost and listenPort in config.yaml say.

`uvicorn main:app` is fine while developing, but a server started in the
background has two problems this file solves:

  1. It starts in whatever folder the launcher uses (C:\\Windows\\System32 for
     Task Scheduler), and the relative paths in config.yaml (outputPath,
     logPath) are resolved against the working directory. Hence the chdir.

  2. Under pythonw.exe there is no console, so what uvicorn prints is thrown
     away, including the reason it failed to start. The log config below sends
     it to server.log instead.

server.log holds start/stop and HTTP request lines; recording problems go to
recording_api.log (logPath in config.yaml).
"""

import os
from pathlib import Path

import uvicorn

from config import Config, load_config

PROJECT_DIR = Path(__file__).parent
SERVER_LOG = PROJECT_DIR / "server.log"

# Same rotation as the recording log in main.py: 5 MB, 3 old files kept.
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
    # uvicorn.error propagates up to "uvicorn" and so reaches the file handler;
    # uvicorn.access does not propagate and needs its own.
    "loggers": {
        "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
        "uvicorn.access": {"handlers": ["file"], "level": "INFO", "propagate": False},
    },
    # Catches what libraries we did not name log; otherwise it would go to a
    # stderr that pythonw.exe discards. main.py opts recording_api out, so
    # recording lines are not written to both files.
    "root": {"handlers": ["file"], "level": "WARNING"},
}


def listen_address() -> tuple[str, int]:
    """Where to listen: listenHost and listenPort from config.yaml."""
    try:
        config = load_config()
    except Exception:
        # A broken config.yaml stops the server either way. Carrying on with
        # the defaults lets uvicorn reach main.py's start-up, which logs the
        # actual reason; failing here would say nothing under pythonw.exe.
        fields = Config.model_fields
        return fields["listen_host"].default, fields["listen_port"].default
    return config.listen_host, config.listen_port


if __name__ == "__main__":
    os.chdir(PROJECT_DIR)
    host, port = listen_address()
    uvicorn.run(
        "main:app",
        host=host,
        port=port,
        log_config=LOG_CONFIG,
        # No proxy sits in front of this server, so the address acceptRequestsFrom
        # checks is the sender's own, never one a header claims.
        proxy_headers=False,
    )
