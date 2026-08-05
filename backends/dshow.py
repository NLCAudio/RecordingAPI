r"""
Windows capture backend (dshow)
===============================

Windows does not expose the Dante Virtual Soundcard as one multi-channel
device. Each stereo pair is a separate audio device, and ffmpeg addresses them
by name rather than by number:

    [in#0 @ ...] "DVS Receive  9-10 (Dante Virtual Soundcard)" (audio)
    [in#0 @ ...]   Alternative name "@device_cm_{33D9A762-...}\wave_{1050...}"

So a channel number here selects both a device and a channel inside it:
channel 9 is channel 0 of the "9-10" device, channel 10 is channel 1 of it.

The channel range is read out of the device name itself rather than being
built from a template, because the names are padded to line the numbers up
("DVS Receive  9-10" has two spaces, "DVS Receive 15-16" has one) and exact
string matching would silently miss half of them.

If the left and right channel land in different pairs, both devices are opened
and amerge glues them into one stream before the pan. That means two capture
clocks feeding one filter, so both inputs get a deep packet queue, and
audioBufferMs in config.yaml is the knob to reach for if that turns out to
drop audio in practice.
"""

import dataclasses
import re

from .base import (
    CaptureBackend,
    CaptureInput,
    ChannelNotAvailableError,
    DeviceNotFoundError,
    pan_to_stereo,
)

# ffmpeg prints the friendly name first, then the alternative name on the
# following line. Older builds prefix those lines with "[dshow @ 0x...]" and
# newer ones with "[in#0 @ 0x...]", so neither pattern anchors to the prefix.
DEVICE_LINE = re.compile(r'"([^"]+)"\s*\(audio\)')
ALTERNATIVE_NAME_LINE = re.compile(r'Alternative name\s+"([^"]+)"')
# The channel range inside a device name, e.g. the "9-10" of "DVS Receive 9-10".
CHANNEL_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)")


@dataclasses.dataclass(frozen=True)
class ChannelSource:
    """Where one requested channel lives once the device list is parsed."""

    device: str  # the value passed to ffmpeg's -i
    channel: int  # 0-based channel index *within* that device
    device_channels: int  # how many channels that device carries in total


class DShowBackend(CaptureBackend):
    framework = "dshow"

    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        channels = self.map_channels_to_devices()
        left = self.find_channel(channels, left_channel)
        right = self.find_channel(channels, right_channel)

        if left.device == right.device:
            devices = [left.device]
            merge = ""
            left_index, right_index = left.channel, right.channel
        else:
            # pan can only read from a single stream, so amerge has to lay the
            # two inputs out end to end first: the left device's channels, then
            # the right device's. That shifts the right channel up by the width
            # of the left device.
            devices = [left.device, right.device]
            merge = "amerge=inputs=2,"
            left_index = left.channel
            right_index = left.device_channels + right.channel

        args = []
        for device in devices:
            args += self.input_args(f"audio={device}")

        labels = "".join(f"[{i}:a]" for i in range(len(devices)))
        return CaptureInput(
            args=args,
            filter_prefix=labels + merge + pan_to_stereo(left_index, right_index),
        )

    def find_channel(
        self, channels: dict[int, ChannelSource], channel: int
    ) -> ChannelSource:
        if channel not in channels:
            raise ChannelNotAvailableError(
                f"Channel {channel} is not offered by any "
                f"'{self.config.input_device}' device. Available channels: "
                f"{', '.join(str(c) for c in sorted(channels))}"
            )
        return channels[channel]

    def extra_input_args(self) -> list[str]:
        # How much audio dshow buffers before handing it to ffmpeg, in
        # milliseconds. Left at the device default unless config.yaml sets it;
        # raising it trades latency (which does not matter for a recording)
        # for resilience against dropouts.
        buffer_ms = self.config.audio_buffer_ms
        return ["-audio_buffer_size", str(buffer_ms)] if buffer_ms else []

    def map_channels_to_devices(self) -> dict[int, ChannelSource]:
        """Map each available channel number to the device carrying it."""
        requested = self.normalise(self.config.input_device)
        devices = self.list_audio_devices()
        channels: dict[int, ChannelSource] = {}
        matched_any = False

        for name, alternative_name in devices.items():
            if requested not in self.normalise(name):
                continue
            matched_any = True

            match = CHANNEL_RANGE.search(name)
            if match is None:
                # A matching device whose name carries no channel range tells
                # us nothing about which channels it holds, so skip it.
                continue

            first, last = int(match.group(1)), int(match.group(2))
            for index, channel in enumerate(range(first, last + 1)):
                channels[channel] = ChannelSource(
                    # The alternative name is a device path rather than a
                    # label: it survives the sound card being renamed and does
                    # not depend on the padding in the friendly name, so prefer
                    # it whenever ffmpeg gave us one.
                    device=alternative_name or name,
                    channel=index,
                    device_channels=last - first + 1,
                )

        if not matched_any:
            raise DeviceNotFoundError(
                requested=requested, found_devices=list(devices)
            )

        return channels

    def list_audio_devices(self) -> dict[str, str | None]:
        """Map friendly device name -> alternative name (if ffmpeg listed one)."""
        devices: dict[str, str | None] = {}
        current_name = None

        for line in self.list_devices_output().splitlines():
            device_match = DEVICE_LINE.search(line)
            if device_match is not None:
                current_name = device_match.group(1)
                devices[current_name] = None
                continue

            alternative_match = ALTERNATIVE_NAME_LINE.search(line)
            if alternative_match is not None and current_name is not None:
                devices[current_name] = alternative_match.group(1)
                current_name = None

        return devices
