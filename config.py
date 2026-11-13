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

import ipaddress
from ipaddress import IPv4Network, IPv6Network
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_PATH = Path(__file__).parent / "config.yaml"
EXAMPLE_CONFIG_PATH = Path(__file__).parent / "config.example.yaml"
DEFAULT_LOG_PATH = Path(__file__).parent / "recording_api.log"


class Config(BaseModel):
    # populate_by_name lets us build a Config in tests with snake_case names
    # while the YAML file keeps its camelCase keys. extra="forbid" is what
    # makes a typo in an optional key an error rather than a setting that
    # silently never applies.
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    # How the audio device is read: "auto" picks audioqueue on macOS and dshow
    # on Windows. "avfoundation" is ffmpeg's own macOS capture, kept only for
    # comparison because it loses audio. See backends/__init__.py.
    framework: str = "auto"
    # The name of the audio device to record from. On Windows it is matched
    # against part of the name, so this one setting covers every
    # "DVS Receive N-M (Dante Virtual Soundcard)" pair.
    input_device: str = Field(alias="inputDevice")
    # MP3 quality / file size trade-off, e.g. "128k".
    bitrate: str = "128k"
    # How many milliseconds of audio the capture may hold before any is lost,
    # which is what rides out a moment where the machine is busy. On Windows it
    # is dshow's buffer, and None leaves it at the device default. On macOS it
    # is the length of the capture queue, which is never shorter than two
    # seconds whatever is set here — so a value chosen for Windows is safe in a
    # config that is copied to the Mac.
    audio_buffer_ms: int | None = Field(None, alias="audioBufferMs")
    # Where finished recordings are written, under a folder per room and a
    # folder per service inside it. All of it created on demand.
    output_path: Path = Field(alias="outputPath")
    log_path: Path = Field(DEFAULT_LOG_PATH, alias="logPath")
    # Every Companion instance the status is pushed to. A list, because a site
    # can have more than one control surface and each needs its own copy.
    companion_base_urls: list[str] = Field(alias="companionBaseUrls")
    # The address and port run_server.py listens on. 127.0.0.1 only accepts
    # requests from this machine, which is all that is needed while Companion
    # runs alongside it. "0.0.0.0" accepts them from other machines too — that
    # also needs an inbound rule for the port in Windows Defender Firewall.
    # Read once at start-up, so changing either needs a restart of the server.
    listen_host: str = Field("127.0.0.1", alias="listenHost")
    listen_port: int = Field(8000, alias="listenPort", ge=1, le=65535)
    # Which other machines may use the API, as addresses (192.168.1.50) or
    # ranges (192.168.1.0/24). The API has no log-in, so once it listens on
    # the network this list is what decides who can start and stop recordings.
    # Left empty, every address that can reach the port is accepted. Requests
    # from this machine itself are always accepted — see allows(). Read once at
    # start-up, like the listen address.
    accept_requests_from: list[IPv4Network | IPv6Network] = Field(
        default_factory=list, alias="acceptRequestsFrom"
    )
    # What this server calls itself when more than one reports to the same
    # Companion — a Mac and a Windows machine recording the same rooms, say. It
    # becomes part of the custom variable name, recording_status_<serverName>,
    # so each server has a variable of its own instead of both overwriting one.
    # Left unset, the variable is plain recording_status. Restricted to what a
    # Companion variable name may contain.
    server_name: str | None = Field(
        None, alias="serverName", pattern=r"^[A-Za-z0-9_]+$"
    )
    # How many times per second the status push runs. 15 Hz is an update every
    # ~67 ms, fast enough for a live meter. Must be above zero — the push
    # interval is derived from it.
    status_push_refresh_hz: float = Field(15, alias="statusPushRefreshHz", gt=0)

    @property
    def local_url(self) -> str:
        """The address this machine itself reaches the server on.

        Used by the restart and install scripts, so they follow listenHost and
        listenPort instead of carrying an address of their own. Not simply
        listenHost: "0.0.0.0" means "every address" when listening and is not
        somewhere to connect to, so that becomes the loopback address.
        """
        return local_url(self.listen_host, self.listen_port)

    def allows(self, client: str | None, server: str | None = None) -> bool:
        """Whether a request from the address `client` may use the API.

        `server` is the address the request arrived at. A request whose two
        ends are the same address was made on this machine, whatever address
        it used to reach itself, and this machine is always allowed: the
        restart scripts and a Companion running alongside depend on it.
        """
        if not self.accept_requests_from:
            return True
        try:
            address = ipaddress.ip_address(client or "")
        except ValueError:
            # No address, or not one we can read. With a list in force, what
            # cannot be identified is not let in.
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
                # strict=False so "192.168.1.50/24" means the range that
                # address is in, rather than being an error.
                networks.append(ipaddress.ip_network(str(entry).strip(), strict=False))
            except ValueError:
                # Raised afresh because ipaddress's own message would have to
                # be decoded, and a name instead of a number is the likely slip.
                raise ValueError(
                    f"{entry!r} is not an address or a range. Write an address "
                    "such as 192.168.1.50 or a range such as 192.168.1.0/24; "
                    "machine names are not looked up."
                ) from None
        return networks

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
    # Called per request rather than cached, so the settings a recording uses
    # (device, framework, bitrate, buffer, output folder) take effect on the
    # next recording without restarting the server. The rest are read once at
    # start-up: the listen address, who requests are accepted from, the
    # Companion addresses, the push rate, the server name and the log path.
    try:
        with open(CONFIG_PATH, "r") as file:
            return Config(**yaml.safe_load(file))
    except FileNotFoundError:
        # config.yaml is this machine's own and is not in the repository, so on
        # a fresh copy of the project it does not exist yet.
        raise FileNotFoundError(
            f"{CONFIG_PATH.name} not found. Copy {EXAMPLE_CONFIG_PATH.name} to "
            f"{CONFIG_PATH.name} in {CONFIG_PATH.parent} and fill it in."
        ) from None
