import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

import recording
from backends import CaptureInput
from recording import FLOOR_LEVEL_DB, RecordingRequest, SessionRegistry
from tests.support import (
    FFMPEG,
    BrokenBackend,
    FailingFeeder,
    FeederBackend,
    ToneBackend,
    ToneFeeder,
    make_config,
)

# 128 kbit/s is 16,000 bytes of MP3 for every second of audio.
BYTES_PER_SECOND = 16_000


def wait_until(condition, timeout=5.0):
    """Poll until condition() is true; returns whether it became true in time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def is_finalised(path):
    """Whether ffmpeg closed the MP3 properly, which writes its duration header."""
    with open(path, "rb") as file:
        head = file.read(400)
    return b"Info" in head or b"Xing" in head


class NamesAndPathsTest(unittest.TestCase):
    def test_format_time(self):
        self.assertEqual(recording.format_time(0), "00:00:00")
        self.assertEqual(recording.format_time(3725), "01:02:05")

    def test_parse_db_clamps_what_is_not_a_level(self):
        self.assertEqual(recording.parse_db(b"-23.4"), -23.4)
        for not_a_level in (b"-inf", b"nan", b"-120.0", b"garbage"):
            with self.subTest(not_a_level):
                self.assertEqual(recording.parse_db(not_a_level), FLOOR_LEVEL_DB)

    def test_safe_name(self):
        cases = {
            "LR FOH": "LR FOH",
            "9am.": "9am",  # Windows drops a trailing dot
            "a/b\\c:d": "a_b_c_d",
            "../../etc": "_.._etc",  # cannot climb out of the output folder
            "..": "fallback",
            "   ": "fallback",
            "AUX": "_AUX",  # a device name Windows refuses
        }
        for raw, expected in cases.items():
            with self.subTest(raw):
                self.assertEqual(
                    recording.safe_name(raw, fallback="fallback"), expected
                )

    def test_free_path_numbers_a_name_that_is_taken(self):
        with tempfile.TemporaryDirectory() as folder:
            wanted = Path(folder) / "room_service.mp3"
            self.assertEqual(recording.free_path(wanted), wanted)

            wanted.touch()
            second = recording.free_path(wanted)
            self.assertEqual(second.name, "room_service_2.mp3")

            second.touch()
            self.assertEqual(recording.free_path(wanted).name, "room_service_3.mp3")

    def test_output_path_is_filed_by_room_and_service(self):
        with tempfile.TemporaryDirectory() as folder:
            request = RecordingRequest(room_name="LR FOH", service_name="Sunday")
            path = Path(recording.prepare_output_path(make_config(folder), request))

            self.assertEqual(path.parent, Path(folder) / "LR FOH" / "Sunday")
            self.assertTrue(path.parent.is_dir())
            self.assertRegex(
                path.name, r"^LR FOH_Sunday_\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d\.mp3$"
            )


class RequestTest(unittest.TestCase):
    def test_room_name_is_required(self):
        with self.assertRaises(ValidationError) as raised:
            RecordingRequest()
        self.assertIn("room_name", str(raised.exception))

    def test_channels_default_to_the_first_pair(self):
        request = RecordingRequest(room_name="A")
        self.assertEqual(
            (request.left_input_channel, request.right_input_channel), (1, 2)
        )


class CommandTest(unittest.TestCase):
    def test_every_frame_is_written_as_it_is_encoded(self):
        # What keeps a recording playable through a power cut: without
        # -flush_packets ffmpeg holds up to sixteen seconds back.
        command = recording.build_recording_cmd(
            make_config("out"),
            CaptureInput(args=["-f", "x", "-i", "device"], filter_prefix="[0:a]anull"),
            "out.mp3",
        )

        self.assertEqual(command[-3:], ["-flush_packets", "1", "out.mp3"])
        self.assertIn("libmp3lame", command)
        self.assertEqual(command[command.index("-i") + 1], "device")


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class LifecycleTest(unittest.TestCase):
    """Real recordings of a generated tone, through the same code a button uses."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.config = make_config(self.folder)
        self.registry = SessionRegistry()
        # Registered after the folder's cleanup, so it runs before it: nothing
        # is still writing into the folder when it is removed.
        self.addCleanup(self.registry.stop_all)

    def use(self, backend):
        patcher = mock.patch.object(recording, "get_backend", return_value=backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, room="Room"):
        return self.registry.start(
            RecordingRequest(room_name=room, service_name="Test"), self.config
        )

    def recordings(self):
        return list(self.folder.rglob("*.mp3"))

    def check_start_and_stop(self):
        session = self.start()
        time.sleep(2.5)

        status = self.registry.status()["sessions"]["Room"]
        self.assertTrue(status["recording"])
        self.assertGreater(status["audio_input_level_left"], FLOOR_LEVEL_DB)
        self.assertGreater(status["audio_input_level_right"], FLOOR_LEVEL_DB)

        stopped = self.registry.stop("Room")

        self.assertIs(stopped, session)
        self.assertEqual(self.registry.status()["sessions"], {})
        self.assertEqual(session.process.poll(), 0)
        self.assertTrue(is_finalised(session.path))
        self.assertGreater(Path(session.path).stat().st_size, 1.5 * BYTES_PER_SECOND)

    def test_start_and_stop_where_ffmpeg_reads_the_device(self):
        self.use(ToneBackend())
        self.check_start_and_stop()

    def test_start_and_stop_where_the_server_feeds_ffmpeg(self):
        backend = FeederBackend()
        self.use(backend)
        self.check_start_and_stop()

        # The feeder is told which room it records, for its log lines.
        self.assertEqual(backend.feeder.label, "Room")
        self.assertTrue(backend.feeder.stopped)

    def test_a_room_records_once(self):
        self.use(ToneBackend())
        self.assertIsNotNone(self.start())
        self.assertIsNone(self.start())  # already recording
        self.assertEqual(len(self.recordings()), 1)

    def test_stopping_an_idle_room_does_nothing(self):
        self.assertIsNone(self.registry.stop("Nobody"))

    def test_toggle_starts_then_stops(self):
        self.use(ToneBackend())
        request = RecordingRequest(room_name="Room", service_name="Test")

        started, session = self.registry.toggle(request, self.config)
        self.assertTrue(started)
        self.assertIn("Room", self.registry.status()["sessions"])

        started, stopped = self.registry.toggle(request, self.config)
        self.assertFalse(started)
        self.assertIs(stopped, session)
        self.assertEqual(self.registry.status()["sessions"], {})

    def test_stop_all_stops_every_room(self):
        self.use(ToneBackend())
        self.start("A")
        self.start("B")

        self.assertEqual(sorted(self.registry.stop_all()), ["A", "B"])
        self.assertEqual(self.registry.status()["sessions"], {})
        self.assertTrue(all(is_finalised(path) for path in self.recordings()))

    def test_a_killed_recording_is_still_a_usable_file(self):
        # A power cut, as far as the file is concerned: ffmpeg gets no chance
        # to finish it. Two seconds in, nearly all of it must already be there.
        self.use(ToneBackend())
        session = self.start()
        time.sleep(2.5)

        session.process.kill()

        self.assertTrue(
            wait_until(lambda: "Room" not in self.registry.status()["sessions"])
        )
        self.assertGreater(Path(session.path).stat().st_size, 1.5 * BYTES_PER_SECOND)

    def test_the_feeder_is_released_when_ffmpeg_dies(self):
        backend = FeederBackend()
        self.use(backend)
        session = self.start()

        session.process.kill()

        self.assertTrue(wait_until(lambda: backend.feeder.stopped))
        self.assertTrue(
            wait_until(lambda: "Room" not in self.registry.status()["sessions"])
        )

    def test_a_start_that_fails_says_so_and_leaves_the_room_idle(self):
        self.use(BrokenBackend())

        with self.assertRaises(OSError) as raised:
            self.start()

        self.assertIn("ffmpeg exited with code", str(raised.exception))
        self.assertEqual(self.registry.status()["sessions"], {})

    def test_a_device_that_will_not_open_leaves_nothing_behind(self):
        self.use(FeederBackend(FailingFeeder()))

        with self.assertRaises(OSError) as raised:
            self.start()

        self.assertIn("would not open", str(raised.exception))
        self.assertEqual(self.registry.status()["sessions"], {})
        self.assertEqual(self.recordings(), [])  # no empty file posing as a recording

    def test_a_feeder_that_fails_to_stop_does_not_hide_a_failed_start(self):
        # ffmpeg exits at once; releasing the device then blows up. The start
        # must still be reported as failed, not left to time out as "running".
        feeder = ToneFeeder()
        feeder.stop = mock.Mock(side_effect=RuntimeError("stop failed"))
        self.addCleanup(lambda: feeder.sink.close())
        backend = BrokenBackend()
        broken_input = backend.capture_input(1, 2)
        backend.capture_input = lambda left, right: CaptureInput(
            args=broken_input.args,
            filter_prefix=broken_input.filter_prefix,
            feeder=feeder,
        )
        self.use(backend)

        began = time.monotonic()
        with self.assertRaises(OSError):
            self.start()

        self.assertLess(time.monotonic() - began, recording.STARTUP_TIMEOUT_S)


if __name__ == "__main__":
    unittest.main()
