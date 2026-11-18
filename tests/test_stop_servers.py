import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import stop_servers
from stop_servers import Server

# As lsof -Fpctn prints it: one field per line.
LSOF = """\
p631
crapportd
f16
tIPv6
n*:60592
p47858
cCode Helper (Plugin)
f31
tIPv4
n127.0.0.1:17015
p76682
cPython
f12
tIPv4
n127.0.0.1:8010
p412
cpython3.12
f6
tIPv4
n*:8080
f7
tIPv6
n[::1]:8080
"""

# netstat -ano on a German Windows, where the State column is translated.
NETSTAT = """\

Aktive Verbindungen

  Proto  Lokale Adresse         Remoteadresse          Status           PID
  TCP    0.0.0.0:135            0.0.0.0:0              ABHÖREN         1128
  TCP    127.0.0.1:8080         0.0.0.0:0              ABHÖREN         9120
  TCP    192.168.1.20:52311     20.199.120.85:443      HERGESTELLT     3764
  TCP    [::]:8080              [::]:0                 ABHÖREN         9120
  UDP    0.0.0.0:5353           *:*                                    4321
"""

TASKLIST = """\
"System Idle Process","0","Services","0","8 K"
"svchost.exe","1128","Services","0","12,316 K"
"python.exe","9120","Console","1","45,236 K"
"""


class FakeServer:
    """Answers on a port of this machine the way a recording API does.

    `sessions` is what it says is recording, or `status` its whole answer to
    GET /status, as bytes for one that is not JSON. Rooms stopped through POST
    /recording/stop are noted in `stopped`.
    """

    def __init__(self, sessions=None, status=None):
        self.sessions = dict(sessions or {})
        self.status = status
        self.stopped = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/status":
                    self.answer(404, {"detail": "Not Found"})
                elif fake.status is not None:
                    self.answer(200, fake.status)
                else:
                    self.answer(200, {"status": "online", "sessions": fake.sessions})

            def do_POST(self):
                length = int(self.headers["Content-Length"])
                room = json.loads(self.rfile.read(length))["room_name"]
                fake.stopped.append(room)
                fake.sessions.pop(room, None)
                self.answer(
                    200,
                    {
                        "message": f"recording stopped for {room}",
                        "file": f"recordings/{room}.mp3",
                    },
                )

            def answer(self, code, content):
                body = content if isinstance(content, bytes) else json.dumps(content)
                body = body if isinstance(body, bytes) else body.encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass  # one line per request, on the test output

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        # Polling often, so that close() does not hold a test up.
        threading.Thread(
            target=self.httpd.serve_forever, args=(0.02,), daemon=True
        ).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def on(platform, **outputs):
    """Pretend to be on that system, with its tools printing these outputs."""
    stack = contextlib.ExitStack()
    stack.enter_context(mock.patch.object(stop_servers.sys, "platform", platform))
    stack.enter_context(
        mock.patch.object(
            stop_servers, "output_of", lambda command: outputs[command[0]]
        )
    )
    return stack


class ListeningProcessesTest(unittest.TestCase):
    """Which processes listen where, read from the operating system's tools."""

    def test_lsof_output_is_read(self):
        self.assertEqual(
            stop_servers.listeners_from_lsof(LSOF),
            [
                (631, "rapportd", "[::]:60592"),
                (47858, "Code Helper (Plugin)", "127.0.0.1:17015"),
                (76682, "Python", "127.0.0.1:8010"),
                (412, "python3.12", "0.0.0.0:8080"),
                (412, "python3.12", "[::1]:8080"),
            ],
        )

    def test_netstat_output_is_read_whatever_the_language(self):
        names = stop_servers.names_from_tasklist(TASKLIST)
        self.assertEqual(names[9120], "python.exe")
        self.assertEqual(
            stop_servers.listeners_from_netstat(NETSTAT, names),
            [
                (1128, "svchost.exe", "0.0.0.0:135"),
                (9120, "python.exe", "127.0.0.1:8080"),
                (9120, "python.exe", "[::]:8080"),
            ],
        )

    def test_only_python_processes_count_on_macos(self):
        with on("darwin", lsof=LSOF):
            self.assertEqual(
                stop_servers.listening_python_processes(),
                [
                    (412, "http://127.0.0.1:8080"),
                    (412, "http://[::1]:8080"),
                    (76682, "http://127.0.0.1:8010"),
                ],
            )

    def test_only_python_processes_count_on_windows(self):
        with on("win32", netstat=NETSTAT, tasklist=TASKLIST):
            self.assertEqual(
                stop_servers.listening_python_processes(),
                [(9120, "http://127.0.0.1:8080"), (9120, "http://[::1]:8080")],
            )

    def test_output_that_cannot_be_read_is_an_error_not_an_empty_list(self):
        # Otherwise a Windows that words it differently would read as a
        # machine with no server running.
        with (
            on("win32", netstat="something else entirely", tasklist=TASKLIST),
            self.assertRaises(OSError),
        ):
            stop_servers.listening_python_processes()

    def test_a_listener_on_every_address_is_reached_on_this_machines_own(self):
        for address, url in (
            ("0.0.0.0:8080", "http://127.0.0.1:8080"),
            ("[::]:8080", "http://[::1]:8080"),
            ("192.168.1.20:8080", "http://192.168.1.20:8080"),
        ):
            with self.subTest(address):
                self.assertEqual(stop_servers.url_of(address), url)


