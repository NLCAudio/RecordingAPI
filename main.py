"""
Recording API
=============

This is a small web server that controls audio recording on this machine.
Other software (like Bitfocus Companion running on a control surface) talks
to it by sending HTTP requests — the same kind of requests a web browser
makes. There are three main actions:

  POST /recording/start  — begin recording a room
  POST /recording/stop   — stop recording a room
  POST /recording/toggle — start if stopped, stop if running

The server uses ffmpeg (a free command-line audio/video tool) to do the
actual recording and encodes the result as an MP3 file. While recording it
also reads the live audio level (in dB) so dashboards can display a meter.

A background task runs continuously in a separate thread and pushes the
current status to a Bitfocus Companion "custom variable" so the button panel
always shows what is happening without needing to poll.

Configuration (audio device, output folder, companion URL, etc.) lives in
config.yaml next to this file, so you don't need to touch the code to
change those settings.
"""

import dataclasses
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import requests
import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Audio levels below this dB value are treated as silence / no signal.
FLOOR_LEVEL_DB = -60
CONFIG_PATH = Path(__file__).parent / "config.yaml"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # When the server starts up, launch the status-push loop in the background.
    # daemon=True means this thread is automatically killed when the main
    # program exits, so we don't need to clean it up manually.
    threading.Thread(target=push_status, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)


# A Session holds everything we need to know about one active recording.
# Each room gets its own Session while it is recording.
@dataclasses.dataclass
class Session:
    service_name: str
    process: subprocess.Popen  # the running ffmpeg process
    started_at: float  # Unix timestamp so we can compute elapsed time
    path: str  # where the MP3 file is being written
    audio_input_level_left: float
    audio_input_level_right: float


# One entry per room that is currently recording.
# Key = room name string, Value = Session object.
sessions: dict[str, Session] = {}

# A lock prevents two threads from writing to `sessions` at the same time,
# which could corrupt the data. Any code that reads or writes `sessions`
# must hold this lock first (the `with threading_lock:` blocks below).
threading_lock = threading.Lock()


class DeviceNotFoundError(RuntimeError):
    def __init__(self, requested: str, found_devices: list[str], *args):
        self.requested = requested
        self.found_devices = found_devices
        self.message = (
            f"Device requested: {requested}, but found: {", ".join(found_devices)}"
        )
        super().__init__(self.message)


# This describes the JSON body that callers must send with start/stop/toggle
# requests. FastAPI validates the incoming request against this automatically.
class RecordingRequest(BaseModel):
    room_name: str = "room_name"
    service_name: str = "service_name"
    left_input_channel: int = 0  # which physical input maps to the left side
    right_input_channel: int = 1  # which physical input maps to the right side


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as file:
        config = yaml.safe_load(file)
    return config


def get_device_number(config: dict) -> int:
    # ffmpeg identifies audio devices by number, not by name. This function
    # asks ffmpeg to list all available devices, parses that output to build
    # a name→number mapping, and then looks up the device name from config.
    list_audio_devices_cmd = [
        "ffmpeg",
        "-f",
        config["framework"],
        "-list_devices",
        "true",
        "-i",
        '""',
    ]

    audio_devices = subprocess.run(
        list_audio_devices_cmd, capture_output=True, text=True
    )
    audio_device_idx_mapping = {}
    for audio_device in re.findall(
        rf"\[{config['framework']} indev @ \w*\] \[\d+\] [\w\s]+\n",
        re.search(r"audio devices:[\w\W]+ Error", audio_devices.stderr).group(0),
    ):
        audio_device = re.sub(
            rf"\[{config['framework']} indev @ \w+]", "", audio_device.strip().lower()
        ).strip()
        audio_device_idx = re.match(r"\[\d+\]", audio_device).group(0)
        audio_device = audio_device.replace(audio_device_idx, "").strip().lower()
        audio_device_idx_mapping[audio_device] = re.sub(
            r"[\[|\]]", "", audio_device_idx
        )

    requested_audio_device = config["inputDevice"].lower()
    if requested_audio_device in audio_device_idx_mapping:
        return audio_device_idx_mapping[requested_audio_device]

    raise DeviceNotFoundError(
        requested=requested_audio_device, found_devices=audio_device_idx_mapping.keys()
    )


