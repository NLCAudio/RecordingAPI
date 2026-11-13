"""
Shared pieces for the tests: a config that points nowhere real, and stand-ins
for the capture backends that make ffmpeg record a generated tone instead of a
device.
"""

import array
import math
import shutil
import threading
import time

from backends import CaptureInput
from config import Config

FFMPEG = shutil.which("ffmpeg")

SAMPLE_RATE = 48000

# A tenth of a second of a 440 Hz tone, stereo, as the 32-bit floats a feeder
# writes into ffmpeg.
TONE_100MS = array.array(
    "f",
    (
        0.5 * math.sin(2 * math.pi * 440 * frame / SAMPLE_RATE)
        for frame in range(SAMPLE_RATE // 10)
        for _channel in range(2)
    ),
).tobytes()


def make_config(output_path, **overrides) -> Config:
    """A valid Config that records into output_path and pushes to nowhere."""
    settings = {
        "input_device": "test device",
        "output_path": output_path,
        # Port 9 is the "discard" port; nothing listens there.
        "companion_base_urls": ["http://127.0.0.1:9"],
    }
    settings.update(overrides)
    return Config(**settings)


class ToneBackend:
    """A backend where ffmpeg produces the audio itself, as it does on Windows.

    -re makes ffmpeg generate the tone in real time, the way a device delivers
    it, rather than as fast as it can encode.
    """

    def capture_input(self, left_channel, right_channel):
        return CaptureInput(
            args=[
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate={SAMPLE_RATE}",
            ],
            filter_prefix="[0:a]pan=stereo|c0=c0|c1=c0",
        )


class BrokenBackend:
    """A backend whose input ffmpeg cannot open, so the recording never starts."""

    def capture_input(self, left_channel, right_channel):
        return CaptureInput(
            args=["-f", "lavfi", "-i", "there_is_no_such_source"],
            filter_prefix="[0:a]pan=stereo|c0=c0|c1=c0",
        )


class ToneFeeder:
    """Writes a tone into ffmpeg's stdin in real time, as the macOS capture does."""

    def __init__(self):
        self.label = None
        self.sink = None
        self.thread = None
        self.stop_requested = threading.Event()
        self.stop_lock = threading.Lock()
        self.stopped = False

    def start(self, sink, label):
        self.sink = sink
        self.label = label
        self.thread = threading.Thread(target=self.write_tone, daemon=True)
        self.thread.start()

    def write_tone(self):
        next_chunk_at = time.monotonic()
        try:
            while not self.stop_requested.is_set():
                self.sink.write(TONE_100MS)
                self.sink.flush()
                next_chunk_at += 0.1
                self.stop_requested.wait(max(0.0, next_chunk_at - time.monotonic()))
        except (OSError, ValueError):
            pass  # ffmpeg went away, or the pipe was closed under us

    def stop(self):
        with self.stop_lock:
            if self.stopped:
                return
            self.stopped = True
            self.stop_requested.set()
            if self.thread is not None:
                self.thread.join(2)
            try:
                self.sink.close()
            except OSError:
                pass


class FailingFeeder:
    """A feeder whose device will not open."""

    def start(self, sink, label):
        raise OSError("the device would not open")

    def stop(self):
        raise AssertionError("stop() called on a feeder that never started")


class FeederBackend:
    """A backend that feeds ffmpeg through a pipe, as the macOS backend does."""

    def __init__(self, feeder=None):
        self.feeder = feeder or ToneFeeder()

    def capture_input(self, left_channel, right_channel):
        return CaptureInput(
            args=["-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "-i", "pipe:0"],
            filter_prefix="[0:a]pan=stereo|c0=c0|c1=c1",
            feeder=self.feeder,
        )
