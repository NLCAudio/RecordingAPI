"""
Tests
=====

Run from the project folder with:

    python -m unittest

No audio device is needed. Tests that make real recordings have ffmpeg record
a generated tone and are skipped where ffmpeg is not installed. test_device.py
is the exception: it records from a real device, and only when asked to (see
the top of that file).
"""

import logging

# Without a handler, every warning the code under test logs on purpose would
# be printed across the test output.
logging.getLogger("recording_api").addHandler(logging.NullHandler())
