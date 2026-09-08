"""
Recording sessions
==================

Owns everything about recordings that are currently running: the ffmpeg
processes, the live audio levels read back from them, and the bookkeeping that
keeps the two in step.

All of that state lives inside SessionRegistry, which holds the locks that
protect it. Callers never take a lock themselves — they call a registry method
and get a Session back, so it is not possible to read the state without
holding the right lock.
"""

import dataclasses
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from backends import CaptureInput, get_backend
from config import Config

# Audio levels below this dB value are treated as silence / no signal.
FLOOR_LEVEL_DB = -60.0

# The per-second level lines the astats filter writes to stderr, e.g.
# "lavfi.astats.1.RMS_level=-23.4". Channel numbering starts at 1.
LEVEL_LINE = re.compile(rb"lavfi\.astats\.(\d)\.RMS_level=(-?(\d+\.\d+|inf|nan))")

# Measures the RMS level (volume) of each channel once per second and prints it
# to stderr, where relay_ffmpeg_output() picks it up to drive the live meter.
LEVEL_METERING_FILTER = (
    "astats=metadata=1:reset=1:measure_perchannel=RMS_level:measure_overall=none,"
    "ametadata=mode=print"
)

# Hides the console window ffmpeg would otherwise pop up on Windows. The
# constant only exists there, so fall back to 0 (no flags) everywhere else.
NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How long to wait for ffmpeg to finalise the file before force-killing it.
STOP_TIMEOUT_S = 10

# Lines ffmpeg prints only once every input and output is open and the encode
# has begun. It prints neither if it gives up on the device, the encoder options
# or the output file — verified against ffmpeg 8.1 for each of those failures —
# which is what makes them proof that a recording really started rather than a
# guess based on elapsed time. A per-second level line means the same thing and
# more (audio has reached the encoder), so it counts too, in relay_ffmpeg_output.
# Two spellings because one wording change in a future ffmpeg must not leave
# every start unconfirmed.
STARTED_MARKER = re.compile(rb"Press \[q\] to stop|Output #\d+,")

# The backstop for an ffmpeg that neither confirms nor exits — a device that
# hangs on open, say. Only reached when something is already wrong; a healthy
# start confirms in well under half a second.
STARTUP_TIMEOUT_S = 5.0

# Characters that cannot appear in a folder or file name on Windows. The set
# also covers both path separators, which is what keeps a room name like
# "../../etc" from writing outside outputPath.
INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Names Windows refuses whatever the extension, left over from DOS devices.
RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

logger = logging.getLogger("recording_api")


def format_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# What one recording needs to know about itself. This doubles as the JSON body
# callers send to the start/stop/toggle endpoints, which FastAPI validates for
# us. Channels are numbered the way Dante Controller numbers them, from 1.
class RecordingRequest(BaseModel):
    room_name: str = "room_name"
    service_name: str = "service_name"
    left_input_channel: int = 1  # which input channel maps to the left side
    right_input_channel: int = 2  # which input channel maps to the right side


@dataclasses.dataclass
class Session:
    """One recording that is currently running."""

    service_name: str
    process: subprocess.Popen  # the running ffmpeg process
    started_at: float  # Unix timestamp so we can compute elapsed time
    path: str  # where the MP3 file is being written
    # Live level per output channel, index 0 = left, 1 = right. Kept as a list
    # so the stderr reader can write to it by channel number without branching.
    levels: list[float] = dataclasses.field(
        default_factory=lambda: [FLOOR_LEVEL_DB, FLOOR_LEVEL_DB]
    )
    # How the start turned out. All four are written by the one stderr reader
    # thread and read by confirm_started() once the event says they are final,
    # so they need no lock of their own.
    startup_settled: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    confirmed_recording: bool = False  # ffmpeg said it is encoding
    exit_code: int | None = None  # set instead, if it exited first
    last_message: str = ""  # its last stderr line, which says why

    @property
    def is_running(self) -> bool:
        # poll() returns None while the process is still running.
        return self.process.poll() is None

    @property
    def left(self) -> float:
        return self.levels[0]

    @property
    def right(self) -> float:
        return self.levels[1]

    def silence_levels(self) -> None:
        # Used when ffmpeg exits, so the meter shows silence rather than the
        # last captured value, which would be misleading.
        self.levels = [FLOOR_LEVEL_DB] * len(self.levels)

    def status(self) -> dict:
        elapsed_s = int(time.time() - self.started_at)
        return {
            "service_name": self.service_name,
            "recording": True,
            "elapsed_s": elapsed_s,
            "elapsed_str": format_time(elapsed_s),
            "path": self.path,
            "audio_input_level_left": self.left,
            "audio_input_level_right": self.right,
        }


