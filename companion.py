"""
Bitfocus Companion status push
==============================

Pushes the current recording status to every Companion instance listed under
companionBaseUrls in config.yaml, as a custom variable named
"recording_status", so button panels show what is happening without polling
this server themselves.

Each address gets its own thread. A Companion that is switched off or on the
wrong subnet answers slowly or not at all, and a single loop pushing to all of
them in turn would hold every working panel back while it waited on the broken
one — so each panel is kept independent of the others.
"""

import json
import logging
import threading
import time

import requests

from config import Config
from recording import SessionRegistry

logger = logging.getLogger("recording_api")

# The Companion endpoint that sets the custom variable, appended to each
# configured base address.
VARIABLE_PATH = "/api/custom-variable/recording_status/value"

# How long to wait for one push before giving up on it. Without a timeout a
# Companion that accepts the connection and then stops answering — a machine
# suspended mid-service, most often — would hang its thread indefinitely, and
# that panel would freeze on its last value with nothing in the log.
PUSH_TIMEOUT_S = 2.0


def start_status_push(registry: SessionRegistry, config: Config) -> None:
    """Start one background push thread per configured Companion address.

    Config is read once here: changing companionBaseUrls or
    statusPushRefreshHz needs a restart of the server to take effect.
    """
    interval = 1.0 / config.status_push_refresh_hz

    for base_url in config.companion_base_urls:
        # daemon=True means these threads are killed automatically when the main
        # program exits, so we don't need to clean them up manually.
        threading.Thread(
            target=push_status,
            args=(registry, base_url, interval),
            daemon=True,
            name=f"push-{base_url}",
        ).start()


def push_status(registry: SessionRegistry, base_url: str, interval: float) -> None:
    """Push the status to one Companion instance, forever."""
    url = base_url + VARIABLE_PATH

    # A persistent session (not to be confused with a recording Session) keeps
    # the HTTP connection alive between pushes rather than reconnecting each
    # time, which matters at 15 pushes a second.
    push_session = requests.Session()

    # Companion can be down for a whole service. At 15 pushes a second that is
    # tens of thousands of identical lines, which would rotate everything else
    # out of the log — so only the transitions are logged, not every failure.
    # Per address, so one dead panel cannot mask a second one going down.
    failing = False

    while True:
        try:
            status = json.dumps(registry.status())
            response = push_session.post(
                url, params={"value": status}, timeout=PUSH_TIMEOUT_S
            )
            # Reaching Companion is not the same as it accepting the push: a
            # renamed custom variable answers 404 and the panel silently stops
            # updating, which is exactly the kind of thing this log is for.
            response.raise_for_status()
            if failing:
                logger.info("companion push recovered at %s", url)
                failing = False
        except requests.RequestException as e:
            # This address being unreachable must not kill the loop or affect
            # the other addresses; it will be back.
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
