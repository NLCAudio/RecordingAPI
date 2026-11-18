import os
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

import recording
from backends import CaptureInput
from recording import (
    FLOOR_LEVEL_DB,
    WORK_FOLDER,
    RecordingRequest,
    Session,
    SessionRegistry,
)
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
            # And the same place in the work folder, where it is written first.
            self.assertTrue((Path(folder) / WORK_FOLDER / "LR FOH" / "Sunday").is_dir())

    def test_a_name_is_taken_while_a_recording_under_it_is_in_progress(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch.object(recording, "datetime") as clock,
        ):
            clock.now.return_value = datetime(2026, 9, 8, 9, 30, 0)
            config = make_config(folder)
            request = RecordingRequest(room_name="Room", service_name="Test")

            first = Path(recording.prepare_output_path(config, request))
            recording.working_path_for(config.output_path, first).touch()
            second = Path(recording.prepare_output_path(config, request))

            self.assertEqual(first.name, "Room_Test_2026-09-08_09-30-00.mp3")
            self.assertEqual(second.name, "Room_Test_2026-09-08_09-30-00_2.mp3")


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

    def in_progress(self):
        return sorted((self.folder / WORK_FOLDER).rglob("*.mp3"))

    def recordings(self):
        """The finished recordings: what an outside job would see and copy."""
        return sorted(set(self.folder.rglob("*.mp3")) - set(self.in_progress()))

    def check_start_and_stop(self):
        session = self.start()
        time.sleep(2.5)

        status = self.registry.status()["sessions"]["Room"]
        self.assertTrue(status["recording"])
        self.assertGreater(status["audio_input_level_left"], FLOOR_LEVEL_DB)
        self.assertGreater(status["audio_input_level_right"], FLOOR_LEVEL_DB)

        # While it runs the file is only in the work folder, under the same
        # room and service, and nothing shows among the finished recordings.
        final = self.folder / "Room" / "Test" / Path(session.path).name
        self.assertEqual(status["path"], str(final))
        self.assertEqual(self.in_progress(), [Path(session.working_path)])
        self.assertEqual(
            Path(session.working_path),
            self.folder / WORK_FOLDER / "Room" / "Test" / final.name,
        )
        self.assertEqual(self.recordings(), [])

        stopped = self.registry.stop("Room")

        self.assertIs(stopped, session)
        self.assertEqual(self.registry.status()["sessions"], {})
        self.assertEqual(session.process.poll(), 0)
        # Stopped, it is in its place and gone from the work folder.
        self.assertEqual(self.recordings(), [final])
        self.assertEqual(self.in_progress(), [])
        self.assertEqual(session.path, str(final))
        self.assertTrue(is_finalised(final))
        self.assertGreater(final.stat().st_size, 1.5 * BYTES_PER_SECOND)

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
        self.assertEqual(len(self.in_progress()), 1)

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
        self.assertEqual(len(self.recordings()), 2)
        self.assertEqual(self.in_progress(), [])
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
        # It ended, if badly, so it is moved to its place like any other.
        self.assertTrue(wait_until(lambda: Path(session.path).exists()))
        self.assertEqual(self.recordings(), [Path(session.path)])
        self.assertEqual(self.in_progress(), [])
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
        self.assertEqual(self.recordings(), [])
        self.assertEqual(self.in_progress(), [])

    def test_a_device_that_will_not_open_leaves_nothing_behind(self):
        self.use(FeederBackend(FailingFeeder()))

        with self.assertRaises(OSError) as raised:
            self.start()

        self.assertIn("would not open", str(raised.exception))
        self.assertEqual(self.registry.status()["sessions"], {})
        # No empty file posing as a recording, in either place.
        self.assertEqual(self.recordings(), [])
        self.assertEqual(self.in_progress(), [])

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


def leave_behind(folder, relative, content=b"audio", age_s=3600):
    """Put a file in the work folder as if a recording had been cut short."""
    path = Path(folder) / WORK_FOLDER / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    written_at = time.time() - age_s
    os.utime(path, (written_at, written_at))
    return path


