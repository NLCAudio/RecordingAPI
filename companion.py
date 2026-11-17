"""
Bitfocus Companion status push
==============================

Pushes the recording status to every Companion instance in companionBaseUrls,
as the custom variable "recording_status", so button panels show what is
happening without polling this server.

With serverName set, the variable is "recording_status_<serverName>", so two
servers reporting to one Companion each keep their own. Every push carries the
time it was made: a custom variable holds its last value forever, so a dead
server would otherwise look exactly like an idle one.

Each address gets its own thread. A Companion that is off or unreachable
answers slowly or not at all, and one loop pushing to all in turn would hold
every working panel back while it waited.
"""

import json
import logging
import threading
import time

import requests

from config import Config
from recording import SessionRegistry

logger = logging.getLogger("recording_api")

# The custom variable the status is pushed to, and the Companion endpoint that
# sets it, appended to each configured base address.
VARIABLE_NAME = "recording_status"
VARIABLE_PATH = "/api/custom-variable/{name}/value"

# How long one push may take. Without a timeout a Companion that accepts the
# connection and then stops answering (a machine suspended mid-service, most
# often) would hang its thread, freezing that panel with nothing in the log.
PUSH_TIMEOUT_S = 2.0


def start_status_push(registry: SessionRegistry, config: Config) -> None:
    """Start one background push thread per configured Companion address.

    Config is read once here, so changing companionBaseUrls or
    statusPushRefreshHz needs a restart of the server.
    """
    interval = 1.0 / config.status_push_refresh_hz

    for base_url in config.companion_base_urls:
        # Daemon threads end with the main program, so need no cleaning up.
        threading.Thread(
            target=push_status,
            args=(registry, base_url, interval, config.server_name),
            daemon=True,
            name=f"push-{base_url}",
        ).start()


def variable_name(server_name: str | None) -> str:
    """The Companion custom variable this server's status is pushed to."""
    if server_name is None:
        return VARIABLE_NAME
    return f"{VARIABLE_NAME}_{server_name}"


def build_status(registry: SessionRegistry, server_name: str | None) -> dict:
    """The registry's status, plus what a panel watching two servers needs.

    The extra fields are plain values on purpose: a Companion expression can
    read a number out of the JSON but cannot count the keys of "sessions".
    """
    status = registry.status()
    status["server"] = server_name
    status["recording_count"] = len(status["sessions"])
    # Whole seconds since the Unix epoch, to compare with Companion's own
    # $(internal:time_unix): a few seconds old means this server stopped pushing.
    status["pushed_at"] = int(time.time())
    return status


def push_status(
    registry: SessionRegistry,
    base_url: str,
    interval: float,
    server_name: str | None = None,
) -> None:
    """Push the status to one Companion instance, forever."""
    url = base_url + VARIABLE_PATH.format(name=variable_name(server_name))

    # A persistent session (not a recording Session) keeps the HTTP connection
    # open between pushes, which matters at 15 pushes a second.
    push_session = requests.Session()

    # Companion can be down for a whole service: tens of thousands of identical
    # failures that would rotate everything else out of the log. So only the
    # transitions are logged, per address, so one dead panel cannot mask another.
    failing = False

    while True:
        try:
            status = json.dumps(build_status(registry, server_name))
            response = push_session.post(
                url, params={"value": status}, timeout=PUSH_TIMEOUT_S
            )
            # Reaching Companion is not it accepting the push: a renamed custom
            # variable answers 404 and the panel silently stops updating.
            response.raise_for_status()
            if failing:
                logger.info("companion push recovered at %s", url)
                failing = False
        except requests.RequestException as e:
            # An unreachable address must not end the loop or affect the others.
            if not failing:
                logger.warning(
                    "companion push failing at %s: %s (further failures not logged "
                    "until it recovers)",
                    url,
                    e,
                )
                failing = True
        except Exception:
            # A bug in here would otherwise kill the thread without a word, and
            # the meter would just stop moving with the server still up.
            logger.exception("companion push loop error at %s", url)

        time.sleep(interval)
