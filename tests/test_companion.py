"""Companion status push loop: delivery, failure throttling, recovery."""

import requests
import pytest

import companion
from conftest import make_session
from recording import SessionRegistry


class Response:
    def __init__(self, code: int = 200):
        self.code = code

    def raise_for_status(self):
        if self.code >= 400:
            raise requests.HTTPError(f"HTTP {self.code}")


def stop_after(n_sleeps: int):
    """time.sleep stand-in that ends the infinite loop after n calls."""
    slept = {"n": 0}

    def fake_sleep(_interval):
        slept["n"] += 1
        if slept["n"] > n_sleeps:
            raise SystemExit

    return fake_sleep


def make_registry() -> SessionRegistry:
    registry = SessionRegistry()
    session = make_session()
    registry._sessions["sanctuary"] = session
    return registry


URL = "http://panel:8001"
VARIABLE_URL = URL + "/api/custom-variable/recording_status/value"


def test_push_status_posts_status_json(monkeypatch):
    posts = []

    class Session:
        def post(self, url, params=None, timeout=None):
            posts.append((url, params, timeout))
            return Response()

    monkeypatch.setattr(companion.requests, "Session", lambda: Session())
    monkeypatch.setattr(companion.time, "sleep", stop_after(2))

    with pytest.raises(SystemExit):
        companion.push_status(make_registry(), URL, 1 / 15)

    assert len(posts) == 3  # initial push plus one per sleep
    url, params, timeout = posts[0]
    assert url == VARIABLE_URL
    assert '"recording": true' in params["value"]
    assert timeout == companion.PUSH_TIMEOUT_S


def test_push_status_logs_failure_once_then_recovery(monkeypatch, caplog):
    caplog.set_level("INFO")  # the recovery line is logged at INFO
    failures = [True, True]  # two failures, then success

    class Flaky:
        def post(self, url, params=None, timeout=None):
            if failures:
                failures.pop(0)
                raise requests.ConnectionError("panel switched off")
            return Response()

    monkeypatch.setattr(companion.requests, "Session", lambda: Flaky())
    monkeypatch.setattr(companion.time, "sleep", stop_after(3))

    with pytest.raises(SystemExit):
        companion.push_status(make_registry(), URL, 1 / 15)

    text = caplog.text
    assert text.count("failing at") == 1  # not one per failure
    assert "recovered" in text


def test_push_status_loop_error_logged_and_loop_continues(monkeypatch, caplog):
    class Boom:
        def post(self, *args, **kwargs):
            raise ValueError("a bug in the loop")

    monkeypatch.setattr(companion.requests, "Session", lambda: Boom())
    monkeypatch.setattr(companion.time, "sleep", stop_after(2))

    with pytest.raises(SystemExit):
        companion.push_status(make_registry(), URL, 1 / 15)

    assert "loop error" in caplog.text