"""
Capture backend interface
=========================

Every operating system exposes audio devices differently; that knowledge lives
in the backends rather than in recording.py.

A backend answers one question:

    "I want channel L on the left and channel R on the right — where does
     ffmpeg get that audio, and how is it routed into a stereo stream?"

Either ffmpeg opens the device itself (Windows), or the backend reads the
device and feeds ffmpeg through a pipe (macOS); CaptureInput covers both.
Everything after that (MP3 encoding, level metering, writing the file) is the
same on every platform and stays in recording.py.

Channel numbers in this interface count from 1, as in Dante Controller.
"""

import dataclasses
import re
import subprocess
from abc import ABC, abstractmethod
from typing import BinaryIO, Protocol

from config import Config

# How many audio packets ffmpeg may queue between reading a device and
# processing them, where ffmpeg reads the device itself. A deep queue lets its
# reader keep draining the device through a stall further down (disk, encoder,
# scheduling). It does nothing about audio lost before that reader, which is
# how avfoundation loses it; whether dshow needs it has not been measured.
THREAD_QUEUE_SIZE = "4096"


@dataclasses.dataclass(frozen=True)
class CaptureInput:
    """The device-specific front half of an ffmpeg command line."""

    # Every argument from the first -f up to and including the last -i.
    args: list[str]
    # A filter graph turning those inputs into one stereo stream, e.g.
    # "[0:a]pan=stereo|c0=c0|c1=c1", combining them first if there are several
    # (Windows). recording.py appends the level metering and maps the output.
    filter_prefix: str
    # Set when the server itself writes the audio into ffmpeg's stdin (macOS).
    # The recording then starts with feeder.start() and ends with feeder.stop(),
    # which closes that pipe, instead of ffmpeg being sent "q".
    feeder: "AudioFeeder | None" = None


class AudioFeeder(Protocol):
    """Something that writes one recording's audio into ffmpeg's stdin."""

    def start(self, sink: BinaryIO, label: str) -> None:
        """Begin writing into sink; label names the recording in log lines.

        Raises OSError if the device won't open, leaving sink untouched.
        """

    def stop(self) -> None:
        """Write what is left and close the sink. Safe to call twice."""


def pan_to_stereo(left_index: int, right_index: int) -> str:
    """Route two channels of the filter graph's input to left and right.

    The indexes are 0-based positions within the graph's input, not the
    channel numbers the caller asked for: on Windows channel 7 is channel 0 of
    the "7-8" device.
    """
    return f"pan=stereo|c0=c{left_index}|c1=c{right_index}"


class DeviceNotFoundError(RuntimeError):
    """The configured input device is not among the devices that were found."""

    def __init__(self, requested: str, found_devices: list[str]):
        self.requested = requested
        self.found_devices = found_devices
        self.message = (
            f"Device requested: {requested}, but found: {', '.join(found_devices)}"
        )
        super().__init__(self.message)


class ChannelNotAvailableError(RuntimeError):
    """The requested channels exist, but not in a form we can record."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class CaptureBackend(ABC):
    # The name this backend is selected by with `framework` in config.yaml.
    # Where ffmpeg opens the device itself, it is also the value of ffmpeg's -f.
    framework: str

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        """Resolve two 1-based channel numbers into an ffmpeg input."""

    def list_devices_output(self) -> str:
        # ffmpeg has no "just list the devices" mode: it prints the list, then
        # fails on the empty input name. The list is in stderr either way.
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-f",
                self.framework,
                "-list_devices",
                "true",
                "-i",
                "",
            ],
            capture_output=True,
            text=True,
            check=False,  # the non-zero exit is the normal case, as above
        )
        return result.stderr

    def input_args(self, device: str) -> list[str]:
        return [
            "-f",
            self.framework,
            "-thread_queue_size",
            THREAD_QUEUE_SIZE,
            *self.extra_input_args(),
            "-i",
            device,
        ]

    def extra_input_args(self) -> list[str]:
        """Per-input options only some backends need. Optional override."""
        return []

    @staticmethod
    def normalise(name: str) -> str:
        # Device names are padded to line their numbers up ("DVS Receive  9-10"
        # vs "DVS Receive 15-16"), so collapse whitespace before comparing.
        return re.sub(r"\s+", " ", name).strip().lower()
