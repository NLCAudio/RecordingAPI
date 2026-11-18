r"""
A check of a real audio device, end to end
==========================================

Records from the device through the same path the server uses, and fails if
the recording is shorter than the time it ran for, which is how lost audio
shows. Run it on a machine before trusting it with a service, and after any
change to its audio set-up.

It only runs when told which device to use:

    macOS:
        RECORDING_API_TEST_DEVICE="Dante Virtual Soundcard" \
            python -m unittest tests.test_device -v

    Windows PowerShell:
        $env:RECORDING_API_TEST_DEVICE = "Dante Virtual Soundcard"
        python -m unittest tests.test_device -v

Optional settings:

    RECORDING_API_TEST_CHANNELS    left and right channel, default "1,2"
    RECORDING_API_TEST_SECONDS     how long to record, default 20
    RECORDING_API_TEST_FRAMEWORK   the `framework` setting to test, default "auto"

The device need not be carrying sound: silence is recorded like anything else.
The file is made in a temporary folder and removed afterwards.
"""

import os
import tempfile
import time
import unittest
from pathlib import Path

from recording import RecordingRequest, SessionRegistry
from tests.support import FFMPEG, make_config

DEVICE = os.environ.get("RECORDING_API_TEST_DEVICE")
CHANNELS = os.environ.get("RECORDING_API_TEST_CHANNELS", "1,2")
SECONDS = float(os.environ.get("RECORDING_API_TEST_SECONDS", "20"))
FRAMEWORK = os.environ.get("RECORDING_API_TEST_FRAMEWORK", "auto")

# How much shorter than its running time a recording may be. Starting ffmpeg
# and opening the device takes a few tenths of a second, which is not loss.
ALLOWED_SHORTFALL_S = 1.0

# Words in a log line that mean audio did not make it into the file: the macOS
# capture's own reports, and ffmpeg's on Windows.
LOSS_WORDS = ("lost", "no audio has arrived", "behind", "dropped")


@unittest.skipUnless(
    DEVICE, "set RECORDING_API_TEST_DEVICE to a device name to run this"
)
@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealDeviceTest(unittest.TestCase):
    def test_records_for_as_long_as_it_ran_and_reports_no_loss(self):
        left, right = (int(channel) for channel in CHANNELS.split(","))
        registry = SessionRegistry()
        self.addCleanup(registry.stop_all)

        with tempfile.TemporaryDirectory() as folder:
            config = make_config(folder, input_device=DEVICE, framework=FRAMEWORK)
            request = RecordingRequest(
                room_name="device-check",
                service_name="test",
                left_input_channel=left,
                right_input_channel=right,
            )

            with self.assertLogs("recording_api", level="INFO") as logs:
                began = time.monotonic()
                session = registry.start(request, config)
                time.sleep(SECONDS)
                ran_for = time.monotonic() - began
                registry.stop("device-check")

            # Constant bitrate, so the file's size is its length.
            bits_per_second = int(config.bitrate.rstrip("k")) * 1000
            recorded = Path(session.path).stat().st_size * 8 / bits_per_second

        reports_of_loss = [
            line for line in logs.output if any(word in line for word in LOSS_WORDS)
        ]
        print(
            f"\n  device   : {DEVICE}, channels {left} and {right}"
            f"\n  framework: {FRAMEWORK}"
            f"\n  ran for  : {ran_for:.2f} s"
            f"\n  recorded : {recorded:.2f} s"
            f"\n  loss reported in the log: {reports_of_loss or 'none'}"
        )

        self.assertEqual(reports_of_loss, [])
        self.assertGreater(
            recorded,
            ran_for - ALLOWED_SHORTFALL_S,
            f"only {recorded:.2f} s of audio from {ran_for:.2f} s of recording",
        )


if __name__ == "__main__":
    unittest.main()