class SessionRegistry:
    """The set of rooms currently recording, and the locks guarding it.

    Two locks with one job each:

    - _state_lock guards the dictionary itself and is only ever held for
      dictionary operations. It is never held across starting or stopping
      ffmpeg, so spawning a recording in one room cannot stall the status push
      thread reading levels for another.
    - _room_locks serialise the slow work per room, so a start and a stop for
      the same room cannot interleave. Without it, two toggles arriving
      together could both decide the room was idle and both start a recording.
      They are reentrant, so toggle() can hold one while calling start() or
      stop(), which take the same lock.
    """

    def __init__(self):
        self._state_lock = threading.Lock()
        self._room_locks: dict[str, threading.RLock] = {}
        self._sessions: dict[str, Session] = {}

    def _room_lock(self, room_name: str) -> threading.RLock:
        with self._state_lock:
            return self._room_locks.setdefault(room_name, threading.RLock())

    def _live_session(self, room_name: str) -> Session | None:
        """The room's session if it is still recording, forgetting it if not."""
        with self._state_lock:
            session = self._sessions.get(room_name)
            if session is not None and not session.is_running:
                # ffmpeg exited on its own — drop it so the room reads as idle.
                del self._sessions[room_name]
                return None
            return session

    def start(self, request: RecordingRequest, config: Config) -> Session | None:
        """Start recording a room. Returns None if it already was."""
        room_name = request.room_name
        with self._room_lock(room_name):
            if self._live_session(room_name) is not None:
                return None

            # Deliberately outside _state_lock: this spawns a process and
            # touches the filesystem, which is far too slow to hold up readers.
            session = start_ffmpeg(request, config)

            # Watches ffmpeg's stderr for level data and keeps the Session
            # updated in real time. Started before the room is registered
            # because it is also what reports whether the start worked at all.
            threading.Thread(
                target=relay_ffmpeg_output,
                args=(self, room_name, session),
                daemon=True,
            ).start()

            # Raises if ffmpeg failed, leaving the room unregistered and idle —
            # so a failed start is reported as one instead of leaving a session
            # behind that never records anything.
            confirm_started(room_name, session)

            with self._state_lock:
                self._sessions[room_name] = session

            logger.info("[%s] recording started -> %s", room_name, session.path)
            return session

    def stop(self, room_name: str) -> Session | None:
        """Stop recording a room. Returns None if it was not recording."""
        with self._room_lock(room_name):
            session = self._live_session(room_name)
            if session is None:
                # Nothing to stop, but the room may still hold a session whose
                # ffmpeg died on its own; _live_session has just dropped it.
                return None

            stop_ffmpeg(session.process)

            with self._state_lock:
                self._sessions.pop(room_name, None)

            logger.info("[%s] recording stopped -> %s", room_name, session.path)
            return session

    def toggle(self, request: RecordingRequest, config: Config) -> tuple[bool, Session]:
        """Stop the room if it is recording, start it if it is not.

        Returns (is_recording_now, session). The room lock is held across the
        decision and the action, so simultaneous toggles queue up instead of
        both seeing an idle room.
        """
        with self._room_lock(request.room_name):
            if self._live_session(request.room_name) is not None:
                return False, self.stop(request.room_name)
            return True, self.start(request, config)

    def set_level(self, room_name: str, session: Session, channel: int, db: float):
        with self._state_lock:
            # Guard against a race where the reader thread outlives its session.
            if self._sessions.get(room_name) is not session:
                return
            if 0 <= channel < len(session.levels):
                session.levels[channel] = db

    def silence_levels(self, room_name: str, session: Session) -> None:
        with self._state_lock:
            if self._sessions.get(room_name) is session:
                session.silence_levels()

    def status(self) -> dict:
        with self._state_lock:
            finished = [r for r, s in self._sessions.items() if not s.is_running]
            for room_name in finished:
                del self._sessions[room_name]
            statuses = {r: s.status() for r, s in self._sessions.items()}
        return {"status": "online", "sessions": statuses}