class FinishRecordingTest(unittest.TestCase):
    """Moving a recording to its place once ffmpeg has gone."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def session(self, content=b"audio"):
        working = leave_behind(self.folder, "Room/Test/take.mp3", content, age_s=0)
        return Session(
            service_name="Test",
            process=mock.Mock(),
            started_at=time.time(),
            path=str(self.folder / "Room" / "Test" / "take.mp3"),
            working_path=str(working),
        )

    def test_the_file_is_moved_to_its_place(self):
        session = self.session()
        recording.finish_recording("Room", session)

        self.assertEqual(Path(session.path).read_bytes(), b"audio")
        self.assertFalse(Path(session.working_path).exists())

    def test_it_only_happens_once(self):
        session = self.session()
        recording.finish_recording("Room", session)
        with mock.patch.object(recording, "move_into_place") as move:
            recording.finish_recording("Room", session)
        move.assert_not_called()

    def test_a_file_nothing_was_recorded_into_is_removed(self):
        session = self.session(content=b"")
        recording.finish_recording("Room", session)

        self.assertFalse(Path(session.working_path).exists())
        self.assertFalse(Path(session.path).exists())

    def test_it_waits_for_the_disk_syncer_to_let_go_of_the_file(self):
        # On Windows a file cannot be moved while anything holds it open.
        session = self.session()
        session.file_released.clear()
        released_after = 0.3
        timer = threading.Timer(released_after, session.file_released.set)
        timer.start()
        self.addCleanup(timer.cancel)

        began = time.monotonic()
        recording.finish_recording("Room", session)

        self.assertGreaterEqual(time.monotonic() - began, released_after - 0.05)
        self.assertTrue(session.ffmpeg_finished.is_set())  # the syncer was told
        self.assertTrue(Path(session.path).exists())

    def test_a_file_that_cannot_be_moved_stays_and_is_reported(self):
        session = self.session()
        refused = PermissionError("in use")
        with (
            mock.patch.object(recording, "move_into_place", side_effect=refused),
            self.assertLogs("recording_api", level="ERROR") as logs,
        ):
            recording.finish_recording("Room", session)

        self.assertIn("could not move the finished recording", logs.output[0])
        self.assertEqual(session.path, session.working_path)  # where it really is
        self.assertTrue(Path(session.working_path).exists())

    def test_a_refused_move_is_tried_again(self):
        # A virus scanner holding the file for a moment after ffmpeg closed it.
        working = leave_behind(self.folder, "Room/Test/take.mp3")
        final = self.folder / "Room" / "Test" / "take.mp3"
        attempts = []

        def replace(source, destination, really=os.replace):
            attempts.append(source)
            if len(attempts) < 3:
                raise PermissionError("in use")
            really(source, destination)

        with (
            mock.patch.object(recording.os, "replace", replace),
            mock.patch.object(recording.time, "sleep"),
        ):
            self.assertEqual(recording.move_into_place(working, final), final)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(final.exists())


class LeftoversTest(unittest.TestCase):
    """Recordings left in the work folder by a server or machine that went down."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_leftover_is_moved_to_where_it_belongs(self):
        leftover = leave_behind(self.folder, "LR FOH/Sunday/take.mp3")

        result = recording.finish_leftovers(self.folder)

        final = self.folder / "LR FOH" / "Sunday" / "take.mp3"
        self.assertEqual(result.moved, [final])
        self.assertEqual(final.read_bytes(), b"audio")
        self.assertFalse(leftover.exists())

    def test_a_file_still_being_written_is_left_alone(self):
        recording_now = leave_behind(self.folder, "Room/Test/take.mp3", age_s=1)

        result = recording.finish_leftovers(self.folder)

        self.assertEqual(result.moved, [])
        self.assertEqual(result.kept, [(recording_now, "still being written")])
        self.assertTrue(recording_now.exists())

    def test_what_the_server_is_recording_now_is_left_alone_however_quiet(self):
        # A recording whose device has gone silent writes nothing for a while.
        quiet = leave_behind(self.folder, "Room/Test/take.mp3")
        other = leave_behind(self.folder, "Other/Test/take.mp3")

        # Named through another spelling of the folder, where there is one
        # (on macOS the temporary folder is reached through a link).
        result = recording.finish_leftovers(
            self.folder, recording_now={quiet.resolve()}
        )

        self.assertEqual(result.moved, [self.folder / "Other" / "Test" / "take.mp3"])
        self.assertTrue(quiet.exists())
        self.assertFalse(other.exists())

    def test_an_empty_leftover_is_removed(self):
        empty = leave_behind(self.folder, "Room/Test/take.mp3", content=b"")

        result = recording.finish_leftovers(self.folder)

        self.assertEqual(result.removed, [empty])
        self.assertFalse(empty.exists())
        self.assertFalse((self.folder / "Room" / "Test" / "take.mp3").exists())

    def test_a_finished_recording_is_never_overwritten(self):
        final = self.folder / "Room" / "Test" / "take.mp3"
        final.parent.mkdir(parents=True)
        final.write_bytes(b"already here")
        leave_behind(self.folder, "Room/Test/take.mp3", content=b"left behind")

        result = recording.finish_leftovers(self.folder)

        numbered = final.with_name("take_2.mp3")
        self.assertEqual(result.moved, [numbered])
        self.assertEqual(final.read_bytes(), b"already here")
        self.assertEqual(numbered.read_bytes(), b"left behind")

    def test_only_recordings_are_touched(self):
        stray = leave_behind(self.folder, ".DS_Store")

        result = recording.finish_leftovers(self.folder)

        self.assertEqual((result.moved, result.removed, result.kept), ([], [], []))
        self.assertTrue(stray.exists())

    def test_no_work_folder_means_nothing_to_do(self):
        result = recording.finish_leftovers(self.folder)
        self.assertEqual((result.moved, result.removed, result.kept), ([], [], []))

    def test_start_up_moves_leftovers_and_clears_out_the_empty_folders(self):
        leave_behind(self.folder, "Room/Test/take.mp3")
        still_recording = leave_behind(self.folder, "Other/Test/take.mp3", age_s=1)

        with self.assertLogs("recording_api", level="WARNING") as logs:
            left = recording.recover_leftovers(self.folder, at_start_up=True)

        self.assertEqual(left, [(still_recording, "still being written")])
        self.assertTrue((self.folder / "Room" / "Test" / "take.mp3").exists())
        self.assertIn("moved a leftover recording into place", logs.output[0])
        self.assertIn("still being written", logs.output[1])
        work = self.folder / WORK_FOLDER
        self.assertFalse((work / "Room").exists())  # emptied, so removed
        self.assertTrue(still_recording.exists())  # and its folders with it


