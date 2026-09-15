"""ffmpeg command construction and output-path preparation."""

from pathlib import Path

import pytest

from backends import CaptureInput
from config import Config
from conftest import make_config
from recording import (
    RecordingRequest,
    build_recording_cmd,
    prepare_output_path,
)


def test_build_recording_cmd_structure(config: Config):
    capture_input = CaptureInput(
        args=["-f", "avfoundation", "-thread_queue_size", "4096", "-i", ":0"],
        filter_prefix="[0:a]pan=stereo|c0=c0|c1=c1",
    )
    cmd = build_recording_cmd(config, capture_input, "/tmp/out.mp3")

    assert cmd[0] == "ffmpeg"
    assert cmd[1:3] == ["-hide_banner", "-nostats"]
    # the capture input's args are spliced in verbatim
    assert cmd[3:9] == capture_input.args
    filter_idx = cmd.index("-filter_complex")
    filter_graph = cmd[filter_idx + 1]
    assert filter_graph.startswith("[0:a]pan=stereo|c0=c0|c1=c1,")
    assert "astats=metadata=1" in filter_graph  # level metering attached
    assert "[out]" in filter_graph
    assert cmd[cmd.index("-map") + 1] == "[out]"
    assert cmd[cmd.index("-c:a") + 1] == "libmp3lame"
    assert cmd[cmd.index("-b:a") + 1] == "128k"
    assert cmd[cmd.index("-flush_packets") + 1] == "1"
    assert cmd[-1] == "/tmp/out.mp3"


def test_build_recording_cmd_uses_config_bitrate(config: Config):
    config = make_config(bitrate="192k")
    cmd = build_recording_cmd(
        config,
        CaptureInput(args=["-i", "x"], filter_prefix="[0:a]pan=stereo"),
        "o.mp3",
    )
    assert cmd[cmd.index("-b:a") + 1] == "192k"


def test_prepare_output_path_creates_nested_folders(tmp_path, request_body):
    config = make_config(output_path=tmp_path)
    out = prepare_output_path(config, request_body)
    assert (tmp_path / "sanctuary" / "9am").is_dir()
    name = Path(out).name
    assert name.startswith("sanctuary_9am_")
    assert name.endswith(".mp3")


def test_prepare_output_path_sanitises_names(tmp_path, caplog):
    config = make_config(output_path=tmp_path)
    request = RecordingRequest(room_name="a/b:c", service_name="..")
    out = prepare_output_path(config, request)
    assert (tmp_path / "a_b_c" / "service").is_dir()  # ".." fell back
    assert "a_b_c" in out
    assert caplog.records  # the substitution was logged


def test_prepare_output_path_unwritable_raises(tmp_path, monkeypatch, request_body):
    config = make_config(output_path=tmp_path)
    monkeypatch.setattr("recording.os.access", lambda *a, **k: False)
    with pytest.raises(OSError, match="not writable"):
        prepare_output_path(config, request_body)


def test_prepare_output_path_mkdir_failure_names_the_folder(
    tmp_path, monkeypatch, request_body
):
    config = make_config(output_path=tmp_path / "nested" / "deep")

    def boom(self, *args, **kwargs):
        raise PermissionError("nope")

    monkeypatch.setattr(Path, "mkdir", boom)
    with pytest.raises(OSError, match="could not create recording folder"):
        prepare_output_path(config, request_body)


def test_prepare_output_path_avoids_same_second_collisions(tmp_path, request_body):
    config = make_config(output_path=tmp_path)
    first = prepare_output_path(config, request_body)
    Path(first).write_bytes(b"x")
    second = prepare_output_path(config, request_body)
    assert second != first
    assert second.endswith("_2.mp3")