class FindServersTest(unittest.TestCase):
    def fake(self, **what):
        server = FakeServer(**what)
        self.addCleanup(server.close)
        return server

    def test_a_recording_api_is_known_by_its_status(self):
        fake = self.fake(sessions={"Room": {"path": "recordings/Room/take.mp3"}})
        status = stop_servers.status_of(fake.url)
        self.assertEqual(list(status["sessions"]), ["Room"])

    def test_other_programs_are_not_taken_for_one(self):
        for answer in ({"sessions": "three"}, {"up": True}, [1, 2], b"<html>"):
            with self.subTest(answer):
                self.assertIsNone(stop_servers.status_of(self.fake(status=answer).url))

    def test_a_page_that_is_not_there_is_not_one(self):
        self.assertIsNone(stop_servers.status_of(self.fake().url + "/elsewhere"))

    def test_nor_is_something_that_does_not_speak_http(self):
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def answer_with_noise():
            connection, _ = listener.accept()
            with connection:
                connection.recv(1000)
                connection.sendall(b"\x00\x01 not http\r\n\r\n")

        threading.Thread(target=answer_with_noise, daemon=True).start()
        port = listener.getsockname()[1]
        self.assertIsNone(stop_servers.status_of(f"http://127.0.0.1:{port}"))

    def test_nor_is_a_port_nothing_listens_on(self):
        with socket.create_server(("127.0.0.1", 0)) as listener:
            port = listener.getsockname()[1]
        self.assertIsNone(stop_servers.status_of(f"http://127.0.0.1:{port}"))

    @unittest.skipUnless(
        sys.platform == "win32" or shutil.which("lsof"), "needs lsof or Windows"
    )
    def test_a_server_is_found_wherever_it_listens(self):
        # For real: this test process is a Python process listening on a port.
        fake = self.fake(sessions={"Room": {}})
        self.assertIn(
            Server(os.getpid(), fake.url, {"Room": {}}), stop_servers.find_servers()
        )


def spawn(test, code):
    """Run a line of Python as a process of its own, ended with the test."""
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)

    def end():
        process.kill()
        process.wait()
        process.stdout.close()

    test.addCleanup(end)
    return process


