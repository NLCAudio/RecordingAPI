"""
macOS capture backend (avfoundation)
====================================

On macOS the Dante Virtual Soundcard shows up as a single device carrying all
of its channels, and ffmpeg addresses devices by number rather than by name:

    [AVFoundation indev @ 0x...] AVFoundation audio devices:
    [AVFoundation indev @ 0x...] [0] Dante Virtual Soundcard
    [AVFoundation indev @ 0x...] [1] MacBook Pro Microphone

So this backend only has to translate the device name from config.yaml into
its index, and the requested channel numbers straight into channel indexes of
that one device.
"""

import re

from .base import CaptureBackend, CaptureInput, DeviceNotFoundError, pan_to_stereo

# Everything after this line in ffmpeg's output is an audio device. The video
# devices listed above it use the same "[N] name" shape, so we must not start
# reading before it.
AUDIO_HEADER = "audio devices:"
DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")


class AVFoundationBackend(CaptureBackend):
    framework = "avfoundation"

    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        device_index = self.find_device_index()
        return CaptureInput(
            # avfoundation names inputs "<video>:<audio>"; the empty half
            # before the colon means "no video, audio device N only".
            args=self.input_args(f":{device_index}"),
            # One device carries every channel, so the channel numbers are the
            # channel indexes, just counted from zero instead of one.
            filter_prefix="[0:a]" + pan_to_stereo(left_channel - 1, right_channel - 1),
        )

    def find_device_index(self) -> str:
        devices = self.list_audio_devices()
        requested = self.normalise(self.config.input_device)

        for index, name in devices.items():
            if name == requested:
                return index

        raise DeviceNotFoundError(
            requested=requested, found_devices=list(devices.values())
        )

    def list_audio_devices(self) -> dict[str, str]:
        """Map device index -> normalised device name."""
        devices = {}
        in_audio_section = False

        for line in self.list_devices_output().splitlines():
            # Strip the "[AVFoundation indev @ 0x7f...]" prefix ffmpeg puts on
            # every line so it does not interfere with the parsing below.
            line = re.sub(r"^\[[^\]]+\]\s*", "", line)

            if AUDIO_HEADER in line.lower():
                in_audio_section = True
                continue
            if not in_audio_section:
                continue

            match = DEVICE_LINE.match(line)
            if match is None:
                # The device list ends at the first line that is not a device,
                # which is the error about the empty input name.
                break
            devices[match.group(1)] = self.normalise(match.group(2))

        return devices
