"""
Background launcher
===================

Starts the recording API without a console window, for machines where it
should just be running whenever the operator is logged in.

Running `uvicorn main:app` from a terminal is fine for testing, but a
background launcher has two problems this file solves:

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

# 127.0.0.1 means only this machine can reach the API, which is all that is
# needed while Companion runs alongside it. Change to "0.0.0.0" to accept
# requests from a Companion on another machine — that also needs an inbound
# rule for this port in Windows Defender Firewall.
HOST = "127.0.0.1"
PORT = 8000

PROJECT_DIR = Path(__file__).parent
SERVER_LOG = PROJECT_DIR / "server.log"

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
}


if __name__ == "__main__":
    os.chdir(PROJECT_DIR)
    uvicorn.run("main:app", host=HOST, port=PORT, log_config=LOG_CONFIG)
