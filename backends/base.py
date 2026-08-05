"""
Capture backend interface
=========================

Every operating system exposes audio devices to ffmpeg differently, so the
device-specific knowledge lives here instead of being spread through main.py.

A backend answers exactly one question:

    "I want channel L on the left and channel R on the right — which ffmpeg
     inputs do I open, and how do I route them into a stereo stream?"

Everything after that (encoding to MP3, measuring dB levels, writing the file)
is identical on every platform and stays in main.py.

Channel numbers used by this interface are 1-based, exactly like the channel
numbers shown in Dante Controller: channel 1 is the first channel.
"""

import dataclasses
import re
import subprocess
from abc import ABC, abstractmethod

from config import Config

# How many audio packets ffmpeg may queue up before it starts dropping them.
# The capture device hands ffmpeg one small buffer at a time and the OS
# silently discards audio while ffmpeg is busy, so any downstream stall (disk,
# encoder, thread scheduling) longer than a few tens of milliseconds becomes an
# audible glitch. A deep queue lets the input thread keep draining the device
# through such stalls.
THREAD_QUEUE_SIZE = "4096"


@dataclasses.dataclass(frozen=True)
class CaptureInput:
    """The device-specific front half of an ffmpeg command line."""

    # Every argument from the first -f up to and including the last -i.
    args: list[str]
    # A filter graph consuming those inputs and producing one stereo stream,
    # e.g. "[0:a]pan=stereo|c0=c0|c1=c1". main.py appends the level metering
    # to it and maps the result into the output file. A backend that needs
    # more than one input (Windows) also combines them here.
    filter_prefix: str


def pan_to_stereo(left_index: int, right_index: int) -> str:
    """Route two channels of the filter graph's input to left and right.

    The indexes are 0-based positions *within* the graph's input, which is not
    the channel number the caller asked for: on Windows channel 7 is channel 0
    of the "7-8" device.
    """
    return f"pan=stereo|c0=c{left_index}|c1=c{right_index}"


class DeviceNotFoundError(RuntimeError):
    """The configured input device is not among the devices ffmpeg listed."""

    def __init__(self, requested: str, found_devices: list[str], *args):
        self.requested = requested
        self.found_devices = found_devices
        self.message = (
            f"Device requested: {requested}, but found: {', '.join(found_devices)}"
        )
        super().__init__(self.message)


class ChannelNotAvailableError(RuntimeError):
    """The requested channels exist, but not in a form we can record."""

    def __init__(self, message: str, *args):
        self.message = message
        super().__init__(message)


class CaptureBackend(ABC):
    # The value passed to ffmpeg's -f, e.g. "avfoundation" or "dshow".
    framework: str

    def __init__(self, config: Config):
        self.config = config

    @abstractmethod
    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        """Resolve two 1-based channel numbers into an ffmpeg input."""

    def list_devices_output(self) -> str:
        # ffmpeg has no "just list the devices" mode: it prints the list, then
        # complains that the empty input name does not exist and exits
        # non-zero. That is expected — the list we want is in stderr either way.
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
        # Device names are padded so their numbers line up in ffmpeg's output
        # ("DVS Receive  9-10" vs "DVS Receive 15-16"), so collapse runs of
        # whitespace before comparing anything.
        return re.sub(r"\s+", " ", name).strip().lower()