def build_recording_cmd(
    config: Config,
    capture_input: CaptureInput,
    output_path: str,
) -> list[str]:
    # Builds the list of arguments we pass to ffmpeg to start a recording.
    # The backend supplies the inputs and the filter that turns them into a
    # stereo stream; everything here is the same on every platform.
    return [
        "ffmpeg",
        "-hide_banner",  # suppress the ffmpeg version/copyright header
        # Suppress the size=/time= progress ticker, which would otherwise
        # clutter the log (it is written with \r, not newlines).
        "-nostats",
        *capture_input.args,  # -f <framework> ... -i <device>, one set per input
        "-filter_complex",
        f"{capture_input.filter_prefix},{LEVEL_METERING_FILTER}[out]",
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",  # encode as MP3
        "-b:a",
        config.bitrate,
        output_path,
    ]


def free_path(path: Path) -> Path:
    """The given path, or the first numbered variant of it that is free.

    The timestamp in a recording's name only goes down to the second, so two
    recordings of the same room started within the same second would collide.
    ffmpeg refuses to overwrite an existing file, so without this the second
    recording would fail and be lost.
    """
    candidate = path
    attempt = 2
    while candidate.exists():
        candidate = path.with_stem(f"{path.stem}_{attempt}")
        attempt += 1
    return candidate


def safe_name(raw: str, fallback: str) -> str:
    """`raw` reduced to something every filesystem accepts as one folder name.

    Room and service names arrive in the request body, so they are whatever the
    Companion button was configured with — and they are about to become folder
    names. Anything unusable is replaced rather than rejected: an odd-looking
    folder is a much better outcome mid-service than a button press that
    records nothing.
    """
    name = INVALID_NAME_CHARS.sub("_", raw)
    # Windows silently drops trailing dots and spaces, so a service named "9am."
    # would not end up in the folder we think we created. Stripping leading ones
    # too also disposes of "." and "..", which contain no invalid character but
    # would otherwise walk up out of outputPath.
    name = name.strip(" .")
    if not name:
        return fallback
    if name.upper() in RESERVED_NAMES:
        # Prefixed rather than replaced, so a room genuinely called "AUX" is
        # still recognisable in the folder listing.
        return f"_{name}"
    return name


def prepare_output_path(config: Config, request: RecordingRequest) -> str:
    """Create <outputPath>/<room>/<service>/ and return the file to record into.

    Grouping recordings by room and then by service is what keeps a season of
    services findable; the room, service and timestamp stay in the filename as
    well, so a file that is copied out of here still says what it is.
    """
    room = safe_name(request.room_name, fallback="room")
    service = safe_name(request.service_name, fallback="service")
    for raw, safe, field in (
        (request.room_name, room, "room_name"),
        (request.service_name, service, "service_name"),
    ):
        if safe != raw:
            logger.warning(
                "[%s] %s %r cannot be a folder name; recording into %r instead",
                request.room_name,
                field,
                raw,
                safe,
            )

    directory = config.output_path / room / service
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Re-raised with the full path: the operator needs to know which folder
        # could not be made, and "Permission denied" alone does not say.
        raise OSError(f"could not create recording folder {directory}: {e}") from e

    # mkdir succeeding on a folder that already exists says nothing about being
    # able to write into it — a read-only OneDrive folder gets caught here
    # rather than as an ffmpeg error a second later.
    if not os.access(directory, os.W_OK):
        raise OSError(f"recording folder {directory} is not writable")

    return str(
        free_path(
            directory / f"{room}_{service}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp3"
        )
    )


def confirm_started(room_name: str, session: Session) -> None:
    """Wait until ffmpeg is known to be recording, or known to have failed.

    Popen returns as soon as the process is *spawned*, so an ffmpeg that refuses
    the device or cannot open the output file still looks like a successful
    start: the button lights up green and the service goes unrecorded. So rather
    than assume, wait for ffmpeg to say which of the two happened — the stderr
    reader is already watching for exactly that and sets the event either way.

    A healthy start settles this in around a third of a second, and a failing
    one usually faster, so the wait costs nothing that is not worth knowing.
    """
    if not session.startup_settled.wait(STARTUP_TIMEOUT_S):
        # Neither confirmed nor exited. Reporting a failure here would be its
        # own kind of lie — ffmpeg may well be recording — so let it run and
        # leave a line explaining why the button took so long to answer.
        logger.warning(
            "[%s] ffmpeg has not confirmed it is recording after %.0fs; treating "
            "it as running -> %s",
            room_name,
            STARTUP_TIMEOUT_S,
            session.path,
        )
        return

    if session.confirmed_recording:
        return

    # The event was set by the reader thread reaching end of output, which it
    # only does once ffmpeg has exited and been reaped — so exit_code and
    # last_message are both final by now.
    raise OSError(
        f"ffmpeg exited with code {session.exit_code} without recording "
        f"{session.path}: {session.last_message or 'no output from ffmpeg'}"
    )


