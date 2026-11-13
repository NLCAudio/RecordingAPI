import json
import threading
import time
import types
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
from urllib.parse import parse_qs, urlparse

import companion
from recording import SessionRegistry


class StopPushing(Exception):
    """Raised from the push loop's sleep, to end a loop that never ends itself."""


class StubCompanion:
    """A local HTTP server standing in for Companion: records what is pushed."""

    def __init__(self, status=200):
        self.pushes = []  # (path, decoded value) for every request received
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                url = urlparse(self.path)
                stub.pushes.append(
                    (url.path, json.loads(parse_qs(url.query)["value"][0]))
                )
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass  # keep the test output clean

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def push(base_url, server_name=None, times=1):
    """Run the push loop for a fixed number of pushes."""
    # The loop sleeps once after every push, so making the sleep raise on the
    # last one is what ends it. Only companion's own view of the time module is
    # replaced, so nothing else that sleeps meanwhile is affected.
    naps = mock.Mock(side_effect=[None] * (times - 1) + [StopPushing])
    clock = types.SimpleNamespace(sleep=naps, time=time.time)
    with mock.patch.object(companion, "time", clock):
        try:
            companion.push_status(SessionRegistry(), base_url, 0.01, server_name)
        except StopPushing:
            pass


class VariableNameTest(unittest.TestCase):
    def test_plain_without_a_server_name(self):
        self.assertEqual(companion.variable_name(None), "recording_status")

    def test_carries_the_server_name(self):
        self.assertEqual(companion.variable_name("mac"), "recording_status_mac")


class BuildStatusTest(unittest.TestCase):
    def test_adds_what_a_panel_watching_two_servers_needs(self):
        status = companion.build_status(SessionRegistry(), "mac")

        self.assertEqual(status["status"], "online")
        self.assertEqual(status["sessions"], {})
        self.assertEqual(status["server"], "mac")
        self.assertEqual(status["recording_count"], 0)
        self.assertAlmostEqual(status["pushed_at"], time.time(), delta=2)


class PushTest(unittest.TestCase):
    def setUp(self):
        self.companion = StubCompanion()
        self.addCleanup(self.companion.close)

    def test_pushes_to_the_plain_variable(self):
        push(self.companion.url)

        path, value = self.companion.pushes[0]
        self.assertEqual(path, "/api/custom-variable/recording_status/value")
        self.assertIsNone(value["server"])

    def test_pushes_to_the_named_servers_own_variable(self):
        push(self.companion.url, server_name="windows")

        path, value = self.companion.pushes[0]
        self.assertEqual(path, "/api/custom-variable/recording_status_windows/value")
        self.assertEqual(value["server"], "windows")

    def test_keeps_pushing(self):
        push(self.companion.url, times=3)
        self.assertEqual(len(self.companion.pushes), 3)


class FailingPushTest(unittest.TestCase):
    def test_a_refused_push_is_logged_once_not_every_time(self):
        # A custom variable that does not exist in Companion answers 404.
        refusing = StubCompanion(status=404)
        self.addCleanup(refusing.close)

        with self.assertLogs("recording_api", level="WARNING") as logs:
            push(refusing.url, times=3)

        self.assertEqual(len(refusing.pushes), 3)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("companion push failing", logs.output[0])

    def test_an_unreachable_companion_does_not_end_the_loop(self):
        with self.assertLogs("recording_api", level="WARNING") as logs:
            push("http://127.0.0.1:9", times=2)  # nothing listens on port 9

        self.assertEqual(len(logs.output), 1)


if __name__ == "__main__":
    unittest.main()
