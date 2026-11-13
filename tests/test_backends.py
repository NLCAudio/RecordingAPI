import array
import ctypes
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

import backends
from backends import (
    ChannelNotAvailableError,
    DeviceNotFoundError,
    audioqueue,
    get_backend,
)
from backends.audioqueue import (
    AudioQueueBackend,
    AudioQueueCapture,
    InputDevice,
    pick_stereo,
)
from backends.avfoundation import AVFoundationBackend
from backends.dshow import DShowBackend
from tests.support import make_config

ON_MAC = sys.platform == "darwin"

# What "ffmpeg -f dshow -list_devices true" prints on the Windows machine: one
# device per stereo pair, the pair's numbers padded so they line up.
DSHOW_DEVICES = r"""
[in#0 @ 000001a8] "DVS Receive  1-2 (Dante Virtual Soundcard)" (audio)
[in#0 @ 000001a8]   Alternative name "@device_cm_{33D9A762}\wave_{0001}"
[in#0 @ 000001a8] "DVS Receive  3-4 (Dante Virtual Soundcard)" (audio)
[in#0 @ 000001a8]   Alternative name "@device_cm_{33D9A762}\wave_{0002}"
[in#0 @ 000001a8] "DVS Receive  9-10 (Dante Virtual Soundcard)" (audio)
[in#0 @ 000001a8]   Alternative name "@device_cm_{33D9A762}\wave_{0005}"
[in#0 @ 000001a8] "Microphone (Realtek Audio)" (audio)
[in#0 @ 000001a8]   Alternative name "@device_cm_{33D9A762}\wave_{0009}"
[in#0 @ 000001a8] Error opening input: Immediate exit requested
"""

# And what "ffmpeg -f avfoundation -list_devices true" prints on a Mac.
AVFOUNDATION_DEVICES = """
[AVFoundation indev @ 0x7f8] AVFoundation video devices:
[AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7f8] AVFoundation audio devices:
[AVFoundation indev @ 0x7f8] [0] Dante Virtual Soundcard
[AVFoundation indev @ 0x7f8] [1] MacBook Pro Microphone
[in#0 @ 0x600] Error opening input: Input/output error
"""

DANTE = InputDevice(
    name="Dante Virtual Soundcard", uid="dante-uid", channels=64, sample_rate=48000.0
)
MICROPHONE = InputDevice(
    name="MacBook Air Microphone", uid="mic-uid", channels=1, sample_rate=44100.0
)


def config(**overrides):
    return make_config(
        "out", **{"input_device": "Dante Virtual Soundcard", **overrides}
    )


class SelectionTest(unittest.TestCase):
    def test_auto_follows_the_platform(self):
        for platform, expected in (
            ("darwin", AudioQueueBackend),
            ("win32", DShowBackend),
        ):
            with (
                self.subTest(platform),
                mock.patch.object(backends.sys, "platform", platform),
            ):
                self.assertIsInstance(get_backend(config()), expected)

    def test_auto_on_an_unsupported_platform_says_so(self):
        with (
            mock.patch.object(backends.sys, "platform", "linux"),
            self.assertRaises(RuntimeError) as raised,
        ):
            get_backend(config())
        self.assertIn("linux", str(raised.exception))

    def test_a_framework_can_be_asked_for_by_name(self):
        self.assertIsInstance(get_backend(config(framework="dshow")), DShowBackend)
        self.assertIsInstance(
            get_backend(config(framework="AudioQueue")), AudioQueueBackend
        )

    def test_an_unknown_framework_is_refused(self):
        with self.assertRaises(RuntimeError) as raised:
            get_backend(config(framework="alsa"))
        self.assertIn("alsa", str(raised.exception))


