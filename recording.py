"""
Recording sessions
==================

Everything about recordings that are running: the ffmpeg processes, the
capture feeding them where ffmpeg does not read the device itself, their live
audio levels, and the bookkeeping that keeps these in step.

The state lives in SessionRegistry, which also holds the locks protecting it.
Callers only call registry methods and get a Session back, so the state cannot
be read without the right lock held.
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
from backends.base import AudioFeeder
from config import Config

# How often the recording is forced from the OS cache onto the disk.
# -flush_packets (see build_recording_cmd) gets each MP3 frame to the OS at
# once, but the OS may hold it in memory until its own writeback — 30 s on
# macOS — and a power cut takes what is still there. fsync closes that window.
# Each call measured well under a millisecond, also on a OneDrive folder,
# because the data is already in the cache.
DISK_SYNC_INTERVAL_S = 5.0

# Audio levels below this dB value are treated as silence / no signal.
FLOOR_LEVEL_DB = -60.0

# The per-second level lines the astats filter writes to stderr, e.g.
# "lavfi.astats.1.RMS_level=-23.4". Channel numbering starts at 1.
LEVEL_LINE = re.compile(rb"lavfi\.astats\.(\d)\.RMS_level=(-?(\d+\.\d+|inf|nan))")

# Prints each channel's RMS level (volume) to stderr once a second, where
# relay_ffmpeg_output() reads it to drive the live meter.
LEVEL_METERING_FILTER = (
    "astats=metadata=1:reset=1:measure_perchannel=RMS_level:measure_overall=none,"
    "ametadata=mode=print"
)

# Stops ffmpeg opening a console window on Windows. The constant only exists
# there, hence the fallback to 0 (no flags).
NO_WINDOW_FLAG = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How long to wait for ffmpeg to finalise the file before force-killing it.
STOP_TIMEOUT_S = 10

# Lines ffmpeg prints only once every input and output is open and encoding has
# begun — not if it gives up on the device, the encoder options or the output
# file (verified against ffmpeg 8.1). That makes them proof that a recording
# really started. A level line proves the same and more, so relay_ffmpeg_output
# counts it too. Two spellings, so that one wording change in a future ffmpeg
# does not leave every start unconfirmed.
#
# With a feeder, ffmpeg's input is a pipe and these lines say nothing about the
# device; the feeder opens the device itself and raises if it cannot.
STARTED_MARKER = re.compile(rb"Press \[q\] to stop|Output #\d+,")

# Backstop for an ffmpeg that neither confirms nor exits (a device that hangs
# on open, say). A healthy start confirms in well under half a second.
STARTUP_TIMEOUT_S = 5.0

# Characters Windows does not allow in a file or folder name. Covering both
# path separators is what keeps a room name like "../../etc" inside outputPath.
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


# One recording's parameters, and the JSON body of the start/stop/toggle
# endpoints (validated by FastAPI). Channels count from 1, as in Dante Controller.
class RecordingRequest(BaseModel):
    # Required: a recording is found again by it when stopped, so a request
    # without one is refused rather than filed under a default.
    room_name: str
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
    # Live level per output channel: 0 = left, 1 = right. A list, so the stderr
    # reader can write to it by channel number.
    levels: list[float] = dataclasses.field(
        default_factory=lambda: [FLOOR_LEVEL_DB, FLOOR_LEVEL_DB]
    )
    # How the start turned out. Written only by the stderr reader thread and
    # read by confirm_started() once the event is set, so no lock is needed.
    startup_settled: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    confirmed_recording: bool = False  # ffmpeg said it is encoding
    exit_code: int | None = None  # set instead, if it exited first
    last_message: str = ""  # its last stderr line, which says why
    # Set once ffmpeg has exited, telling sync_to_disk() to do its last fsync.
    ffmpeg_finished: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    # Writes the audio into ffmpeg's stdin where ffmpeg does not read the device
    # itself (macOS); None where it does.
    feeder: AudioFeeder | None = None

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
        # On ffmpeg's exit: silence misleads less than the last captured level.
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
    """The rooms currently recording, and the locks guarding them.

    - _state_lock guards the dictionary and is held only for dictionary
      operations, never while starting or stopping ffmpeg, so one room's start
      cannot stall the status push reading another room's levels.
    - _room_locks serialise the slow work per room, so two toggles arriving
      together cannot both find the room idle and both start. They are
      reentrant, so toggle() can hold one while calling start() or stop().
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

            # Outside _state_lock on purpose: spawning a process and touching
            # the filesystem is far too slow to hold up readers.
            session = start_ffmpeg(request, config)

            # Reads ffmpeg's stderr: keeps the levels current and reports
            # whether the start worked, so it runs before the room is registered.
            threading.Thread(
                target=relay_ffmpeg_output,
                args=(self, room_name, session),
                daemon=True,
            ).start()

            # Raises if ffmpeg failed, leaving the room unregistered and idle.
            confirm_started(room_name, session)

            # Only now: it opens the recording by name, and ffmpeg has just
            # confirmed that it created it.
            threading.Thread(
                target=sync_to_disk,
                args=(room_name, session),
                daemon=True,
            ).start()

            with self._state_lock:
                self._sessions[room_name] = session

            logger.info("[%s] recording started -> %s", room_name, session.path)
            return session

    def stop(self, room_name: str) -> Session | None:
        """Stop recording a room. Returns None if it was not recording."""
        with self._room_lock(room_name):
            session = self._live_session(room_name)
            if session is None:
                # Idle, or its ffmpeg died and _live_session has just dropped it.
                return None

            stop_ffmpeg(session)

            with self._state_lock:
                self._sessions.pop(room_name, None)

            logger.info("[%s] recording stopped -> %s", room_name, session.path)
            return session

    def toggle(self, request: RecordingRequest, config: Config) -> tuple[bool, Session]:
        """Stop the room if it is recording, start it if it is not.

        Returns (is_recording_now, session). The room lock is held across the
        decision and the action, so simultaneous toggles queue up.
        """
        with self._room_lock(request.room_name):
            if self._live_session(request.room_name) is not None:
                return False, self.stop(request.room_name)
            return True, self.start(request, config)

    def stop_all(self) -> list[str]:
        """Stop every recording room, as on shutdown; returns the rooms stopped.

        Goes through stop(), so each MP3 is finalised as for a stop request.
        """
        with self._state_lock:
            rooms = list(self._sessions)

        stopped = []
        for room_name in rooms:
            try:
                if self.stop(room_name) is not None:
                    stopped.append(room_name)
            except Exception:
                # One room failing to stop must not leave the others running.
                logger.exception("[%s] could not be stopped", room_name)
        return stopped

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
    # The ffmpeg command for one recording. The backend supplies the inputs and
    # the filter that makes them stereo; the rest is the same on every platform.
    return [
        "ffmpeg",
        "-hide_banner",  # suppress the ffmpeg version/copyright header
        # No size=/time= progress ticker: written with \r, it clutters the log.
        "-nostats",
        *capture_input.args,  # -f <format> ... -i <source>, one set per input
        "-filter_complex",
        f"{capture_input.filter_prefix},{LEVEL_METERING_FILTER}[out]",
        "-map",
        "[out]",
        "-c:a",
        "libmp3lame",  # encode as MP3
        "-b:a",
        config.bitrate,
        # Write each MP3 frame as it is encoded instead of filling ffmpeg's
        # 256KB output buffer first. Without it a power cut loses what is in
        # that buffer: up to sixteen seconds at 128k, and the whole file if the
        # recording is shorter than that. Measured: ten seconds then SIGKILL
        # leaves an empty file without the flag and a complete one with it.
        # The cost is about 38 small writes a second.
        "-flush_packets",
        "1",
        output_path,
    ]


