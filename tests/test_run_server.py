import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

import run_server


class LogConfigTest(unittest.TestCase):
    def handlers_of_every_logger(self, on_terminal):
        settings = run_server.log_config(on_terminal)
        loggers = [settings["root"], *settings["loggers"].values()]
        return {tuple(logger["handlers"]) for logger in loggers}

    def test_in_the_background_everything_goes_to_the_file(self):
        self.assertEqual(self.handlers_of_every_logger(False), {("file",)})

    def test_started_from_a_terminal_it_is_shown_there_too(self):
        self.assertEqual(self.handlers_of_every_logger(True), {("file", "terminal")})

    def test_the_file_is_in_the_logs_folder(self):
        file = run_server.log_config(False)["handlers"]["file"]
        self.assertEqual(Path(file["filename"]).parent.name, "logs")


class OutputTest(unittest.TestCase):
    """What the server prints where there is no console to show it."""

    def test_no_console_goes_nowhere_and_a_file_goes_somewhere(self):
        self.assertTrue(run_server.goes_nowhere(None))  # pythonw.exe
        with tempfile.TemporaryFile("w") as file:
            self.assertFalse(run_server.goes_nowhere(file))

    @unittest.skipUnless(os.name == "posix", "launchd is on macOS")
    def test_the_null_device_goes_nowhere(self):
        with open(os.devnull, "w") as nowhere:
            self.assertTrue(run_server.goes_nowhere(nowhere))

    def kept_from(self, script, **streams):
        """Run a script that calls keep(); returns the file's text, and the run."""
        with tempfile.TemporaryDirectory() as folder:
            kept = Path(folder) / "console.log"
            script = (
                "import os, sys, run_server\n"
                "from pathlib import Path\n"
                f"keep = lambda: run_server.keep_output(Path({str(kept)!r}))\n"
            ) + script
            done = subprocess.run(
                [sys.executable, "-c", script],
                cwd=run_server.PROJECT_DIR,
                check=False,
                **streams,
            )
            return (kept.read_text() if kept.exists() else None), done

    def test_without_a_console_what_is_printed_is_kept(self):
        # As under pythonw.exe: there is no stream to print to at all.
        kept, _ = self.kept_from(
            "sys.stdout = sys.stderr = None\n"
            "keep()\n"
            "print('printed')\n"
            "raise RuntimeError('an error in the code')\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self.assertIn("printed", kept)
        self.assertIn("RuntimeError: an error in the code", kept)

    @unittest.skipUnless(os.name == "posix", "launchd is on macOS")
    def test_under_launchd_what_is_printed_is_kept(self):
        # launchd hands its agents the null device. A library that writes to
        # the file descriptor directly, as Apple's do, is kept as well.
        kept, _ = self.kept_from(
            "keep()\n"
            "print('printed', flush=True)\n"
            "os.write(2, b'from a library\\n')\n"
            "raise RuntimeError('an error in the code')\n",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for expected in ("printed", "from a library", "an error in the code"):
            self.assertIn(expected, kept)

    def test_what_someone_can_see_is_left_where_it_goes(self):
        kept, done = self.kept_from(
            "keep()\nraise RuntimeError('an error in the code')\n",
            capture_output=True,
            text=True,
        )

        self.assertIsNone(kept)
        self.assertIn("RuntimeError: an error in the code", done.stderr)


class StartTest(unittest.TestCase):
    """main(), in a process and a project folder of its own."""

    def start(self, **settings):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        settings = {
            "inputDevice": "test device",
            "outputPath": "./recordings",
            "companionBaseUrls": ["http://127.0.0.1:9"],
            **settings,
        }
        (self.folder / "config.yaml").write_text(yaml.safe_dump(settings))
        script = (
            "import faulthandler\n"
            "from pathlib import Path\n"
            "import config, run_server\n"
            f"folder = Path({str(self.folder)!r})\n"
            "run_server.PROJECT_DIR = folder\n"
            "run_server.LOG_FOLDER = folder / 'logs'\n"
            "config.CONFIG_PATH = folder / 'config.yaml'\n"
            "try:\n"
            "    run_server.main()\n"
            "finally:\n"
            "    print('crashes are reported:', faulthandler.is_enabled())\n"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=run_server.PROJECT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )

    def server_log(self):
        return (self.folder / "logs" / "server.log").read_text()

    def test_an_unusable_config_is_reported_and_nothing_is_started(self):
        done = self.start(listenPortt=8080)

        self.assertEqual(done.returncode, 1)
        self.assertIn(
            "the server was not started: config.yaml cannot be used: listenPortt",
            self.server_log(),
        )
        self.assertIn("crashes are reported: True", done.stdout)

    def test_a_port_that_is_taken_is_reported_with_what_to_do(self):
        with socket.create_server(("127.0.0.1", 0)) as taken:
            done = self.start(listenPort=taken.getsockname()[1])

        self.assertEqual(done.returncode, 1)
        self.assertIn("is already in use", self.server_log())
        self.assertIn("stop_servers.py --list", self.server_log())

    def test_a_port_in_use_is_told_from_a_free_one(self):
        with socket.create_server(("127.0.0.1", 0)) as taken:
            port = taken.getsockname()[1]
            self.assertTrue(run_server.port_in_use("127.0.0.1", port))
        self.assertFalse(run_server.port_in_use("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
