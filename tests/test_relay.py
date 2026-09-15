"""The stderr reader thread: level parsing, start confirmation, exit reaping."""

import io

import recording
from conftest import make_session
from recording import FLOOR_LEVEL_DB, SessionRegistry


def run_relay(registry, session, stderr_bytes: bytes) -> None:
    session.process.stderr = io.BytesIO(stderr_bytes)
    recording.relay_ffmpeg_output(registry, "sanctuary", session)


def test_parses_level_lines_and_confirms_start(monkeypatch):
    registry = SessionRegistry()
    session = make_session(code=0)
    registry._sessions["sanctuary"] = session
    captured = []
    monkeypatch.setattr(
        registry, "set_level", lambda room, s, ch, db: captured.append((ch, db))
    )

    run_relay(
        registry,
        session,
        (
            b"frame=  123 fps= 25 q=-1.0 Lsize=    1234kB "
            b"time=00:00:04.93 bitrate=2043.1kbits/s speed=1.01x\n"
            b"lavfi.astats.1.RMS_level=-18.4\n"  # instance numbers start at 1
            b"[ametadata] pts_time:4.96\n"
            b"lavfi.astats.2.RMS_level=-inf\n"
        ),
    )

    assert session.confirmed_recording is True
    assert session.exit_code == 0
    # instance 1 -> left channel, instance 2 -> right; -inf clamps to floor
    assert captured == [(0, -18.4), (1, FLOOR_LEVEL_DB)]
    # and once ffmpeg is gone the meter shows silence, not the last value
    assert session.left == FLOOR_LEVEL_DB


def test_started_marker_confirms_without_level_lines():
    registry = SessionRegistry()
    session = make_session(code=0)
    registry._sessions["sanctuary"] = session

    run_relay(
        registry, session, b"[out#0/mp3 @ 0x55555] Output #0, mp3, to 'x.mp3':\n"
    )

    assert session.confirmed_recording is True


def test_exit_records_code_and_last_message():
    registry = SessionRegistry()
    session = make_session(code=1)
    registry._sessions["sanctuary"] = session

    run_relay(registry, session, b"Device or resource busy\n")

    assert session.exit_code == 1
    assert session.last_message == "Device or resource busy"
    assert session.confirmed_recording is False
    assert session.ffmpeg_finished.is_set()


def test_nonzero_exit_logged_as_error(caplog):
    registry = SessionRegistry()
    session = make_session(code=1)
    registry._sessions["sanctuary"] = session

    run_relay(registry, session, b"")

    assert "[sanctuary] ffmpeg exited with code 1" in caplog.text