class LeftoverWatchTest(unittest.TestCase):
    """The check the server repeats for as long as it runs."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_later_look_moves_leftovers_but_removes_no_folders(self):
        # Later, a recording may be starting: it makes its folder a moment
        # before ffmpeg puts a file into it, so an empty folder must stay.
        leave_behind(self.folder, "Room/Test/take.mp3")
        starting = self.folder / WORK_FOLDER / "Other" / "Test"
        starting.mkdir(parents=True)

        with self.assertLogs("recording_api", level="WARNING"):
            recording.recover_leftovers(self.folder)

        self.assertTrue((self.folder / "Room" / "Test" / "take.mp3").exists())
        self.assertTrue(starting.is_dir())

    def test_what_has_to_stay_is_logged_once_not_at_every_check(self):
        leave_behind(self.folder, "Room/Test/take.mp3", age_s=1)

        with self.assertLogs("recording_api", level="WARNING"):
            left = recording.recover_leftovers(self.folder)
        with self.assertNoLogs("recording_api"):
            again = recording.recover_leftovers(self.folder, already_logged=left)

        self.assertEqual(again, left)

    def test_it_moves_what_no_session_is_recording_into(self):
        # Both files have been quiet for an hour; only one is a session's.
        quiet = leave_behind(self.folder, "Room/Test/take.mp3")
        leave_behind(self.folder, "Other/Test/take.mp3")
        registry = SessionRegistry()
        registry._sessions["Room"] = Session(
            service_name="Test",
            process=mock.Mock(),
            started_at=time.time(),
            path=str(self.folder / "Room" / "Test" / "take.mp3"),
            working_path=str(quiet),
        )
        stopped = threading.Event()
        self.addCleanup(stopped.set)

        with (
            mock.patch.object(recording, "LEFTOVER_CHECK_INTERVAL_S", 0.02),
            self.assertLogs("recording_api", level="WARNING"),
        ):
            watch = threading.Thread(
                target=recording.watch_for_leftovers,
                args=(registry, self.folder, stopped),
            )
            watch.start()
            moved = self.folder / "Other" / "Test" / "take.mp3"
            self.assertTrue(wait_until(moved.exists))
            stopped.set()
            watch.join(5)

        self.assertFalse(watch.is_alive())  # it ends when told to
        self.assertTrue(quiet.exists())

    def test_a_check_that_fails_does_not_end_the_watch(self):
        stopped = threading.Event()
        checks = []

        def check(*args, **kwargs):
            checks.append(args)
            if len(checks) == 1:
                raise RuntimeError("a bug")
            stopped.set()
            return []

        with (
            mock.patch.object(recording, "LEFTOVER_CHECK_INTERVAL_S", 0.01),
            mock.patch.object(recording, "recover_leftovers", check),
            self.assertLogs("recording_api", level="ERROR"),
        ):
            recording.watch_for_leftovers(SessionRegistry(), self.folder, stopped)

        self.assertEqual(len(checks), 2)


class RegistryPathsTest(unittest.TestCase):
    def test_it_names_the_files_being_recorded_into(self):
        registry = SessionRegistry()
        registry._sessions["Room"] = Session(
            service_name="Test",
            process=mock.Mock(),
            started_at=time.time(),
            path="out/Room/Test/take.mp3",
            working_path="out/.in-progress/Room/Test/take.mp3",
        )
        self.assertEqual(
            registry.working_paths(), {Path("out/.in-progress/Room/Test/take.mp3")}
        )


if __name__ == "__main__":
    unittest.main()
