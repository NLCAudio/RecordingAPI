"""Config parsing and validation: aliases, error messages, path resolution."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config import DEFAULT_LOG_PATH, Config, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def base(data: dict | None = None) -> dict:
    merged = {
        "input_device": "mic",
        "output_path": Path("out"),
        "companion_base_urls": ["http://127.0.0.1:8001"],
    }
    merged.update(data or {})
    return merged


def test_defaults():
    c = Config(**base())
    assert c.framework == "auto"
    assert c.bitrate == "128k"
    assert c.api_version == "0.0.1"
    assert c.status_push_refresh_hz == 15
    assert c.audio_buffer_ms is None


def test_camelcase_yaml_keys_are_read():
    c = Config(
        apiVersion="0.0.1",
        framework="dshow",
        inputDevice="DVS",
        bitrate="192k",
        audioBufferMs=200,
        outputPath="/tmp/out",
        logPath="/tmp/rec.log",
        companionBaseUrls=["http://one", "http://two"],
        statusPushRefreshHz=30,
    )
    assert c.input_device == "DVS"
    assert c.output_path == Path("/tmp/out")
    assert c.log_path == Path("/tmp/rec.log")
    assert c.audio_buffer_ms == 200
    assert c.status_push_refresh_hz == 30


def test_unknown_key_rejected():
    # A misspelled optional key must be an error, not a silent no-op.
    with pytest.raises(ValidationError):
        Config(**base({"outputPth": "/x"}))


def test_old_single_url_key_explains_the_rename():
    with pytest.raises(ValidationError, match="companionBaseUrl has been replaced"):
        Config(**base({"companionBaseUrl": "http://a"}))


def test_bare_url_string_rejected_with_hint():
    with pytest.raises(ValidationError, match="expected a list of addresses"):
        Config(**base({"companion_base_urls": "http://a"}))


def test_urls_cleaned_deduped_and_trimmed():
    c = Config(
        **base(
            {
                "companion_base_urls": [
                    "http://a/",
                    "  http://b  ",
                    "http://a",
                    "http://a/",
                    "",
                ]
            }
        )
    )
    assert c.companion_base_urls == ["http://a", "http://b"]


def test_at_least_one_url_required():
    with pytest.raises(ValidationError, match="at least one address"):
        Config(**base({"companion_base_urls": [""]}))


def test_refresh_hz_must_be_positive():
    with pytest.raises(ValidationError):
        Config(**base({"status_push_refresh_hz": 0}))
    with pytest.raises(ValidationError):
        Config(**base({"status_push_refresh_hz": -3}))


def _write_config(tmp_path: Path, **overrides) -> Path:
    data = {
        "inputDevice": "Dante Virtual Soundcard",
        "outputPath": "./recordings",
        "companionBaseUrls": ["http://127.0.0.1:8001"],
    }
    data.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_relative_paths_resolved_against_config_dir(tmp_path):
    cfg = _write_config(
        tmp_path, logPath="./logs/api.log", outputPath="./recs"
    )
    c = load_config(cfg)
    assert c.output_path == tmp_path / "recs"
    assert c.log_path == tmp_path / "logs" / "api.log"


def test_absolute_paths_left_alone(tmp_path):
    cfg = _write_config(tmp_path, outputPath="/abs/recs")
    c = load_config(cfg)
    assert c.output_path == Path("/abs/recs")
    # logPath unset -> default path next to the code, left untouched
    assert c.log_path == DEFAULT_LOG_PATH


def test_load_config_reads_the_repo_config(tmp_path):
    c = load_config(REPO_ROOT / "config.yaml")
    assert c.input_device == "Dante Virtual Soundcard"
    assert c.output_path.is_absolute()  # ./recordings anchored to repo root