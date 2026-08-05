"""
Configuration
=============

Everything in config.yaml, described in one place: what each setting is called,
what type it holds, and what happens if it is left out. A misspelled key or a
value of the wrong type is reported when the file is read, naming the field,
instead of surfacing as a KeyError somewhere in the middle of a recording.

The YAML keys are camelCase and the Python attributes are snake_case; the
aliases below map between the two.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_PATH = Path(__file__).parent / "config.yaml"
DEFAULT_LOG_PATH = Path(__file__).parent / "recording_api.log"


class Config(BaseModel):
    # populate_by_name lets us build a Config in tests with snake_case names
    # while the YAML file keeps its camelCase keys. extra="forbid" is what
    # makes a typo in an optional key an error rather than a setting that
    # silently never applies.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    api_version: str = Field("0.0.1", alias="apiVersion")
    # Which ffmpeg capture framework to use: "auto" picks avfoundation on macOS
    # and dshow on Windows. See backends/__init__.py.
    framework: str = "auto"
    # Matched against the device names ffmpeg reports, so on Windows this one
    # name matches every "DVS Receive N-M (Dante Virtual Soundcard)" pair.
    input_device: str = Field(alias="inputDevice")
    # MP3 quality / file size trade-off, e.g. "128k".
    bitrate: str = "128k"
    # Windows only: how many milliseconds dshow buffers before handing audio to
    # ffmpeg. None leaves it at the device default.
    audio_buffer_ms: int | None = Field(None, alias="audioBufferMs")
    # Where finished recordings are written. Created on demand.
    output_path: Path = Field(alias="outputPath")
    log_path: Path = Field(DEFAULT_LOG_PATH, alias="logPath")
    companion_base_url: str = Field(alias="companionBaseUrl")
    # How many times per second the status push runs. 15 Hz is an update every
    # ~67 ms, fast enough for a live meter.
    status_push_refresh_hz: float = Field(15, alias="statusPushRefreshHz")

    @field_validator("companion_base_url")
    @classmethod
    def strip_trailing_slash(cls, url: str) -> str:
        # So the URLs built from this never end up with a doubled slash,
        # whether or not the config file happens to include one.
        return url.rstrip("/")


def load_config() -> Config:
    # Called per request rather than cached, so edits to config.yaml take
    # effect without restarting the server.
    with open(CONFIG_PATH, "r") as file:
        return Config(**yaml.safe_load(file))