class StopScriptTest(unittest.TestCase):
    """stop_servers.py, with the servers to find and the agent stood in for.

    Never with the real ones: the tests must not stop a server that is
    recording a service on the machine they are run on.
    """

    def setUp(self):
        self.found = self.patch("find_servers")
        self.stop_service = self.patch("stop_service")
        self.patch("PORT_CLOSE_TIMEOUT_S", 0)
        self.fake = FakeServer(sessions={"Room": {}})
        self.addCleanup(self.fake.close)
        self.process = spawn(self, "import time; time.sleep(60)")
        self.server = Server(self.process.pid, self.fake.url, {"Room": {}})

    def patch(self, name, *replacement):
        patcher = mock.patch.object(stop_servers, name, *replacement)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def run_script(self, *arguments):
        output = io.StringIO()
        with (
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            code = stop_servers.main(list(arguments))
        return code, output.getvalue()

    def test_list_only_shows_them(self):
        self.found.return_value = [self.server]

        code, said = self.run_script("--list")

        self.assertEqual(code, 0)
        self.assertIn(f"{self.fake.url} (process {self.process.pid})", said)
        self.assertIn("recording Room", said)
        self.assertEqual(self.fake.stopped, [])
        self.stop_service.assert_not_called()
        self.assertIsNone(self.process.poll())  # still running

    def test_it_ends_the_recordings_then_stops_everything(self):
        self.found.side_effect = [[self.server], []]

        code, said = self.run_script("--force")

        self.assertEqual(code, 0)
        self.assertEqual(self.fake.stopped, ["Room"])
        self.assertIn("saved recordings/Room.mp3", said)
        self.stop_service.assert_called_once_with()
        self.process.wait(timeout=5)  # raises if it was left running

    def test_it_asks_before_ending_recordings(self):
        self.found.return_value = [self.server]

        with mock.patch("builtins.input", return_value="n"):
            code, said = self.run_script()

        self.assertEqual(code, 1)
        self.assertIn("Nothing changed", said)
        self.assertEqual(self.fake.stopped, [])
        self.stop_service.assert_not_called()
        self.assertIsNone(self.process.poll())

    def test_with_nobody_to_ask_nothing_is_stopped(self):
        self.found.return_value = [self.server]

        with mock.patch("builtins.input", side_effect=EOFError):
            code, _ = self.run_script()

        self.assertEqual(code, 1)
        self.assertEqual(self.fake.stopped, [])

    def test_an_idle_server_is_stopped_without_asking(self):
        idle = Server(self.process.pid, self.fake.url, {})
        self.found.side_effect = [[idle], []]

        with mock.patch("builtins.input", side_effect=AssertionError("asked")):
            code, said = self.run_script()

        self.assertEqual(code, 0)
        self.assertIn("Stopped.", said)
        self.process.wait(timeout=5)

    def test_the_agent_is_stopped_even_when_no_server_answers(self):
        # A server that fails at once is started again and again by launchd.
        self.found.return_value = []

        code, said = self.run_script()

        self.assertEqual(code, 0)
        self.assertIn("No recording API server is running", said)
        self.stop_service.assert_called_once_with()

    def test_a_server_that_is_still_there_is_an_error(self):
        self.found.return_value = [Server(self.process.pid, self.fake.url, {})]
        self.patch("stop_processes")

        code, said = self.run_script()

        self.assertEqual(code, 1)
        self.assertIn(f"still running: {self.fake.url}", said)

    def test_it_says_when_it_could_not_look(self):
        self.found.side_effect = OSError("no lsof here")

        code, said = self.run_script()

        self.assertEqual(code, 2)
        self.assertIn("no lsof here", said)

    @unittest.skipIf(sys.platform == "win32", "Windows only knows killing")
    def test_a_server_that_will_not_exit_is_killed_with_its_ffmpeg(self):
        stubborn = spawn(
            self,
            "import signal, subprocess, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(60)\n",
        )
        child = int(stubborn.stdout.readline())
        self.addCleanup(stop_servers.send, child, signal.SIGKILL)
        self.patch("EXIT_TIMEOUT_S", 0.5)

        with contextlib.redirect_stdout(io.StringIO()) as output:
            stop_servers.stop_processes([stubborn.pid])

        self.assertEqual(stubborn.wait(timeout=5), -signal.SIGKILL)
        self.assertIn("did not exit", output.getvalue())
        self.assertFalse(stop_servers.is_running(child))


class ServiceCommandsTest(unittest.TestCase):
    """What launchd and Windows are asked to do, which no test can let them do."""

    def commands(self, platform, call, exit_code=lambda command: 0):
        """Make `call` as on that system; returns the commands it ran."""
        ran = []

        def run(command, **options):
            ran.append(command)
            return mock.Mock(returncode=exit_code(command), stdout="")

        with (
            mock.patch.object(stop_servers.sys, "platform", platform),
            mock.patch.object(stop_servers.subprocess, "run", run),
            mock.patch.object(stop_servers.os, "getuid", lambda: 501, create=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            call()
        return ran

    def test_the_scheduled_task_is_ended(self):
        self.assertEqual(
            self.commands("win32", stop_servers.stop_service),
            [["schtasks", "/End", "/TN", stop_servers.TASK_NAME]],
        )

    def test_a_windows_server_is_killed_with_everything_under_it(self):
        self.assertEqual(
            self.commands("win32", lambda: stop_servers.stop_processes([42])),
            [["taskkill", "/PID", "42", "/T", "/F"]],
        )

    def test_the_agent_is_unloaded_and_waited_for(self):
        service = f"gui/501/{stop_servers.LAUNCHD_LABEL}"
        loaded = [True]

        def exit_code(command):
            if command[1] == "bootout":
                loaded[0] = False
            return 0 if loaded[0] or command[1] == "bootout" else 113

        self.assertEqual(
            self.commands("darwin", stop_servers.stop_service, exit_code),
            [
                ["launchctl", "print", service],
                ["launchctl", "bootout", service],
                ["launchctl", "print", service],  # gone: nothing to wait for
            ],
        )

    def test_an_agent_that_is_not_loaded_is_left_alone(self):
        service = f"gui/501/{stop_servers.LAUNCHD_LABEL}"
        self.assertEqual(
            self.commands("darwin", stop_servers.stop_service, lambda command: 113),
            [["launchctl", "print", service]],
        )


if __name__ == "__main__":
    unittest.main()
