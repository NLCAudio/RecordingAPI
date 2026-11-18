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

  2. It has no console, so what uvicorn and Python itself print is thrown
     away, including the reason the server failed to start. Here it is kept in
     the logs folder instead.

The logs folder holds server.log (start, stop and HTTP request lines),
recording_api.log (recordings; logPath in config.yaml) and console.log (what
was printed with no console to show it, mostly the error that stopped the
server). Started from a terminal, the first two are shown there as well.
"""

import errno
import faulthandler
import logging
import logging.config
import os
import socket
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
LOG_FOLDER = PROJECT_DIR / "logs"

logger = logging.getLogger("run_server")


def log_config(on_terminal: bool) -> dict:
    """Where uvicorn's log lines go: server.log, and the terminal if asked."""
    handlers = {
        # Same rotation as the recording log in main.py: 5 MB, 3 old files kept.
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "default",
            "filename": str(LOG_FOLDER / "server.log"),
            "maxBytes": 5_000_000,
            "backupCount": 3,
        },
    }
    if on_terminal:
        handlers["terminal"] = {
            "class": "logging.StreamHandler",
            "formatter": "default",
        }
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
        },
        "handlers": handlers,
        # uvicorn.error propagates up to "uvicorn" and so reaches its handlers;
        # uvicorn.access does not propagate and needs its own.
        "loggers": {
            "uvicorn": {
                "handlers": list(handlers),
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": list(handlers),
                "level": "INFO",
                "propagate": False,
            },
        },
        # What this file logs, and libraries we did not name. main.py opts
        # recording_api out, so recording lines are not written to both files.
        "root": {"handlers": list(handlers), "level": "WARNING"},
    }


def on_terminal() -> bool:
    """Whether this was started from a terminal, where someone is watching."""
    return sys.stderr is not None and sys.stderr.isatty()


def goes_nowhere(stream) -> bool:
    """Whether what is written to this standard stream is thrown away."""
    if stream is None:
        return True  # pythonw.exe has no console
    if os.name != "posix":
        return False
    try:
        # launchd hands its agents the null device.
        return os.path.samestat(os.fstat(stream.fileno()), os.stat(os.devnull))
    except (OSError, ValueError):
        return False


def keep_output(path: Path) -> None:
    """Send what this process prints into a file, where it would go nowhere.

    Little is printed, but it is the only trace of some failures: an error in
    the code itself, an exception that ended a thread, a crash in a library.
    """
    if sys.stderr is None:
        # Left open: it is this process's output from here on.
        sys.stdout = sys.stderr = open(  # noqa: SIM115
            path, "a", buffering=1, encoding="utf-8", errors="backslashreplace"
        )
        return
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and goes_nowhere(stream):
            # The file descriptor itself, so that what Apple's audio
            # frameworks print is kept as well.
            with open(path, "a") as file:
                os.dup2(file.fileno(), stream.fileno())


def port_in_use(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        socket.create_server((host, port), family=family).close()
    except OSError as e:
        # Anything else that is wrong with the address is uvicorn's to report.
        return e.errno == errno.EADDRINUSE
    return False


def main() -> None:
    os.chdir(PROJECT_DIR)
    LOG_FOLDER.mkdir(exist_ok=True)
    keep_output(LOG_FOLDER / "console.log")
    # A crash inside a library ends the server without a Python error; this
    # prints where it was. The audio frameworks on macOS are called directly.
    faulthandler.enable()
    logging.config.dictConfig(log_config(on_terminal()))

    # Imported only now, so that a failure to import them is shown or kept.
    import uvicorn

    from config import ConfigError, load_config

    try:
        config = load_config()
    except ConfigError as e:
        logger.error("the server was not started: %s", e)
        sys.exit(1)

    if port_in_use(config.listen_host, config.listen_port):
        logger.error(
            "the server was not started: port %d on %s is already in use, most "
            "likely by a recording API server that is still running. "
            "`python stop_servers.py --list` shows them.",
            config.listen_port,
            config.listen_host,
        )
        sys.exit(1)

    uvicorn.run(
        "main:app",
        host=config.listen_host,
        port=config.listen_port,
        # Already set up above; uvicorn would only do the same again.
        log_config=None,
        # No proxy sits in front of this server, so the address acceptRequestsFrom
        # checks is the sender's own, never one a header claims.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