def build_recording_cmd(
    config: dict,
    audio_device_number: str,
    output_path: str,
    left_input_channel: int,
    right_input_channel: int,
) -> list[str]:
    # Builds the list of arguments we pass to ffmpeg to start a recording.
    # The "-af" (audio filter) chain does two things at once:
    #   1. pan=stereo — routes the chosen input channels into a stereo file
    #      (e.g. channel 0 → left ear, channel 1 → right ear)
    #   2. astats / ametadata — computes the RMS level (volume) each second
    #      and prints it to stderr so read_db_values() can pick it up live
    return [
        "ffmpeg",
        "-hide_banner",  # suppress the ffmpeg version/copyright header
        "-f",
        config["framework"],  # audio capture framework (e.g. avfoundation on Mac)
        "-i",
        f":{audio_device_number}",  # the audio device to record from
        "-af",
        (
            f"pan=stereo|c0=c{left_input_channel}|c1=c{right_input_channel},"
            "astats=metadata=1:reset=1:measure_perchannel=RMS_level:measure_overall=none,"
            "ametadata=mode=print"
        ),
        "-c:a",
        "libmp3lame",  # encode as MP3
        "-b:a",
        config["bitrate"],  # audio quality / file size trade-off (e.g. "128k")
        output_path,
    ]


def bytes_to_db(raw_db: bytes) -> float:
    # Convert raw bytes from ffmpeg output into a usable dB float.
    # ffmpeg can output "inf" or "nan" for silence/invalid; clamp those to
    # the floor so consumers always get a sensible number.
    try:
        db_value = float(raw_db.decode())
    except ValueError:
        return FLOOR_LEVEL_DB
    return max(db_value, FLOOR_LEVEL_DB) if math.isfinite(db_value) else FLOOR_LEVEL_DB


def read_db_values(room_name: str, process: subprocess.Popen) -> None:
    # This function runs in its own background thread for each active recording.
    # It reads ffmpeg's stderr line by line looking for the audio level output
    # that the astats filter writes once per second, and updates the Session.
    db_regex = re.compile(rb"lavfi\.astats\.(\d)\.RMS_level=(-?(\d+\.\d+|inf|nan))")
    try:
        while True:
            line = process.stderr.readline()

            if not line:  # ffmpeg exited — no more output
                break

            matches = db_regex.search(line)
            if matches is None:
                continue

            channel, raw_db = matches.group(1), matches.group(2)
            parsed_db = bytes_to_db(raw_db)

            with threading_lock:
                session = sessions.get(room_name)
                # Guard against a race where this thread outlives the session
                if session is None or session.process is not process:
                    continue
                if channel == b"1":
                    session.audio_input_level_left = parsed_db
                elif channel == b"2":
                    session.audio_input_level_right = parsed_db
    finally:
        # If ffmpeg died unexpectedly, reset the levels so the meter shows silence
        # rather than the last captured value, which would be misleading.
        with threading_lock:
            session = sessions.get(room_name)
            if session is not None and session.process is process:
                session.audio_input_level_left = FLOOR_LEVEL_DB
                session.audio_input_level_right = FLOOR_LEVEL_DB


