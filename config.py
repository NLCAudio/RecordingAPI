"""
Configuration
=============

Everything config.yaml may contain: each setting's name, type and default. A
misspelled key or a value of the wrong type is reported when the file is read,
naming the field, rather than as a KeyError in the middle of a recording.

The YAML keys are camelCase and the Python attributes snake_case; the aliases
map between the two.
"""

import ipaddress
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_PATH = Path(__file__).parent / "config.yaml"
EXAMPLE_CONFIG_PATH = Path(__file__).parent / "config.example.yaml"
DEFAULT_LOG_PATH = Path(__file__).parent / "recording_api.log"


class Config(BaseModel):
    # populate_by_name lets tests build a Config with the snake_case names.
    # extra="forbid" makes a typo in an optional key an error instead of a
    # setting that silently never applies.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    # How the audio device is read: "auto" picks audioqueue on macOS and dshow
    # on Windows. "avfoundation" is ffmpeg's own macOS capture, kept only for
    # comparison because it loses audio. See backends/__init__.py.
    framework: str = "auto"
    # The audio device to record from. On Windows part of the name is enough,
    # so one setting covers every "DVS Receive N-M (Dante Virtual Soundcard)".
    input_device: str = Field(alias="inputDevice")
    # MP3 quality / file size trade-off, e.g. "128k".
    bitrate: str = "128k"
    # Milliseconds of audio the capture may hold before any is lost, which
    # rides out a busy moment. On Windows it is dshow's buffer; None leaves the
    # device default. On macOS it is the capture queue, never shorter than two
    # seconds, so a value chosen for Windows is safe in a config copied to the Mac.
    audio_buffer_ms: int | None = Field(None, alias="audioBufferMs")
    # Where recordings are written, under a folder per room and, inside it, per
    # service. Created on demand.
    output_path: Path = Field(alias="outputPath")
    log_path: Path = Field(DEFAULT_LOG_PATH, alias="logPath")
    # Every Companion instance the status is pushed to; a site can have more
    # than one control surface.
    companion_base_urls: list[str] = Field(alias="companionBaseUrls")
    # Where run_server.py listens. 127.0.0.1 only accepts requests from this
    # machine, which is enough while Companion runs alongside. "0.0.0.0" accepts
    # them from other machines too, which on Windows also needs an inbound
    # firewall rule for the port. The port is 8080 because Companion's own
    # default is 8000. Read once at start-up.
    listen_host: str = Field("127.0.0.1", alias="listenHost")
    listen_port: int = Field(8080, alias="listenPort", ge=1, le=65535)
    # The other machines that may use the API, as addresses (192.168.1.50) or
    # ranges (192.168.1.0/24). The API has no log-in, so once it listens on the
    # network this list decides who can start and stop recordings. Empty accepts
    # every address. This machine is always accepted (see allows()). Read once
    # at start-up.
    accept_requests_from: list[IPv4Network | IPv6Network] = Field(
        default_factory=list, alias="acceptRequestsFrom"
    )
    # This server's name when more than one reports to the same Companion (a Mac
    # and a Windows machine recording the same rooms, say). The status then goes
    # to the custom variable recording_status_<serverName>, so the servers do
    # not overwrite each other; unset, it is plain recording_status. Limited to
    # what a Companion variable name may contain.
    server_name: str | None = Field(
        None, alias="serverName", pattern=r"^[A-Za-z0-9_]+$"
    )
    # Status pushes per second. 15 Hz is one every ~67 ms, fast enough for a
    # live meter. Must be above zero: the push interval is derived from it.
    status_push_refresh_hz: float = Field(15, alias="statusPushRefreshHz", gt=0)

    @property
    def local_url(self) -> str:
        """The address this machine itself reaches the server on.

        For the restart and install scripts, so they follow listenHost and
        listenPort. Not simply listenHost: "0.0.0.0" means "every address" when
        listening and cannot be connected to, so it becomes the loopback address.
        """
        return local_url(self.listen_host, self.listen_port)

    def allows(self, client: str | None, server: str | None = None) -> bool:
        """Whether a request from the address `client` may use the API.

        `server` is the address the request arrived at. If both ends are the
        same address the request was made on this machine, which is always
        allowed: the restart scripts and a Companion alongside depend on it.
        """
        if not self.accept_requests_from:
            return True
        try:
            address = ipaddress.ip_address(client or "")
        except ValueError:
            # No readable address: with a list in force it is not let in.
            return False
        # A server listening on "::" sees IPv4 callers as ::ffff:192.168.1.50.
        if address.version == 6 and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_loopback or client == server:
            return True
        return any(address in network for network in self.accept_requests_from)

    @field_validator("accept_requests_from", mode="before")
    @classmethod
    def parse_accept_requests_from(cls, entries: object) -> object:
        if entries is None:
            return []  # the key written with nothing under it
        if isinstance(entries, str):
            raise ValueError(
                "expected a list of addresses, not a single one. Write it as:\n"
                f"acceptRequestsFrom:\n  - {entries}"
            )
        networks = []
        for entry in entries:
            try:
                # strict=False: "192.168.1.50/24" means the range that address
                # is in, not an error.
                networks.append(ipaddress.ip_network(str(entry).strip(), strict=False))
            except ValueError:
                # ipaddress's own message is cryptic, and a name instead of a
                # number is the likely slip.
                raise ValueError(
                    f"{entry!r} is not an address or a range. Write an address "
                    "such as 192.168.1.50 or a range such as 192.168.1.0/24; "
                    "machine names are not looked up."
                ) from None
        return networks

    @field_validator("companion_base_urls", mode="before")
    @classmethod
    def reject_single_url(cls, urls: object) -> object:
        # A bare address is a common thing to write, and pydantic would only
        # answer "input should be a valid list".
        if isinstance(urls, str):
            raise ValueError(
                "expected a list of addresses, not a single one. Write it as:\n"
                f"companionBaseUrls:\n  - {urls}"
            )
        return urls

    @field_validator("companion_base_urls")
    @classmethod
    def clean_urls(cls, urls: list[str]) -> list[str]:
        # Trailing slashes are stripped so URLs built from these never double them.
        cleaned = [url.strip().rstrip("/") for url in urls if url.strip()]
        if not cleaned:
            raise ValueError("expected at least one address")
        # Duplicates would only push the same value to the same panel twice.
        return list(dict.fromkeys(cleaned))


def local_url(host: str, port: int) -> str:
    """The URL this machine reaches a server listening on host:port at."""
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:  # an IPv6 address needs brackets inside a URL
        host = f"[{host}]"
    return f"http://{host}:{port}"


def load_config() -> Config:
    # Read per request, not cached, so the settings a recording uses (device,
    # framework, bitrate, buffer, output folder) apply to the next recording
    # without a restart. The rest are read once at start-up: listen address,
    # accepted addresses, Companion addresses, push rate, server name, log path.
    try:
        with open(CONFIG_PATH, "r") as file:
            return Config(**yaml.safe_load(file))
    except FileNotFoundError:
        # config.yaml is each machine's own and not in the repository, so a
        # fresh copy of the project does not have one yet.
        raise FileNotFoundError(
            f"{CONFIG_PATH.name} not found. Copy {EXAMPLE_CONFIG_PATH.name} to "
            f"{CONFIG_PATH.name} in {CONFIG_PATH.parent} and fill it in."
        ) from None