def free_path(path: Path) -> Path:
    """The given path, or the first numbered variant of it that is free.

    Names are timestamped to the second, so two recordings of one room started
    within the same second would collide, and ffmpeg refuses to overwrite.
    """
    candidate = path
    attempt = 2
    while candidate.exists():
        candidate = path.with_stem(f"{path.stem}_{attempt}")
        attempt += 1
    return candidate


def safe_name(raw: str, fallback: str) -> str:
    """`raw` reduced to something every filesystem accepts as one folder name.

    Room and service names come from the Companion button and become folder
    names. Anything unusable is replaced rather than rejected: an odd-looking
    folder beats a button press that records nothing.
    """
    name = INVALID_NAME_CHARS.sub("_", raw)
    # Windows drops trailing dots and spaces, so "9am." would not land in the
    # folder we created. Stripping leading ones too removes "." and "..", which
    # would otherwise climb out of outputPath.
    name = name.strip(" .")
    if not name:
        return fallback
    if name.upper() in RESERVED_NAMES:
        # Prefixed, not replaced, so a room called "AUX" stays recognisable.
        return f"_{name}"
    return name


def prepare_output_path(config: Config, request: RecordingRequest) -> str:
    """Create <outputPath>/<room>/<service>/ and return the file to record into.

    The room, service and timestamp are in the filename too, so a file copied
    out of its folder still says what it is.
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
        # Re-raised with the path: "Permission denied" alone does not say where.
        raise OSError(f"could not create recording folder {directory}: {e}") from e

    # mkdir succeeding on an existing folder says nothing about writing into
    # it; this catches a read-only OneDrive folder before ffmpeg fails on it.
    if not os.access(directory, os.W_OK):
        raise OSError(f"recording folder {directory} is not writable")

    return str(
        free_path(
            directory / f"{room}_{service}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp3"
        )
    )


def confirm_started(room_name: str, session: Session) -> None:
    """Wait until ffmpeg is known to be recording, or known to have failed.

    Popen returns once the process is spawned, so an ffmpeg that refuses the
    device or the output file would still look like a successful start: a green
    button and an unrecorded service. The stderr reader sets the event either
    way, for a healthy start in about a third of a second.
    """
    if not session.startup_settled.wait(STARTUP_TIMEOUT_S):
        # Neither confirmed nor exited. ffmpeg may well be recording, so let it
        # run and log why the button took so long to answer.
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

    # The reader sets the event at end of output, once ffmpeg has been reaped,
    # so exit_code and last_message are final by now.
    raise OSError(
        f"ffmpeg exited with code {session.exit_code} without recording "
        f"{session.path}: {session.last_message or 'no output from ffmpeg'}"
    )


def start_ffmpeg(request: RecordingRequest, config: Config) -> Session:
    # Where ffmpeg gets the requested channels from. Resolved per recording
    # because devices come and go (Dante Virtual Soundcard restarting, say).
    capture_input = get_backend(config).capture_input(
        request.left_input_channel, request.right_input_channel
    )

    out_path = prepare_output_path(config, request)

    # stdin is for sending "q" to stop ffmpeg or, with a feeder, for the audio
    # itself; stderr is read by relay_ffmpeg_output for the levels.
    process = subprocess.Popen(
        build_recording_cmd(config, capture_input, out_path),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW_FLAG,
    )

    if capture_input.feeder is not None:
        try:
            capture_input.feeder.start(process.stdin, request.room_name)
        except OSError:
            # The device would not open. Take down the ffmpeg that would wait
            # for audio forever, and the empty file it has just created.
            process.kill()
            process.wait()
            close_pipes(process)
            Path(out_path).unlink(missing_ok=True)
            raise

    return Session(
        service_name=request.service_name,
        process=process,
        started_at=time.time(),
        path=out_path,
        feeder=capture_input.feeder,
    )


def stop_ffmpeg(session: Session) -> None:
    process = session.process

    if session.feeder is not None:
        # stdin carries the audio here, so there is no "q" to send. Ending the
        # capture closes the pipe, and ffmpeg finalises when its input ends.
        session.feeder.stop()
    else:
        send_quit(process)

    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def close_pipes(process: subprocess.Popen) -> None:
    """Close our ends of the pipes to an ffmpeg that has exited.

    Python would do it eventually, when the session is collected; doing it now
    keeps a long-running server from holding the pipes of finished recordings.
    """
    for pipe in (process.stdin, process.stderr):
        try:
            pipe.close()
        except OSError:
            pass  # something unsent to an ffmpeg that is no longer there


def send_quit(process: subprocess.Popen) -> None:
    # "q" over stdin makes ffmpeg stop and finalise the MP3 properly.
    try:
        process.stdin.write(b"q")
        process.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        # stdin is already closed, by ffmpeg (OSError) or by us once it exited
        # (ValueError), so fall back to a signal. Windows cannot deliver SIGINT
        # to another process; terminating instead loses the last buffered
        # fraction of a second.
        if sys.platform == "win32":
            process.terminate()
        else:
            process.send_signal(signal.SIGINT)


def sync_to_disk(room_name: str, session: Session) -> None:
    """Force the recording onto the disk every few seconds until ffmpeg is done.

    One thread per recording. ffmpeg has no option for this, so the flush has
    to come from outside the process.

    The file is opened read-only on purpose: fsync flushes its dirty pages
    either way, and a read-only handle cannot disturb what ffmpeg is writing.
    """
    try:
        fd = os.open(session.path, os.O_RDONLY)
    except OSError as e:
        # Not worth failing the recording over: -flush_packets gets the data to
        # the OS anyway; only the last seconds before a power cut are at stake.
        logger.warning(
            "[%s] cannot open %s to flush it to disk; recording continues "
            "unsynced: %s",
            room_name,
            session.path,
            e,
        )
        return

    try:
        while True:
            finished = session.ffmpeg_finished.wait(DISK_SYNC_INTERVAL_S)
            try:
                os.fsync(fd)
            except OSError as e:
                # Windows cannot flush through a read-only handle. Say it once,
                # not every tick, and leave the rest to -flush_packets.
                logger.warning(
                    "[%s] cannot flush %s to disk; recording continues unsynced: %s",
                    room_name,
                    session.path,
                    e,
                )
                return
            # After the fsync, not instead of it: this last pass gets the end
            # of a finished recording onto the disk.
            if finished:
                return
    finally:
        os.close(fd)


def parse_db(raw_db: bytes) -> float:
    # ffmpeg reports "inf" or "nan" for silence and invalid input; clamp those
    # to the floor so consumers always get a number.
    try:
        db_value = float(raw_db.decode())
    except ValueError:
        return FLOOR_LEVEL_DB
    return max(db_value, FLOOR_LEVEL_DB) if math.isfinite(db_value) else FLOOR_LEVEL_DB


def relay_ffmpeg_output(
    registry: SessionRegistry, room_name: str, session: Session
) -> None:
    # One thread per recording, reading ffmpeg's stderr. Level lines update the
    # meter; everything else, ffmpeg's warnings included, goes to the log.
    process = session.process
    try:
        while True:
            line = process.stderr.readline()
            if not line:  # ffmpeg exited — no more output
                break

            match = LEVEL_LINE.search(line)

            # A level line or a start marker settles what confirm_started()
            # is waiting on.
            if not session.startup_settled.is_set() and (
                match is not None or STARTED_MARKER.search(line)
            ):
                session.confirmed_recording = True
                session.startup_settled.set()

            if match is None:
                # "ametadata" lines are the frame/pts headers of each level print.
                if b"ametadata" not in line:
                    message = line.decode(errors="replace").rstrip()
                    # Kept for a failed start: ffmpeg says why in its last line.
                    session.last_message = message
                    logger.info("[%s] ffmpeg: %s", room_name, message)
                continue

            # astats numbers channels from 1; levels is indexed from 0.
            channel = int(match.group(1)) - 1
            registry.set_level(room_name, session, channel, parse_db(match.group(2)))
    except Exception:
        # Nothing else reports this: under pythonw.exe a thread's traceback
        # goes to a stderr that is thrown away.
        logger.exception("[%s] level reader failed", room_name)
    finally:
        # wait(), not poll(): end of stderr only means the pipe closed, and the
        # exit code is not known until the process is reaped.
        code = process.wait()
        session.exit_code = code
        # Set once the exit code and reason are final, and before anything slow
        # below, so a start still waiting learns in time that ffmpeg is gone.
        session.startup_settled.set()
        # Lets the disk syncer make its final pass over the finished file.
        session.ffmpeg_finished.set()
        # Non-zero means ffmpeg gave up and the file is likely truncated or
        # missing, so log it at a level that can be filtered for.
        log = logger.info if code == 0 else logger.error
        log("[%s] ffmpeg exited with code %s", room_name, code)
        registry.silence_levels(room_name, session)
        if session.feeder is None:
            close_pipes(process)
        else:
            # If ffmpeg ended by itself, let go of the device (a no-op after a
            # normal stop). The pipe into ffmpeg is the feeder's to close: it
            # may still be in the middle of a write to it.
            try:
                session.feeder.stop()
            except Exception:
                logger.exception("[%s] could not release the audio device", room_name)
            process.stderr.close()