class DShowTest(unittest.TestCase):
    def backend(self, **overrides):
        backend = DShowBackend(config(**overrides))
        backend.list_devices_output = lambda: DSHOW_DEVICES
        return backend

    def test_a_pair_on_one_device_opens_that_device_once(self):
        capture = self.backend().capture_input(3, 4)

        self.assertEqual(capture.args.count("-i"), 1)
        self.assertEqual(capture.args[-1], r"audio=@device_cm_{33D9A762}\wave_{0002}")
        self.assertEqual(capture.filter_prefix, "[0:a]pan=stereo|c0=c0|c1=c1")
        self.assertIsNone(capture.feeder)

    def test_left_and_right_can_be_swapped(self):
        capture = self.backend().capture_input(4, 3)
        self.assertEqual(capture.filter_prefix, "[0:a]pan=stereo|c0=c1|c1=c0")

    def test_channels_on_two_devices_are_merged(self):
        capture = self.backend().capture_input(1, 10)

        self.assertEqual(capture.args.count("-i"), 2)
        # The right device's channels follow the left device's two, so its
        # second channel is the fourth of the merged stream.
        self.assertEqual(
            capture.filter_prefix, "[0:a][1:a]amerge=inputs=2,pan=stereo|c0=c0|c1=c3"
        )

    def test_the_padding_in_device_names_does_not_hide_a_pair(self):
        channels = self.backend().map_channels_to_devices()
        self.assertEqual(sorted(channels), [1, 2, 3, 4, 9, 10])

    def test_a_channel_no_device_carries_is_refused_with_what_there_is(self):
        with self.assertRaises(ChannelNotAvailableError) as raised:
            self.backend().capture_input(7, 8)
        self.assertIn("1, 2, 3, 4, 9, 10", raised.exception.message)

    def test_an_unknown_device_is_refused_with_what_was_found(self):
        with self.assertRaises(DeviceNotFoundError) as raised:
            self.backend(input_device="Focusrite").capture_input(1, 2)
        self.assertIn("Microphone (Realtek Audio)", raised.exception.message)

    def test_the_buffer_is_only_passed_when_set(self):
        self.assertNotIn("-audio_buffer_size", self.backend().capture_input(1, 2).args)

        args = self.backend(audio_buffer_ms=200).capture_input(1, 2).args
        self.assertEqual(args[args.index("-audio_buffer_size") + 1], "200")


class AVFoundationTest(unittest.TestCase):
    def backend(self, **overrides):
        backend = AVFoundationBackend(config(**overrides))
        backend.list_devices_output = lambda: AVFOUNDATION_DEVICES
        return backend

    def test_the_device_is_addressed_by_its_number_among_the_audio_devices(self):
        with self.assertLogs("recording_api", level="WARNING"):
            capture = self.backend().capture_input(5, 6)

        self.assertEqual(capture.args[-2:], ["-i", ":0"])
        self.assertEqual(capture.filter_prefix, "[0:a]pan=stereo|c0=c4|c1=c5")

    def test_every_recording_through_it_is_logged_as_lossy(self):
        with self.assertLogs("recording_api", level="WARNING") as logs:
            self.backend().capture_input(1, 2)
        self.assertIn("drops audio", logs.output[0])

    def test_an_unknown_device_is_refused(self):
        with (
            self.assertLogs("recording_api", level="WARNING"),
            self.assertRaises(DeviceNotFoundError),
        ):
            self.backend(input_device="Focusrite").capture_input(1, 2)