def start_ffmpeg(request: RecordingRequest, config: Config) -> Session:
    # Ask the platform's backend which ffmpeg inputs carry the requested
    # channels. Resolved per recording because devices come and go (Dante
    # Virtual Soundcard restarting, for instance).
    capture_input = get_backend(config).capture_input(
        request.left_input_channel, request.right_input_channel
    )

    out_path = prepare_output_path(config, request)

    # stdin=PIPE lets us send "q" to stop ffmpeg gracefully later.
    # stderr=PIPE lets relay_ffmpeg_output consume the level output.
    process = subprocess.Popen(
        build_recording_cmd(config, capture_input, out_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        # Keeps Windows from opening a console window for every recording.
        creationflags=NO_WINDOW_FLAG,
    )

    return Session(
        service_name=request.service_name,
        process=process,
        started_at=time.time(),
        path=out_path,
    )


def stop_ffmpeg(process: subprocess.Popen) -> None:
    # Ask ffmpeg to stop by sending the "q" key over its stdin, which lets it
    # finalise the MP3 properly (write headers, flush buffers).
    try:
        process.stdin.write(b"q")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        # stdin is already closed — fall back to a signal instead. Windows
        # cannot deliver SIGINT to another process (send_signal rejects it),
        # so there we can only terminate, which loses the last buffered
        # fraction of a second rather than finalising the file cleanly.
        if sys.platform == "win32":
            process.terminate()
        else:
            process.send_signal(signal.SIGINT)

    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def parse_db(raw_db: bytes) -> float:
    # ffmpeg reports "inf" or "nan" for silence and invalid input; clamp those
    # to the floor so consumers always get a sensible number.
    try:
        db_value = float(raw_db.decode())
    except ValueError:
        return FLOOR_LEVEL_DB
    return max(db_value, FLOOR_LEVEL_DB) if math.isfinite(db_value) else FLOOR_LEVEL_DB


def relay_ffmpeg_output(
    registry: SessionRegistry, room_name: str, session: Session
) -> None:
    # Runs in its own thread for each active recording, reading ffmpeg's stderr
    # line by line. Level lines update the session's meter; everything else is
    # written to the log, which is where dropped packets and buffer overruns
    # show up.
    process = session.process
    try:
        while True:
            line = process.stderr.readline()
            if not line:  # ffmpeg exited — no more output
                break

            match = LEVEL_LINE.search(line)

            # A level line means audio has already reached the encoder, and the
            # markers mean ffmpeg opened everything and began; either settles
            # the question confirm_started() is waiting on.
            if not session.startup_settled.is_set() and (
                match is not None or STARTED_MARKER.search(line)
            ):
                session.confirmed_recording = True
                session.startup_settled.set()

            if match is None:
                # The "ametadata" check skips the frame/pts header lines that
                # accompany every per-second level print.
                if b"ametadata" not in line:
                    message = line.decode(errors="replace").rstrip()
                    # Kept so a start that fails can report the reason, which
                    # ffmpeg puts in the last thing it says before exiting.
                    session.last_message = message
                    logger.info("[%s] ffmpeg: %s", room_name, message)
                continue

            # astats numbers channels from 1; levels is indexed from 0.
            channel = int(match.group(1)) - 1
            registry.set_level(room_name, session, channel, parse_db(match.group(2)))
    except Exception:
        # Nothing else can report this: an exception raised in a thread is
        # printed to stderr, which is thrown away under pythonw.exe.
        logger.exception("[%s] level reader failed", room_name)
    finally:
        # wait() rather than poll(): stderr reaching EOF only means ffmpeg
        # closed the pipe, and poll() returns None until the process is
        # actually reaped, which is why exits used to be logged as "code None".
        code = process.wait()
        session.exit_code = code
        # Set last, so that a start still waiting on this reads an exit code and
        # a reason that are already final. Does nothing if the recording ran
        # normally and confirmed itself long ago.
        session.startup_settled.set()
        # A non-zero exit means ffmpeg gave up — the file is likely truncated
        # or missing, so it belongs at a level the log can be filtered for.
        log = logger.info if code == 0 else logger.error
        log("[%s] ffmpeg exited with code %s", room_name, code)
        registry.silence_levels(room_name, session)
