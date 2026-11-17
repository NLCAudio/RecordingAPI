"""
macOS capture backend (avfoundation)
====================================

On macOS the Dante Virtual Soundcard is a single device carrying all of its
channels, and ffmpeg addresses devices by number:

    [AVFoundation indev @ 0x...] AVFoundation audio devices:
    [AVFoundation indev @ 0x...] [0] Dante Virtual Soundcard
    [AVFoundation indev @ 0x...] [1] MacBook Pro Microphone

So this backend only translates the device name from config.yaml into its
index, and the requested channel numbers into channel indexes of that device.

Not the default: ffmpeg loses audio capturing this way (see audioqueue.py). It
is used only when config.yaml asks for it by name, and every recording made
through it is logged with a warning.
"""

import logging
import re

from .base import CaptureBackend, CaptureInput, DeviceNotFoundError, pan_to_stereo

# What follows this line in ffmpeg's output is an audio device. The video
# devices above it have the same "[N] name" shape, so reading starts here.
AUDIO_HEADER = "audio devices:"
DEVICE_LINE = re.compile(r"\[(\d+)\]\s+(.+?)\s*$")

logger = logging.getLogger("recording_api")


class AVFoundationBackend(CaptureBackend):
    framework = "avfoundation"

    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        # Per recording, not once at start-up, so a config.yaml left on this
        # setting by accident shows up next to every recording it damaged.
        logger.warning(
            "recording through ffmpeg's avfoundation capture, which drops audio "
            "(about a tenth of it when measured). Set 'framework: auto' in "
            "config.yaml to use the audioqueue capture instead."
        )
        device_index = self.find_device_index()
        return CaptureInput(
            # avfoundation names inputs "<video>:<audio>"; an empty first half
            # means no video, audio device N only.
            args=self.input_args(f":{device_index}"),
            # One device carries every channel, so channel numbers are channel
            # indexes, counted from zero instead of one.
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
            # Strip the "[AVFoundation indev @ 0x7f...]" prefix of every line.
            line = re.sub(r"^\[[^\]]+\]\s*", "", line)

            if AUDIO_HEADER in line.lower():
                in_audio_section = True
                continue
            if not in_audio_section:
                continue

            match = DEVICE_LINE.match(line)
            if match is None:
                # The list ends at the first line that is not a device: the
                # error about the empty input name.
                break
            devices[match.group(1)] = self.normalise(match.group(2))

        return devices