class AudioQueueBackendTest(unittest.TestCase):
    def backend(self, devices=(DANTE, MICROPHONE), **overrides):
        patcher = mock.patch.object(
            audioqueue, "list_input_devices", return_value=list(devices)
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return AudioQueueBackend(config(**overrides))

    def test_ffmpeg_is_handed_plain_stereo_at_the_devices_rate(self):
        capture = self.backend().capture_input(41, 7)

        self.assertEqual(
            capture.args, ["-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "pipe:0"]
        )
        self.assertEqual(capture.filter_prefix, "[0:a]pan=stereo|c0=c0|c1=c1")

    def test_the_two_channels_are_picked_by_the_capture_counted_from_zero(self):
        feeder = self.backend().capture_input(41, 7).feeder

        self.assertIs(feeder.device, DANTE)
        self.assertEqual((feeder.left, feeder.right), (40, 6))

    def test_the_capture_queue_is_never_shorter_than_two_seconds(self):
        # A value sized for the Windows buffer must not shrink the Mac's.
        self.assertEqual(self.backend().capture_input(1, 2).feeder.buffer_ms, 2000)
        self.assertEqual(
            self.backend(audio_buffer_ms=200).capture_input(1, 2).feeder.buffer_ms, 2000
        )
        self.assertEqual(
            self.backend(audio_buffer_ms=5000).capture_input(1, 2).feeder.buffer_ms,
            5000,
        )

    def test_a_channel_the_device_does_not_have_is_refused(self):
        for left, right in ((0, 1), (1, 65), (65, 1)):
            with self.subTest(left=left, right=right):
                with self.assertRaises(ChannelNotAvailableError) as raised:
                    self.backend().capture_input(left, right)
                self.assertIn("1-64", raised.exception.message)

    def test_the_default_pair_is_refused_on_a_mono_device(self):
        # Deliberate: a silent right channel would be a recording that looks
        # fine and is not.
        backend = self.backend(input_device="MacBook Air Microphone")

        with self.assertRaises(ChannelNotAvailableError):
            backend.capture_input(1, 2)
        self.assertEqual(backend.capture_input(1, 1).feeder.right, 0)

    def test_an_unknown_device_is_refused_with_what_was_found(self):
        with self.assertRaises(DeviceNotFoundError) as raised:
            self.backend(input_device="Focusrite").capture_input(1, 2)
        self.assertIn("macbook air microphone", raised.exception.message)

    def test_importing_it_loads_nothing_from_macos(self):
        # backends/__init__.py imports this module on Windows too, where the
        # frameworks it uses do not exist. In a process of its own, so that
        # the import really happens rather than being answered from memory.
        script = (
            "import ctypes.util\n"
            "def refuse(name): raise SystemExit('import looked for ' + name)\n"
            "ctypes.util.find_library = refuse\n"
            "import backends.audioqueue\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parent.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class PickStereoTest(unittest.TestCase):
    def test_picks_two_channels_out_of_many(self):
        # Every sample says where it came from: its channel, plus its frame.
        channels, frames = 64, 480
        interleaved = array.array(
            "f",
            (
                channel + frame / 1000
                for frame in range(frames)
                for channel in range(channels)
            ),
        )

        stereo = array.array("f", pick_stereo(interleaved.tobytes(), channels, 40, 6))

        self.assertEqual(len(stereo), 2 * frames)
        for frame in (0, 1, 250, frames - 1):
            self.assertAlmostEqual(stereo[2 * frame], 40 + frame / 1000, places=3)
            self.assertAlmostEqual(stereo[2 * frame + 1], 6 + frame / 1000, places=3)

    def test_reads_the_memory_a_capture_buffer_is_handed_over_as(self):
        samples = (ctypes.c_float * 6)(
            0.1, 0.2, 0.3, 1.1, 1.2, 1.3
        )  # 2 frames of 3 channels
        stereo = array.array("f", pick_stereo(samples, 3, 2, 0))
        self.assertEqual([round(value, 1) for value in stereo], [0.3, 0.1, 1.3, 1.1])

    def test_one_channel_can_feed_both_sides(self):
        mono = array.array("f", (0.0, 0.5, 1.0))
        stereo = array.array("f", pick_stereo(mono.tobytes(), 1, 0, 0))
        self.assertEqual(list(stereo), [0.0, 0.0, 0.5, 0.5, 1.0, 1.0])


class AudioQueueCaptureTest(unittest.TestCase):
    """The parts of the capture that need no audio device: its bookkeeping."""

    def capture(self):
        capture = AudioQueueCapture(DANTE, left=0, right=1, buffer_ms=2000)
        capture.label = "Room"
        return capture

    def piped(self, capture):
        """Give the capture a pipe to write into; returns the end to read from."""
        read_fd, write_fd = os.pipe()
        capture.sink = os.fdopen(write_fd, "wb")
        self.addCleanup(os.close, read_fd)
        return read_fd

    def test_what_is_queued_is_written_and_the_pipe_is_then_closed(self):
        capture = self.capture()
        read_fd = self.piped(capture)
        capture.keep(b"first", 1)
        capture.keep(b"second", 1)
        capture.capture_ended.set()

        capture.write_to_sink()

        self.assertEqual(os.read(read_fd, 100), b"firstsecond")
        self.assertEqual(
            os.read(read_fd, 100), b""
        )  # end of input: ffmpeg would finalise
        self.assertTrue(capture.sink.closed)

    def test_a_writer_that_fails_still_closes_the_pipe(self):
        # Otherwise ffmpeg waits for audio for ever and the room shows as
        # recording for the rest of the service.
        capture = self.capture()
        self.piped(capture)
        capture.keep(b"audio", 1)

        with (
            mock.patch.object(
                audioqueue, "write_all", side_effect=RuntimeError("boom")
            ),
            self.assertLogs("recording_api", level="ERROR"),
        ):
            capture.write_to_sink()

        self.assertTrue(capture.sink.closed)
        self.assertTrue(capture.winding_down.is_set())

    def test_a_reader_that_has_gone_ends_the_writer(self):
        capture = self.capture()
        read_fd, write_fd = os.pipe()
        capture.sink = os.fdopen(write_fd, "wb")
        os.close(read_fd)  # ffmpeg has exited
        capture.keep(b"audio", 1)

        with self.assertLogs("recording_api", level="WARNING") as logs:
            capture.write_to_sink()

        self.assertIn("ffmpeg stopped reading audio", logs.output[0])
        self.assertTrue(capture.sink.closed)

    def test_audio_is_dropped_not_hoarded_when_ffmpeg_falls_too_far_behind(self):
        capture = self.capture()
        room_for = capture.chunks.maxsize
        self.assertEqual(room_for, 300)  # 30 s of 100 ms chunks

        for _ in range(room_for):
            capture.keep(b"chunk", 4800)

        with self.assertLogs("recording_api", level="ERROR") as logs:
            capture.keep(b"chunk", 4800)
            capture.keep(b"chunk", 4800)
        self.assertEqual(len(logs.output), 1)  # said once, not per chunk
        self.assertIn("more than 30 s behind", logs.output[0])
        self.assertEqual(capture.chunks.qsize(), room_for)

        capture.chunks.get()  # ffmpeg reads again
        with self.assertLogs("recording_api", level="ERROR") as logs:
            capture.keep(b"chunk", 4800)
        self.assertIn("0.2 s of audio were lost", logs.output[0])

    def test_stop_before_start_does_nothing(self):
        self.capture().stop()  # must not touch a queue that was never created

    def test_gaps_between_buffers_are_reported_by_the_writer(self):
        capture = self.capture()
        self.piped(capture)
        capture.gaps.append(4800)  # a tenth of a second at 48 kHz
        capture.capture_ended.set()

        with self.assertLogs("recording_api", level="WARNING") as logs:
            capture.write_to_sink()

        self.assertIn("lost 100 ms of audio", logs.output[0])


class ErrorCodeTest(unittest.TestCase):
    def test_shown_as_its_four_characters_when_it_has_them(self):
        code = audioqueue.fourcc("!dev")
        self.assertEqual(audioqueue.describe_status(code), f"{code} ('!dev')")
        self.assertEqual(audioqueue.describe_status(-66680), "-66680")


@unittest.skipUnless(ON_MAC, "needs macOS")
class CoreAudioTest(unittest.TestCase):
    def test_lists_this_macs_input_devices(self):
        devices = audioqueue.list_input_devices()

        for device in devices:
            with self.subTest(device.name):
                self.assertTrue(device.name)
                self.assertTrue(device.uid)
                self.assertGreaterEqual(device.channels, 1)
                self.assertGreater(device.sample_rate, 0)

    def test_a_device_that_cannot_be_read_is_left_out_not_fatal(self):
        unreadable = audioqueue.CaptureError("no answer")
        with (
            mock.patch.object(audioqueue, "describe_device", side_effect=unreadable),
            self.assertLogs("recording_api", level="WARNING"),
        ):
            self.assertEqual(audioqueue.list_input_devices(), [])

    def test_a_device_that_does_not_exist_cannot_be_started(self):
        ghost = InputDevice(
            name="Ghost", uid="no-such-device", channels=2, sample_rate=48000.0
        )
        capture = AudioQueueCapture(ghost, left=0, right=1, buffer_ms=2000)
        read_fd, write_fd = os.pipe()
        sink = os.fdopen(write_fd, "wb")
        self.addCleanup(os.close, read_fd)
        self.addCleanup(sink.close)

        with self.assertRaises(audioqueue.CaptureError):
            capture.start(sink, "Room")

        self.assertFalse(sink.closed)  # still the caller's to deal with
        self.assertIsNone(capture.writer)  # nothing left running
        capture.stop()  # and stopping what never started is harmless


if __name__ == "__main__":
    unittest.main()
