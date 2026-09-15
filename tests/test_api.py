"""HTTP layer: endpoint wiring, error mapping, request validation."""

import pytest
from fastapi.testclient import TestClient

import main
from conftest import make_session

FAKE_STATUS = {"status": "online", "sessions": {}}


@pytest.fixture
def client(monkeypatch):
    # Keep the lifespan side effects out of the way: no log file handlers,
    # no status-push threads reaching for real Companion addresses.
    monkeypatch.setattr(main, "setup_logging", lambda path: None)
    monkeypatch.setattr(main, "start_status_push", lambda registry, config: None)
    with TestClient(main.app) as test_client:
        yield test_client


def test_status_endpoint(client, monkeypatch):
    monkeypatch.setattr(main.registry, "status", lambda: FAKE_STATUS)
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json() == FAKE_STATUS


def test_start_recording(client, monkeypatch):
    session = make_session()
    monkeypatch.setattr(main.registry, "start", lambda request, config: session)

    response = client.post("/recording/start", json={"room_name": "sanctuary"})

    assert response.status_code == 200
    assert response.json()["message"] == "recording started for sanctuary"


def test_start_when_already_recording(client, monkeypatch):
    monkeypatch.setattr(main.registry, "start", lambda request, config: None)

    response = client.post("/recording/start", json={"room_name": "sanctuary"})

    assert response.json()["message"] == "sanctuary is already recording"


def test_stop_recording_returns_file(client, monkeypatch):
    session = make_session()
    monkeypatch.setattr(main.registry, "stop", lambda room: session)

    response = client.post("/recording/stop", json={"room_name": "sanctuary"})

    assert response.status_code == 200
    assert response.json()["file"] == session.path


def test_stop_when_not_recording(client, monkeypatch):
    monkeypatch.setattr(main.registry, "stop", lambda room: None)
    response = client.post("/recording/stop", json={"room_name": "sanctuary"})
    assert response.json()["message"] == "sanctuary is not recording"


def test_toggle_stops_when_running(client, monkeypatch):
    session = make_session()
    monkeypatch.setattr(main.registry, "toggle", lambda request, config: (False, session))

    response = client.post("/recording/toggle", json={"room_name": "sanctuary"})

    assert response.json()["message"] == "recording stopped for sanctuary"
    assert response.json()["file"] == session.path


def test_toggle_starts_when_idle(client, monkeypatch):
    session = make_session()
    monkeypatch.setattr(main.registry, "toggle", lambda request, config: (True, session))

    response = client.post("/recording/toggle", json={"room_name": "sanctuary"})

    assert response.json()["message"] == "recording started for sanctuary"


def test_zero_channel_rejected_with_422(client):
    # Channels are numbered from 1; a 0 must fail validation, not ffmpeg.
    response = client.post(
        "/recording/start",
        json={"room_name": "x", "left_input_channel": 0},
    )
    assert response.status_code == 422


def test_device_error_maps_to_422(client, monkeypatch):
    from backends.base import DeviceNotFoundError

    def boom(request, config):
        raise DeviceNotFoundError(
            requested="ghost", found_devices=["Dante Virtual Soundcard"]
        )

    monkeypatch.setattr(main.registry, "start", boom)
    response = client.post("/recording/start", json={"room_name": "x"})
    assert response.status_code == 422
    assert "ghost" in response.json()["detail"]


def test_os_error_maps_to_400(client, monkeypatch):
    def boom(request, config):
        raise OSError("ffmpeg not installed")

    monkeypatch.setattr(main.registry, "start", boom)
    response = client.post("/recording/start", json={"room_name": "x"})
    assert response.status_code == 400
    assert "ffmpeg not installed" in response.json()["detail"]


def test_unknown_path_logged_as_404(client, caplog):
    response = client.get("/nope")
    assert response.status_code == 404
    assert "-> 404" in caplog.text