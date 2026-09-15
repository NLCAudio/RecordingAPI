"""Shared base backend behaviour: name normalisation, arg assembly, pan."""

import subprocess

import pytest

from backends.avfoundation import AVFoundationBackend
from backends.base import DeviceNotFoundError, pan_to_stereo


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dante Virtual Soundcard", "dante virtual soundcard"),
        # padded names: two spaces so the numbers line up in ffmpeg's list
        ("DVS Receive  9-10 (Dante Virtual Soundcard)", "dvs receive 9-10 (dante virtual soundcard)"),
        ("  Spaces   Galore  ", "spaces galore"),
        ("", ""),
    ],
)
def test_normalise(raw, expected):
    assert AVFoundationBackend.normalise(raw) == expected


def test_pan_to_stereo():
    assert pan_to_stereo(0, 1) == "pan=stereo|c0=c0|c1=c1"
    assert pan_to_stereo(1, 4) == "pan=stereo|c0=c1|c1=c4"


def test_input_args_shape(config):
    backend = AVFoundationBackend(config)
    args = backend.input_args(":0")
    assert args[:4] == ["-f", "avfoundation", "-thread_queue_size", "4096"]
    assert args[-2:] == ["-i", ":0"]


def test_list_devices_output_returns_stderr(monkeypatch, config):
    backend = AVFoundationBackend(config)

    class Result:
        stderr = "the device dump"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Result())
    assert backend.list_devices_output() == "the device dump"


def test_device_not_found_error_carries_request_and_found():
    error = DeviceNotFoundError(requested="ghost", found_devices=["mic", "speaker"])
    text = str(error)
    assert "ghost" in text
    assert "mic" in text
    assert "speaker" in text