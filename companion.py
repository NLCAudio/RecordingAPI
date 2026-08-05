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

    while True:
        try:
            status = json.dumps(registry.status())
            push_session.post(url, params={"value": status})
        except requests.RequestException:
            # Companion being down must not kill the loop; it will be back.
            pass

        time.sleep(interval)
