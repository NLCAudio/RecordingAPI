"""
Tests
=====

Run from the project folder with:

    python -m unittest

Nothing here needs an audio device. The tests that make real recordings use
ffmpeg with a generated tone and are skipped where ffmpeg is not installed.
test_device.py is the exception: it records from a real device, and only runs
when asked to — see the top of that file.
"""

import logging

# The code under test logs through "recording_api". Without a handler, every
# warning it logs on purpose would be printed across the test output.
logging.getLogger("recording_api").addHandler(logging.NullHandler())
