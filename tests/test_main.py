import asyncio
import tempfile
import time
import unittest
from unittest import mock

from starlette.requests import Request

import main
from backends import ChannelNotAvailableError, DeviceNotFoundError
from recording import RecordingRequest
from tests.support import make_config


class HttpErrorsTest(unittest.TestCase):
    """What a failed recording looks like to the button that asked for it."""

    def status_for(self, error):
        with (
            self.assertLogs("recording_api"),
            self.assertRaises(main.LoggedHTTPException) as raised,
            main.http_errors("Room"),
        ):
            raise error
        return raised.exception.status_code, raised.exception.detail

    def test_a_missing_device_is_the_requests_fault(self):
        status, detail = self.status_for(DeviceNotFoundError("dante", ["microphone"]))
        self.assertEqual(status, 422)
        self.assertIn("microphone", detail)

    def test_a_missing_channel_is_the_requests_fault(self):
        status, detail = self.status_for(
            ChannelNotAvailableError("Channel 99 is not offered")
        )
        self.assertEqual((status, detail), (422, "Channel 99 is not offered"))

    def test_a_machine_problem_is_reported_as_such(self):
        status, detail = self.status_for(OSError("recording folder is not writable"))
        self.assertEqual((status, detail), (400, "recording folder is not writable"))


class EndpointTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, replacement in (
            ("load_config", mock.Mock(return_value=make_config(folder.name))),
            ("registry", mock.Mock()),
        ):
            patcher = mock.patch.object(main, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_start_reports_a_room_that_is_already_recording(self):
        main.registry.start.return_value = None
        reply = main.start_recording(RecordingRequest(room_name="Room"))
        self.assertEqual(reply, {"message": "Room is already recording"})

    def test_stop_reports_the_file_that_was_recorded(self):
        main.registry.stop.return_value = mock.Mock(path="recordings/Room/x.mp3")
        reply = main.stop_recording(RecordingRequest(room_name="Room"))
        self.assertEqual(reply["file"], "recordings/Room/x.mp3")

    def test_stop_reports_a_room_that_was_not_recording(self):
        main.registry.stop.return_value = None
        reply = main.stop_recording(RecordingRequest(room_name="Room"))
        self.assertEqual(reply, {"message": "Room is not recording"})


def run_lifespan(test, config, registry=None):
    """Start the server up and shut it down again; returns what it logged."""
    if registry is None:
        registry = mock.Mock()
        registry.stop_all.return_value = []  # nothing was recording

    async def from_start_to_finish():
        async with main.lifespan(main.app):
            registry.stop_all.assert_not_called()  # still serving

    with (
        mock.patch.object(main, "load_config", return_value=config),
        mock.patch.object(main, "setup_logging"),
        mock.patch.object(main, "start_status_push"),
        mock.patch.object(main, "registry", registry),
        # Start-up notes its config in the module; put back what was there.
        mock.patch.object(main, "startup_config", None),
        test.assertLogs("recording_api") as logs,
    ):
        asyncio.run(from_start_to_finish())
    return logs.output


class ShutdownTest(unittest.TestCase):
    def test_recordings_are_stopped_when_the_server_shuts_down(self):
        registry = mock.Mock()
        registry.stop_all.return_value = ["Room"]

        logged = run_lifespan(self, make_config("out"), registry)

        registry.stop_all.assert_called_once_with()
        self.assertIn("stopped recording in Room", logged[-1])


class AccessTest(unittest.TestCase):
    """Who may use the API: decided in one place, for every endpoint."""

    THIS_SERVER = "192.168.1.20"
    COMPANION = "192.168.1.50"
    STRANGER = "192.168.1.77"

    def setUp(self):
        main.refusals_logged_at.clear()
        self.addCleanup(main.refusals_logged_at.clear)
        self.use(make_config("out", accept_requests_from=[self.COMPANION]))

    def use(self, config):
        patcher = mock.patch.object(main, "startup_config", config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, client, path="/recording/start"):
        """Send a request as if from `client`; returns "served" or the refusal."""
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "query_string": b"",
            "headers": [],
            "app": main.app,
            "client": (client, 50000) if client else None,
            "server": (self.THIS_SERVER, 8080),
        }

        async def endpoint(request):
            return "served"

        return asyncio.run(main.refuse_unlisted_addresses(Request(scope), endpoint))

    def test_a_listed_address_is_served(self):
        self.assertEqual(self.ask(self.COMPANION), "served")

    def test_this_machine_is_served(self):
        self.assertEqual(self.ask("127.0.0.1"), "served")
        self.assertEqual(self.ask(self.THIS_SERVER), "served")

    def test_any_other_address_is_refused_and_logged(self):
        with self.assertLogs("recording_api", level="WARNING") as logs:
            answer = self.ask(self.STRANGER)

        self.assertEqual(answer.status_code, 403)
        self.assertIn(b"not allowed", answer.body)
        self.assertIn(
            f"refused POST /recording/start from {self.STRANGER}", logs.output[0]
        )

    def test_a_caller_without_an_address_is_refused(self):
        with self.assertLogs("recording_api", level="WARNING"):
            self.assertEqual(self.ask(None).status_code, 403)

    def test_an_address_that_keeps_trying_is_logged_once_a_minute(self):
        with self.assertLogs("recording_api", level="WARNING") as logs:
            for _ in range(5):
                self.assertEqual(self.ask(self.STRANGER).status_code, 403)
            self.ask("192.168.1.78")  # a second stranger gets a line of its own
        self.assertEqual(len(logs.output), 2)

        with (
            mock.patch.object(
                main.time, "monotonic", return_value=time.monotonic() + 61
            ),
            self.assertLogs("recording_api", level="WARNING") as logs,
        ):
            self.ask(self.STRANGER)
        self.assertEqual(len(logs.output), 1)

    def test_without_a_list_everyone_is_served(self):
        self.use(make_config("out"))
        self.assertEqual(self.ask(self.STRANGER), "served")

    def test_start_up_says_who_is_accepted(self):
        logged = run_lifespan(
            self, make_config("out", accept_requests_from=[self.COMPANION])
        )
        self.assertTrue(any(f"and from {self.COMPANION}/32" in line for line in logged))

    def test_start_up_warns_when_the_network_can_reach_an_open_api(self):
        logged = run_lifespan(self, make_config("out", listen_host="0.0.0.0"))
        self.assertTrue(any("acceptRequestsFrom is empty" in line for line in logged))

    def test_start_up_does_not_warn_when_only_this_machine_can_reach_it(self):
        logged = run_lifespan(self, make_config("out"))  # listens on 127.0.0.1
        self.assertFalse(any("acceptRequestsFrom is empty" in line for line in logged))

    def test_which_listen_addresses_other_machines_can_reach(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host):
                self.assertFalse(main.accepts_other_machines(host))
        for host in ("0.0.0.0", "::", "192.168.1.20"):
            with self.subTest(host):
                self.assertTrue(main.accepts_other_machines(host))


if __name__ == "__main__":
    unittest.main()
