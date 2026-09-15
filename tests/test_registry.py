"""Registry state machine: start/stop/toggle, eviction, failure paths.

All tests route around ffmpeg entirely: start_ffmpeg is replaced with a
factory that returns a Session around a fake process, and the reader/sync
threads are no-ops. What is exercised is the registry's decisions — which
session is created, what happens on double-start, double-stop, toggle, and
when the underlying process has died.
"""

import pytest

import recording
from conftest import make_config, make_session
from recording import RecordingRequest, SessionRegistry


def patch_spawn(monkeypatch, session=None):
    """Replace everything start() would spawn with inert stand-ins."""
    created = {}

    def fake_start_ffmpeg(request, config):
        session_ = session if session is not None else make_session()
        created["session"] = session_
        return session_

    monkeypatch.setattr(recording, "start_ffmpeg", fake_start_ffmpeg)
    monkeypatch.setattr(recording, "relay_ffmpeg_output", lambda *a: None)
    monkeypatch.setattr(recording, "confirm_started", lambda *a: None)
    monkeypatch.setattr(recording, "sync_to_disk", lambda *a: None)
    return created


def test_start_registers_room(monkeypatch):
    created = patch_spawn(monkeypatch)
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary", service_name="9am")

    session = registry.start(request, make_config())

    assert session is created["session"]
    status = registry.status()["sessions"]["sanctuary"]
    assert status["recording"] is True
    assert status["service_name"] == "9am"
    assert status["elapsed_str"].startswith("00:00:")


def test_start_returns_none_when_already_recording(monkeypatch):
    created = patch_spawn(monkeypatch)
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary")

    assert registry.start(request, make_config()) is created["session"]
    assert registry.start(request, make_config()) is None


def test_stop_returns_session_and_frees_room(monkeypatch):
    patch_spawn(monkeypatch)
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary")

    started = registry.start(request, make_config())
    stopped = registry.stop("sanctuary")

    assert stopped is started
    assert "sanctuary" not in registry.status()["sessions"]
    assert registry.stop("sanctuary") is None  # second stop is a no-op


def test_toggle_starts_when_idle(monkeypatch):
    patch_spawn(monkeypatch)
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary")

    started, session = registry.toggle(request, make_config())

    assert started is True
    assert session is not None
    assert registry.status()["sessions"]["sanctuary"]["recording"] is True


def test_toggle_stops_when_recording(monkeypatch):
    patch_spawn(monkeypatch)
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary")
    registry.toggle(request, make_config())

    started, session = registry.toggle(request, make_config())

    assert started is False
    assert session is not None
    assert "sanctuary" not in registry.status()["sessions"]


def test_dead_ffmpeg_session_evicted(monkeypatch):
    # The process carries a non-None exit code: poll() says it has exited.
    patch_spawn(monkeypatch, session=make_session(code=0))
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary")
    registry.start(request, make_config())

    assert registry._live_session("sanctuary") is None  # forgotten
    assert registry.status()["sessions"] == {}


def test_failed_start_leaves_room_idle(monkeypatch):
    patch_spawn(monkeypatch)

    def fail(*args):
        raise OSError("ffmpeg exited with code 1 without recording x.mp3")

    monkeypatch.setattr(recording, "confirm_started", fail)
    registry = SessionRegistry()
    request = RecordingRequest(room_name="sanctuary")

    with pytest.raises(OSError):
        registry.start(request, make_config())
    assert registry.status()["sessions"] == {}


def test_status_reports_no_sessions_when_empty():
    registry = SessionRegistry()
    assert registry.status() == {"status": "online", "sessions": {}}