@app.post("/recording/start")
def start_recording(request: RecordingRequest):
    # Re-read config on every request so changes to config.yaml take effect
    # without restarting the server.
    config = load_config()
    try:
        audio_device_number = get_device_number(config)
    except DeviceNotFoundError as e:
        raise HTTPException(status_code=422, detail=e.message)

    with threading_lock:
        session = sessions.get(request.room_name)
        if session and session.process.poll() is None:
            # poll() returns None while the process is still running
            return {"message": f"{request.room_name} is already recording"}

        # Build a filename that includes the room, service, and timestamp
        # so recordings are easy to identify after the fact.
        out_path = os.path.join(
            config["outputPath"],
            f"{request.room_name}_{request.service_name}_{datetime.now():%Y-%m-%d_%H:%M:%S}.mp3",
        )
        os.makedirs(config["outputPath"], exist_ok=True)

        try:
            # Popen starts ffmpeg as a child process and keeps a handle to it.
            # stdin=PIPE lets us send "q" to stop it gracefully later.
            # stderr=PIPE lets the db-reading thread consume the level output.
            process = subprocess.Popen(
                build_recording_cmd(
                    config,
                    audio_device_number,
                    out_path,
                    request.left_input_channel,
                    request.right_input_channel,
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        sessions[request.room_name] = Session(
            service_name=request.service_name,
            process=process,
            started_at=time.time(),
            path=out_path,
            audio_input_level_left=FLOOR_LEVEL_DB,
            audio_input_level_right=FLOOR_LEVEL_DB,
        )

        # Launch a background thread that watches ffmpeg's stderr for
        # dB level data and keeps the Session updated in real time.
        threading.Thread(
            target=read_db_values,
            args=(request.room_name, process),
            daemon=True,
        ).start()

    return {"message": f"recording started for {request.room_name}"}


@app.post("/recording/stop")
def stop_recording(request: RecordingRequest):
    with threading_lock:
        session = sessions.get(request.room_name)
        if session is None or session.process.poll() is not None:
            # Session doesn't exist or ffmpeg already exited on its own — clean up.
            if session is not None:
                del sessions[request.room_name]
            return {"message": f"{request.room_name} is not recording"}

        process = session.process

    # Tell ffmpeg to stop gracefully by sending the "q" key over its stdin.
    # This lets it finalize the MP3 file properly (write headers, flush buffers).
    try:
        process.stdin.write(b"q")
        process.stdin.flush()
    except (BrokenPipeError, OSError):
        # stdin is already closed — fall back to an interrupt signal instead.
        process.send_signal(signal.SIGINT)

    try:
        process.wait(timeout=10)  # give ffmpeg up to 10 seconds to finish
    except subprocess.TimeoutExpired:
        process.kill()  # force-kill if it still hasn't stopped
        process.wait()

    with threading_lock:
        del sessions[request.room_name]

    return {
        "message": f"recording stopped for {request.room_name}",
        "file": session.path,
    }


@app.post("/recording/toggle")
def toggle_recording(request: RecordingRequest):
    with threading_lock:
        session = sessions.get(request.room_name)
    if session and session.process.poll() is None:
        return stop_recording(request)
    return start_recording(request)


def format_time(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def get_sessions_status() -> dict:
    out = {}
    dead = []
    with threading_lock:
        for room, session in sessions.items():
            if session.process.poll() is not None:
                dead.append(room)
                continue
            elapsed_s = int(time.time() - session.started_at)
            out[room] = {
                "service_name": session.service_name,
                "recording": True,
                "elapsed_s": elapsed_s,
                "elapsed_str": format_time(elapsed_s),
                "path": session.path,
                "audio_input_level_left": session.audio_input_level_left,
                "audio_input_level_right": session.audio_input_level_right,
            }
        for room in dead:
            del sessions[room]
    return {"status": "online", "sessions": out}


def push_status() -> None:
    # Runs forever in a background thread. Repeatedly fetches the current
    # recording status and pushes it to Bitfocus Companion as a custom variable
    # named "recording_status". Companion can then display this on buttons or
    # trigger actions based on it.
    #
    # Using a persistent requests.Session (not to be confused with the recording
    # Session class) keeps the HTTP connection alive between pushes, which is
    # more efficient than opening a new connection each time.
    push_session = requests.Session()

    config = load_config()
    companion_base_url = config["companionBaseUrl"].rstrip("/")
    # statusPushRefreshHz controls how many times per second we push.
    # Default 15 Hz means an update every ~67 ms — fast enough for a live meter.
    interval = 1.0 / float(config.get("statusPushRefreshHz", 15))
    while True:
        try:
            status = get_sessions_status()
            push_session.post(
                f"{companion_base_url}/api/custom-variable/recording_status/value?value={json.dumps(status)}"
            )
        except requests.RequestException:
            pass  # network errors are silently ignored so the loop keeps running

        time.sleep(interval)


@app.get("/status")
def get_status():
    return get_sessions_status()
