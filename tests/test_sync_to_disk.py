"""The disk-sync thread: fsync cadence, open and flush failure handling."""

import recording
from conftest import make_session


def test_fsyncs_then_stops_when_ffmpeg_done(monkeypatch):
    session = make_session(code=0)
    session.ffmpeg_finished.set()  # ffmpeg already gone: one final flush
    seen = {"fsync": 0, "close": 0}

    monkeypatch.setattr(recording.os, "open", lambda path, flags: object())
    monkeypatch.setattr(
        recording.os, "fsync", lambda fd: seen.__setitem__("fsync", seen["fsync"] + 1)
    )
    monkeypatch.setattr(
        recording.os, "close", lambda fd: seen.__setitem__("close", seen["close"] + 1)
    )

    recording.sync_to_disk("sanctuary", session)

    assert seen == {"fsync": 1, "close": 1}


def test_open_failure_warns_and_recording_continues(monkeypatch, caplog):
    session = make_session(code=0)
    monkeypatch.setattr(recording.os, "open", lambda path, flags: (_ for _ in ()).throw(OSError("no such file")))

    recording.sync_to_disk("sanctuary", session)

    assert "cannot open" in caplog.text


def test_fsync_failure_warns_and_stops_syncing(monkeypatch, caplog):
    session = make_session(code=0)

    monkeypatch.setattr(recording.os, "open", lambda path, flags: object())
    monkeypatch.setattr(
        recording.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("bad handle"))
    )
    monkeypatch.setattr(recording.os, "close", lambda fd: None)

    recording.sync_to_disk("sanctuary", session)

    assert "cannot flush" in caplog.text