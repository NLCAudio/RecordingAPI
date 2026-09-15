"""Windows dshow backend: pair-per-device listing, channel mapping, amerge."""

import pytest

from backends.base import ChannelNotAvailableError, DeviceNotFoundError
from backends.dshow import DShowBackend
from conftest import DSHOW_DEVICES, make_config

ALT_9_10 = (
    "@device_cm_{33D9A762-B2DB-47B5-8E4D-8D0FA400107E}"
    "\\wave_{2F8F0B60-8F04-4E76-85B7-12A114CE5B31}"
)


def make_backend(config=None) -> DShowBackend:
    return DShowBackend(config or make_config())


def test_list_audio_devices_keeps_friendly_and_alternative_names(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    devices = backend.list_audio_devices()

    assert len(devices) == 4  # video devices and trailer lines excluded
    # the padded "9-10" name is preserved raw in the dict key...
    assert devices["DVS Receive  9-10 (Dante Virtual Soundcard)"] == ALT_9_10
    # ...a device without an alternative name is recorded as None
    assert devices["DVS Receive 1-2 (Dante Virtual Soundcard)"] is None
    assert devices["Microphone Array (Realtek Audio)"] is None


def test_map_channels_parses_ranges_from_names(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    channels = backend.map_channels_to_devices()

    assert sorted(channels) == [1, 2, 9, 10, 15, 16]
    # channel 9 is channel 0 of the 9-10 pair, channel 10 is channel 1
    assert channels[9].channel == 0
    assert channels[10].channel == 1
    assert channels[9].device_channels == 2
    # alternative names preferred over friendly ones when present
    assert channels[9].device == ALT_9_10
    # friendly name used when no alternative was listed
    assert channels[1].device == "DVS Receive 1-2 (Dante Virtual Soundcard)"


def test_map_channels_matches_padded_names_after_collapsing_whitespace(monkeypatch):
    backend = make_backend(make_config(input_device="DVS   Receive"))
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    assert sorted(backend.map_channels_to_devices()) == [1, 2, 9, 10, 15, 16]


def test_map_channels_raises_when_device_name_matches_nothing(monkeypatch):
    backend = make_backend(make_config(input_device="Ghost Hardware"))
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    with pytest.raises(DeviceNotFoundError):
        backend.map_channels_to_devices()


def test_find_channel_raises_with_available_list(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    channels = backend.map_channels_to_devices()
    with pytest.raises(ChannelNotAvailableError, match="Channel 5"):
        backend.find_channel(channels, 5)
    with pytest.raises(ChannelNotAvailableError, match="1, 2, 9, 10"):
        backend.find_channel(channels, 5)


def test_capture_input_within_one_device(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    capture_input = backend.capture_input(9, 10)

    assert len(capture_input.args) == 6  # one input
    assert capture_input.args[-1] == f"audio={ALT_9_10}"
    assert capture_input.filter_prefix == "[0:a]pan=stereo|c0=c0|c1=c1"


def test_capture_input_across_devices_merges_and_offsets(monkeypatch):
    backend = make_backend()
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    # ch2 lives on the 1-2 device, ch9 on the 9-10 device
    capture_input = backend.capture_input(2, 9)

    assert len(capture_input.args) == 12  # two inputs
    # amerge lays the inputs end to end, shifting ch9 up by 2ch (left device width)
    assert (
        capture_input.filter_prefix
        == "[0:a][1:a]amerge=inputs=2,pan=stereo|c0=c1|c1=c2"
    )


def test_capture_input_applies_audio_buffer_ms(monkeypatch):
    backend = make_backend(make_config(audio_buffer_ms=200))
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    capture_input = backend.capture_input(9, 10)
    assert "-audio_buffer_size" in capture_input.args
    assert "200" in capture_input.args


def test_capture_input_no_buffer_flag_by_default(monkeypatch):
    backend = make_backend(make_config(audio_buffer_ms=None))
    monkeypatch.setattr(backend, "list_devices_output", lambda: DSHOW_DEVICES)
    capture_input = backend.capture_input(9, 10)
    assert "-audio_buffer_size" not in capture_input.args