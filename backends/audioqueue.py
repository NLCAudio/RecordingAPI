"""
macOS capture backend (AudioQueue)
==================================

The default on macOS. The audio is read from the device by the server itself,
through Apple's Audio Queue Services, and handed to ffmpeg over a pipe —
ffmpeg only encodes it. See macos_capture.py for why ffmpeg is not left to
read the device itself, as the avfoundation backend does: in short, it loses
audio doing so, and has no setting to stop it.

As with avfoundation, the Dante Virtual Soundcard is one device carrying all
of its channels, so the requested channel numbers are channel indexes of that
one device.
"""

from .base import (
    CaptureBackend,
    CaptureInput,
    ChannelNotAvailableError,
    DeviceNotFoundError,
    pan_to_stereo,
)
from .macos_capture import AudioQueueCapture, InputDevice, list_input_devices

# How much audio the capture queue holds when config.yaml does not say. It is
# the stall this server's own threads can have without losing audio; a stall
# in ffmpeg or the disk is absorbed separately and without limit, in memory.
# Memory, not latency, is the cost: at 64 channels this is about 25 MB per
# recording.
DEFAULT_BUFFER_MS = 2000


class AudioQueueBackend(CaptureBackend):
    framework = "audioqueue"

    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        device = self.find_device()

        for channel in (left_channel, right_channel):
            if not 1 <= channel <= device.channels:
                raise ChannelNotAvailableError(
                    f"Channel {channel} is not offered by '{device.name}'. "
                    f"Available channels: 1-{device.channels}"
                )

        return CaptureInput(
            # Raw PCM on stdin, in the format AudioQueueCapture writes: every
            # channel of the device, interleaved 32-bit floats. ffmpeg cannot
            # detect any of that from a bare stream, so all three are stated.
            args=[
                "-f",
                "f32le",
                "-ar",
                str(int(device.sample_rate)),
                "-ac",
                str(device.channels),
                "-i",
                "pipe:0",
            ],
            # One device carries every channel, so the channel numbers are the
            # channel indexes, just counted from zero instead of one.
            filter_prefix="[0:a]" + pan_to_stereo(left_channel - 1, right_channel - 1),
            feeder=AudioQueueCapture(
                device,
                buffer_ms=self.config.audio_buffer_ms or DEFAULT_BUFFER_MS,
                label=device.name,
            ),
        )

    def find_device(self) -> InputDevice:
        devices = list_input_devices()
        requested = self.normalise(self.config.input_device)

        for device in devices:
            if self.normalise(device.name) == requested:
                return device

        raise DeviceNotFoundError(
            requested=requested,
            found_devices=[self.normalise(device.name) for device in devices],
        )
