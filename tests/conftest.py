"""Shared fixtures and fakes for the recording API test suite.

Everything here is offline: no ffmpeg is ever spawned (the fake process
stands in for Popen) and no audio device is touched (the device listings are
captured ffmpeg output shapes).
"""

import io
import time
from pathlib import Path

import pytest

from config import Config
from recording import Session


def make_config(**overrides) -> Config:
    """A Config with every required field set, overridable per test.

    snake_case names work because the model is configured with
    populate_by_name=True; the YAML file uses camelCase aliases instead.
    """
    data = {
        "input_device": "Dante Virtual Soundcard",
        "output_path": Path("/tmp/recordings"),
        "companion_base_urls": ["http://127.0.0.1:8001"],
    }
    data.update(overrides)
    return Config(**data)


class FakeStdin:
    """Stand-in for Popen.stdin: records writes, can fail like a closed pipe."""

    def __init__(self, fail_with: Exception | None = None):
        self.fail_with = fail_with
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.writes.append(data)

    def flush(self) -> None:
        pass


class FakeProcess:
    """Enough of Popen for the code under test: poll/wait/stdin/stderr.

    poll() returns the process's code every time, so a "finished" process is
    one whose code is not None — there is no reaping state to model, which
    is all these tests need.
    """

    def __init__(
        self,
        code: int | None = None,
        stdin_failure: Exception | None = None,
        wait_raises: Exception | None = None,
    ):
        self.code = code
        self.stdin = FakeStdin(fail_with=stdin_failure)
        self.stderr = io.BytesIO()
        self._wait_raises = wait_raises
        self.was_terminated = False
        self.was_killed = False
        self.sent_signals: list = []

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int | None:
        if self._wait_raises is not None:
            raise_me, self._wait_raises = self._wait_raises, None
            raise raise_me
        return self.code

    def terminate(self) -> None:
        self.was_terminated = True
        self.code = 0

    def kill(self) -> None:
        self.was_killed = True
        self.code = 0

    def send_signal(self, sig) -> None:
        self.sent_signals.append(sig)


def make_session(code: int | None = None, **kwargs) -> Session:
    """A Session wrapped around a fake process, nothing running for real."""
    return Session(
        service_name="9am",
        process=FakeProcess(code=code),
        started_at=time.time(),
        path="/recordings/sanctuary/9am/sanctuary_9am_2026-09-15_09-00-00.mp3",
        **kwargs,
    )


@pytest.fixture
def request_body():
    from recording import RecordingRequest

    return RecordingRequest(room_name="sanctuary", service_name="9am")


@pytest.fixture
def config() -> Config:
    return make_config()


# Captured shape of `ffmpeg -f avfoundation -list_devices true -i ""` stderr.
# The video devices come first and must not be read as audio devices; the
# trailer lines are ffmpeg complaining that the empty input name is not a
# device, which is expected and ends the list.
AVFOUNDATION_DEVICES = """\
[AVFoundation indev @ 0x7fb7d8c050e0] AVFoundation video devices:
[AVFoundation indev @ 0x7fb7d8c050e0] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7fb7d8c050e0] [1] Capture screen 0
[AVFoundation indev @ 0x7fb7d8c050e0] AVFoundation audio devices:
[AVFoundation indev @ 0x7fb7d8c050e0] [0] Dante Virtual Soundcard
[AVFoundation indev @ 0x7fb7d8c050e0] [1] MacBook Pro Microphone
[AVFoundation indev @ 0x7fb7d8c050e0] 1.1: Input audio device
[AVFoundation indev @ 0x7fb7d8c050e0] Could not find video device matching ''
"""

# Captured shape of `ffmpeg -f dshow -list_devices true -i ""` stderr.
# Note the padded device names ("DVS Receive  9-10" has two spaces so the
# numbers line up), the friendly-name / alternative-name pairing, and that
# every device carries a stereo pair whose range is in its name.
DSHOW_DEVICES = """\
[dshow @ 0x55f06b0f1d80] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0x55f06b0f1d80]  "Integrated Camera"
[dshow @ 0x55f06b0f1d80] DirectShow audio devices
[dshow @ 0x55f06b0f1d80]  "DVS Receive 1-2 (Dante Virtual Soundcard)" (audio)
[dshow @ 0x55f06b0f1d80]  "DVS Receive  9-10 (Dante Virtual Soundcard)" (audio)
[dshow @ 0x55f06b0f1d80]    Alternative name "@device_cm_{33D9A762-B2DB-47B5-8E4D-8D0FA400107E}\\wave_{2F8F0B60-8F04-4E76-85B7-12A114CE5B31}"
[dshow @ 0x55f06b0f1d80]  "Microphone Array (Realtek Audio)" (audio)
[dshow @ 0x55f06b0f1d80]  DirectShow audio only device list (with usage hints):
[dshow @ 0x55f06b0f1d80]  "DVS Receive 15-16 (Dante Virtual Soundcard)" (audio)
[dshow @ 0x55f06b0f1d80]    Alternative name "@device_cm_{33D9A762-B2DB-47B5-8E4D-8D0FA400107E}\\wave_{3F8F0B60-8F04-4E76-85B7-12A114CE5B31}"
[dshow @ 0x55f06b0f1d80]  Could not enumerate video devices (or none associated with this filter)
"""