"""macOS avfoundation backend: device listing and channel-to-index mapping."""

import pytest

from backends.avfoundation import AVFoundationBackend
from backends.base import DeviceNotFoundError
from conftest import AVFOUNDATION_DEVICES, make_config


def make_backend(config=None) -> AVFoundationBackend:
    return AVFoundationBackend(config or make_config())


def test_list_audio_devices_skips_video_and_stops_at_trailer(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: AVFOUNDATION_DEVICES)
    assert backend.list_audio_devices() == {
        "0": "dante virtual soundcard",
        "1": "macbook pro microphone",
    }


def test_find_device_index_matches_normalised_names(monkeypatch):
    backend = make_backend(make_config(input_device="MacBook Pro Microphone"))
    monkeypatch.setattr(backend, "list_devices_output", lambda: AVFOUNDATION_DEVICES)
    assert backend.find_device_index() == "1"


def test_find_device_index_raises_when_device_missing(monkeypatch):
    backend = make_backend(make_config(input_device="Ghost Mic"))
    monkeypatch.setattr(backend, "list_devices_output", lambda: AVFOUNDATION_DEVICES)
    with pytest.raises(DeviceNotFoundError):
        backend.find_device_index()


def test_capture_input_addresses_device_and_pans(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: AVFOUNDATION_DEVICES)
    capture_input = backend.capture_input(1, 2)
    # avfoundation names inputs "<video>:<audio>"; empty video half
    assert capture_input.args == [
        "-f", "avfoundation", "-thread_queue_size", "4096", "-i", ":0",
    ]
    # channel numbers are 1-based but pan indexes are 0-based
    assert capture_input.filter_prefix == "[0:a]pan=stereo|c0=c0|c1=c1"


def test_capture_input_offsets_arbitrary_channels(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: AVFOUNDATION_DEVICES)
    capture_input = backend.capture_input(2, 3)
    assert capture_input.filter_prefix == "[0:a]pan=stereo|c0=c1|c1=c2"