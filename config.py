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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Where finished recordings are written, under a folder per room and a
    # folder per service inside it. All of it created on demand.
    output_path: Path = Field(alias="outputPath")
    log_path: Path = Field(DEFAULT_LOG_PATH, alias="logPath")
    # Every Companion instance the status is pushed to. A list, because a site
    # can have more than one control surface and each needs its own copy.
    companion_base_urls: list[str] = Field(alias="companionBaseUrls")
    # How many times per second the status push runs. 15 Hz is an update every
    # ~67 ms, fast enough for a live meter. Must be above zero — the push
    # interval is derived from it.
    status_push_refresh_hz: float = Field(15, alias="statusPushRefreshHz", gt=0)

    @model_validator(mode="before")
    @classmethod
    def reject_renamed_url_key(cls, data: object) -> object:
        # companionBaseUrls used to be companionBaseUrl and hold a single
        # address, so a config.yaml written before that change — or copied over
        # from the other machine — arrives here. extra="forbid" would reject it
        # anyway, but only as "extra inputs are not permitted", which doesn't
        # say what to write instead. This is the one error that stops the server
        # from starting at all, so it is worth spelling out.
        if isinstance(data, dict) and "companionBaseUrl" in data:
            raise ValueError(
                "companionBaseUrl has been replaced by companionBaseUrls, which "
                "takes a list so the status can go to more than one Companion. "
                f"Write it as:\ncompanionBaseUrls:\n  - {data['companionBaseUrl']}"
            )
        return data

    @field_validator("companion_base_urls", mode="before")
    @classmethod
    def reject_single_url(cls, urls: object) -> object:
        # A list of one is still a list; a bare address is a common thing to
        # write and pydantic would only answer "input should be a valid list".
        if isinstance(urls, str):
            raise ValueError(
                "expected a list of addresses, not a single one. Write it as:\n"
                f"companionBaseUrls:\n  - {urls}"
            )
        return urls

    @field_validator("companion_base_urls")
    @classmethod
    def clean_urls(cls, urls: list[str]) -> list[str]:
        # Trailing slashes are stripped so the URLs built from these never end
        # up doubled, whether or not the config file happens to include one.
        cleaned = [url.strip().rstrip("/") for url in urls if url.strip()]
        if not cleaned:
            raise ValueError("expected at least one address")
        # Two identical entries would mean two threads pushing the same value to
        # the same panel, at twice the rate and to no effect.
        return list(dict.fromkeys(cleaned))


def load_config(path: Path | None = None) -> Config:
    """Read and validate config.yaml (or the given file, in tests).

    Called per request rather than cached, so edits to config.yaml take
    effect without restarting the server.

    Relative outputPath/logPath values are resolved against the directory
    the config file lives in, not the process's current directory. fastapi
    can be started from anywhere (the Windows Task Scheduler launches the
    server from System32), and a recording quietly written to the wrong
    folder is far worse than one that fails loudly.
    """
    path = Path(path) if path is not None else CONFIG_PATH
    with open(path, "r") as file:
        config = Config(**yaml.safe_load(file))

    base = path.resolve().parent
    updates: dict = {}
    if not config.output_path.is_absolute():
        updates["output_path"] = base / config.output_path
    # A log_path that defaults to DEFAULT_LOG_PATH is already absolute and
    # points next to the code; only an explicit relative one needs anchoring.
    if config.log_path != DEFAULT_LOG_PATH and not config.log_path.is_absolute():
        updates["log_path"] = base / config.log_path
    return config.model_copy(update=updates) if updates else config
