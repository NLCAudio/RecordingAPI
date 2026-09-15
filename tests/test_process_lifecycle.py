"""start/stop of the ffmpeg process: confirmation, graceful stop, fallbacks."""

import signal
import subprocess
import sys

import pytest

import recording
from conftest import FakeProcess, make_session


def test_confirm_started_returns_when_confirmed():
    session = make_session()
    session.confirmed_recording = True
    session.startup_settled.set()
    recording.confirm_started("sanctuary", session)  # must not raise


def test_confirm_started_timeout_treats_as_running(monkeypatch, caplog):
    # ffmpeg neither confirmed nor exited: report it as running with a note.
    monkeypatch.setattr(recording, "STARTUP_TIMEOUT_S", 0.05)
    session = make_session()  # event never gets set
    recording.confirm_started("sanctuary", session)
    assert "has not confirmed" in caplog.text


def test_confirm_started_raises_with_ffmpeg_reason():
    session = make_session(code=1)
    session.startup_settled.set()  # reader thread reached end of output
    session.exit_code = 1
    session.last_message = "Dante Virtual Soundcard: Device or resource busy"
    with pytest.raises(OSError, match="Device or resource busy"):
        recording.confirm_started("sanctuary", session)


def test_stop_sends_q_and_waits():
    process = FakeProcess(code=0)
    recording.stop_ffmpeg(process)
    assert process.stdin.writes == [b"q"]
    assert process.was_killed is False


def test_stop_falls_back_to_sigint_on_closed_pipe():
    process = FakeProcess(code=0, stdin_failure=BrokenPipeError("closed"))
    recording.stop_ffmpeg(process)
    assert process.sent_signals == [signal.SIGINT]
    assert process.was_killed is False


def test_stop_terminates_when_stdin_closed_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    process = FakeProcess(code=0, stdin_failure=BrokenPipeError("closed"))
    recording.stop_ffmpeg(process)
    assert process.was_terminated is True


def test_stop_kills_when_wait_times_out():
    process = FakeProcess(
        code=0, wait_raises=subprocess.TimeoutExpired("ffmpeg", 10)
    )
    recording.stop_ffmpeg(process)
    assert process.was_killed is True