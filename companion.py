"""
Bitfocus Companion status push
==============================

Pushes the current recording status to Companion as a custom variable named
"recording_status", so button panels show what is happening without polling
this server themselves.
"""

import json
import logging
import time

import requests

from config import Config
from recording import SessionRegistry

logger = logging.getLogger("recording_api")


def push_status(registry: SessionRegistry, config: Config) -> None:
    # Runs forever in a background thread. Unlike the request handlers, this
    # reads config once at startup: changing companionBaseUrl or
    # statusPushRefreshHz needs a restart of the server to take effect.
    interval = 1.0 / config.status_push_refresh_hz
    url = f"{config.companion_base_url}/api/custom-variable/recording_status/value"

    # A persistent session (not to be confused with a recording Session) keeps
    # the HTTP connection alive between pushes rather than reconnecting each
    # time, which matters at 15 pushes a second.
    push_session = requests.Session()

    # Companion can be down for a whole service. At 15 pushes a second that is
    # tens of thousands of identical lines, which would rotate everything else
    # out of the log — so only the transitions are logged, not every failure.
    failing = False

    while True:
        try:
            status = json.dumps(registry.status())
            response = push_session.post(url, params={"value": status})
            # Reaching Companion is not the same as it accepting the push: a
            # renamed custom variable answers 404 and the panel silently stops
            # updating, which is exactly the kind of thing this log is for.
            response.raise_for_status()
            if failing:
                logger.info("companion push recovered at %s", url)
                failing = False
        except requests.RequestException as e:
            # Companion being down must not kill the loop; it will be back.
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
            logger.exception("companion push loop error")

        time.sleep(interval)
