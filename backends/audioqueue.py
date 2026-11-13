"""
macOS capture backend (AudioQueue)
==================================

The default on macOS. The server reads the audio device itself, through Apple's
Audio Queue Services, and hands the audio to ffmpeg over a pipe; ffmpeg only
encodes it.

ffmpeg is not left to read the device, as it is on Windows, because its macOS
capture (avfoundation) loses audio. macOS hands it a 512-sample chunk every
10.7 ms; ffmpeg keeps only the most recent one and looks for it about every
10 ms, so a chunk is overwritten whenever it looks slightly late. Measured on a
MacBook's built-in microphone that was a tenth of the audio, and ffmpeg has no
option to change it. An audio queue holds seconds of audio rather than one
chunk, so a late reader costs nothing until the whole queue is full — and if
audio is lost after all, the queue's timestamps show it, so it is logged.

As with avfoundation, the Dante Virtual Soundcard is one device carrying all of
its channels, so a channel number is a channel of that one device. The two
channels a room asked for are picked out here, before the pipe: ffmpeg is
handed plain stereo whatever the device's channel count.

The file reads top down: the backend, the capture it starts for each recording,
then the calls into Apple's frameworks that both rest on. Nothing outside the
standard library is used, and nothing macOS-specific is loaded until a device
is listed or opened, so importing this file on Windows is harmless.
"""

import array
import collections
import ctypes
import ctypes.util
import dataclasses
import functools
import logging
import math
import os
import queue
import struct
import threading
from typing import BinaryIO, NamedTuple

from .base import (
    CaptureBackend,
    CaptureInput,
    ChannelNotAvailableError,
    DeviceNotFoundError,
    pan_to_stereo,
)

# How much audio the capture queue holds, and the least it is ever given: an
# audioBufferMs below this is raised to it. It is the stall this server's own
# threads can have without audio being lost. Memory is the only cost — about
# 25 MB per recording on a 64-channel device — so a shorter queue gains nothing,
# and a value sized for the Windows buffer must not shrink this one.
MIN_BUFFER_MS = 2000

# Each buffer in the queue holds this much audio. Long enough that Python is
# called only ten times a second, short enough that the level meter, which is
# fed from this stream, does not visibly lag.
BUFFER_MS_EACH = 100

# How far ffmpeg may fall behind before audio is dropped instead of kept
# waiting for it. A stalled disk or encoder is ridden out in memory up to here.
# Without a limit, one recording whose ffmpeg had hung for good would grow
# until it took the server, and every other room's recording, down with it.
MAX_BACKLOG_S = 30

# If nothing has arrived from the device for this long, say so. A device that
# stops delivering (Dante Virtual Soundcard being stopped, say) does not end
# the queue or raise an error; the audio just ceases.
SILENT_DEVICE_WARNING_S = 5.0

# How long stop() waits for the last audio to be written into ffmpeg. Only
# reached if ffmpeg has stopped reading.
DRAIN_TIMEOUT_S = 5.0

# How often the writer thread looks up from an empty queue.
POLL_S = 0.2

logger = logging.getLogger("recording_api")


@dataclasses.dataclass(frozen=True)
class InputDevice:
    name: str
    # The identifier that stays the same across reboots, unlike the numeric
    # device id; it is what an audio queue is pointed at a device with.
    uid: str
    channels: int  # how many input channels the device has
    sample_rate: float


class AudioQueueBackend(CaptureBackend):
    framework = "audioqueue"

    def capture_input(self, left_channel: int, right_channel: int) -> CaptureInput:
        device = self.find_device()

        for channel in (left_channel, right_channel):
            if not 1 <= channel <= device.channels:
                raise ChannelNotAvailableError(
                    f"Channel {channel} is not offered by '{device.name}'. "
                    f"Available channels: 1-{device.channels}"
                )

        return CaptureInput(
            # Raw PCM on stdin, in the format AudioQueueCapture writes: stereo,
            # interleaved 32-bit floats. ffmpeg cannot detect any of that from
            # a bare stream, so all three are stated.
            args=[
                "-f",
                "f32le",
                "-ar",
                str(int(device.sample_rate)),
                "-ac",
                "2",
                "-i",
                "pipe:0",
            ],
            # Already stereo, left first: nothing to route, only to name.
            filter_prefix="[0:a]" + pan_to_stereo(0, 1),
            feeder=AudioQueueCapture(
                device,
                # The device's channels are counted from zero in the audio it
                # delivers, and from one everywhere a person sees them.
                left=left_channel - 1,
                right=right_channel - 1,
                buffer_ms=max(self.config.audio_buffer_ms or 0, MIN_BUFFER_MS),
            ),
        )

    def find_device(self) -> InputDevice:
        devices = list_input_devices()
        requested = self.normalise(self.config.input_device)

        for device in devices:
            if self.normalise(device.name) == requested:
                return device

        raise DeviceNotFoundError(
            requested=requested,
            found_devices=[self.normalise(device.name) for device in devices],
        )


def pick_stereo(interleaved, channels: int, left: int, right: int) -> bytes:
    """Two channels out of interleaved 32-bit float audio, as stereo.

    `interleaved` is anything that exposes its memory (bytes, a ctypes array);
    `left` and `right` are 0-based channel indexes and may be the same channel.
    Every step runs in C, so it costs microseconds per buffer whatever the
    channel count — which matters, because it runs on AudioQueue's own thread.
    """
    samples = memoryview(interleaved).cast("B").cast("f")
    frames = len(samples) // channels
    end = frames * channels
    stereo = array.array("f", bytes(8 * frames))
    stereo[0::2] = array.array("f", samples[left:end:channels].tobytes())
    stereo[1::2] = array.array("f", samples[right:end:channels].tobytes())
    return stereo.tobytes()


class AudioQueueCapture:
    """One recording's capture: two channels of a device, written into ffmpeg.

    start() begins recording and returns. stop() ends it and waits for what is
    left to be written; it may be called more than once and from any thread.

    The pipe into ffmpeg is closed by the thread that writes to it, whenever
    and however that thread ends. Closing it is what tells ffmpeg the recording
    is over and lets it finalise the file — so a capture that fails ends the
    recording, instead of leaving one that looks alive and records nothing.
    """

    def __init__(self, device: InputDevice, left: int, right: int, buffer_ms: int):
        self.device = device
        self.left = left  # 0-based channel indexes within the device
        self.right = right
        self.buffer_ms = buffer_ms
        self.label = device.name  # for log lines; start() makes it the room

        self.bytes_per_frame = 4 * device.channels  # 32-bit float per channel

        # Stereo chunks on their way to ffmpeg: filled by the callback on a
        # thread AudioQueue owns, emptied by the writer thread. This is what
        # rides out ffmpeg being slow, up to MAX_BACKLOG_S of it.
        self.chunks: queue.Queue[bytes] = queue.Queue(
            maxsize=int(MAX_BACKLOG_S * 1000 / BUFFER_MS_EACH)
        )
        # Set once buffers should no longer be handed back to the queue to be
        # refilled: while stopping, or because the recording has failed.
        self.winding_down = threading.Event()
        # Set once no more audio will be added to chunks, which is the writer
        # thread's cue to write what is there and close the pipe.
        self.capture_ended = threading.Event()

        # Where the next buffer should start in the device's stream, the gaps
        # found between buffers (in frames, for the writer thread to report),
        # and how much has been dropped because ffmpeg is too far behind.
        self.next_sample_time: float | None = None
        self.gaps: collections.deque[float] = collections.deque()
        self.dropped_frames = 0

        self.queue_ref = ctypes.c_void_p()
        # ctypes keeps the callback alive only as long as we hold on to it.
        self.callback = INPUT_CALLBACK(self.on_buffer)
        self.sink: BinaryIO | None = None
        self.writer: threading.Thread | None = None
        self.stop_lock = threading.Lock()
        self.stopped = False

    def start(self, sink: BinaryIO, label: str) -> None:
        """Start recording into sink (ffmpeg's stdin). Raises CaptureError.

        If it raises, nothing is left running and sink is untouched: the
        caller still owns ffmpeg and has to take it down.
        """
        audio_toolbox = frameworks().audio_toolbox
        core_foundation = frameworks().core_foundation
        device = self.device
        self.sink = sink
        self.label = label

        description = StreamDescription(
            sample_rate=device.sample_rate,
            format_id=fourcc("lpcm"),
            format_flags=1 | 8,  # float, packed
            bytes_per_packet=self.bytes_per_frame,
            frames_per_packet=1,
            bytes_per_frame=self.bytes_per_frame,
            channels_per_frame=device.channels,
            bits_per_channel=32,
        )
        check(
            audio_toolbox.AudioQueueNewInput(
                ctypes.byref(description),
                self.callback,
                None,  # no user data: the callback is a bound method
                None,  # no run loop: AudioQueue calls back on a thread of its own
                None,
                0,
                ctypes.byref(self.queue_ref),
            ),
            "creating the audio queue",
        )
        try:
            # Without this the queue records from whatever the system's
            # default input happens to be.
            uid_ref = ctypes.c_void_p(
                core_foundation.CFStringCreateWithCString(
                    None, device.uid.encode(), KCF_STRING_ENCODING_UTF8
                )
            )
            try:
                check(
                    audio_toolbox.AudioQueueSetProperty(
                        self.queue_ref,
                        fourcc("aqcd"),
                        ctypes.byref(uid_ref),
                        ctypes.sizeof(uid_ref),
                    ),
                    f"selecting the device {device.name!r}",
                )
            finally:
                core_foundation.CFRelease(uid_ref)

            if device.channels > 2:
                # Beyond stereo, a queue may want to be told what the channels
                # are. "Discrete, in order" says they are simply the device's
                # channels as they come, which is all a Dante device's are.
                # An AudioChannelLayout: tag, bitmap, description count.
                layout = struct.pack("@III", (147 << 16) | device.channels, 0, 0)
                status = audio_toolbox.AudioQueueSetProperty(
                    self.queue_ref, fourcc("aqcl"), layout, len(layout)
                )
                if status != 0:
                    # Not fatal by itself: if the queue really cannot run
                    # without it, starting it below fails and says so.
                    logger.info(
                        "[%s] could not set a %d-channel layout (status %s)",
                        self.label,
                        device.channels,
                        describe_status(status),
                    )

            frames_per_buffer = int(device.sample_rate * BUFFER_MS_EACH / 1000)
            for _ in range(math.ceil(self.buffer_ms / BUFFER_MS_EACH)):
                buffer = ctypes.POINTER(QueueBuffer)()
                check(
                    audio_toolbox.AudioQueueAllocateBuffer(
                        self.queue_ref,
                        frames_per_buffer * self.bytes_per_frame,
                        ctypes.byref(buffer),
                    ),
                    "allocating a capture buffer",
                )
                check(
                    audio_toolbox.AudioQueueEnqueueBuffer(
                        self.queue_ref, buffer, 0, None
                    ),
                    "queueing a capture buffer",
                )

            check(
                audio_toolbox.AudioQueueStart(self.queue_ref, None),
                f"starting the capture from {device.name!r}",
            )
        except CaptureError:
            audio_toolbox.AudioQueueDispose(self.queue_ref, True)
            raise

        self.writer = threading.Thread(
            target=self.write_to_sink, daemon=True, name=f"capture-{label}"
        )
        self.writer.start()

    def on_buffer(self, _user, _queue, buffer, timestamp, _count, _descriptions):
        """Called by AudioQueue, on its own thread, with each filled buffer."""
        try:
            frames = buffer.contents.byte_size // self.bytes_per_frame
            if frames:
                # Each buffer says where in the device's stream it starts. If
                # that is not where the previous one ended, the audio in
                # between was never delivered: the queue had no free buffer
                # left for it.
                start = timestamp.contents.sample_time
                expected = self.next_sample_time
                if expected is not None and start - expected > 1:
                    self.gaps.append(start - expected)
                self.next_sample_time = start + frames

                channels = self.device.channels
                samples = (ctypes.c_float * (frames * channels)).from_address(
                    buffer.contents.data
                )
                # Copies the two channels out, so the buffer is free to be
                # refilled as soon as it is handed back below.
                self.keep(pick_stereo(samples, channels, self.left, self.right), frames)
            if not self.winding_down.is_set():
                # Hand the buffer back to be filled again.
                frameworks().audio_toolbox.AudioQueueEnqueueBuffer(
                    self.queue_ref, buffer, 0, None
                )
        except Exception:
            # Must not escape into AudioQueue's thread. Whatever went wrong
            # would go wrong for every buffer, so end the recording where it
            # can be seen rather than go on recording nothing.
            logger.exception(
                "[%s] audio capture failed; ending the recording", self.label
            )
            self.winding_down.set()
            self.capture_ended.set()

    def keep(self, chunk: bytes, frames: int) -> None:
        """Queue one chunk for ffmpeg, or count it as lost if there is no room."""
        try:
            self.chunks.put_nowait(chunk)
        except queue.Full:
            # Logged when it starts and when it ends, not once per chunk: ten
            # lines a second would bury the rest of the log.
            if self.dropped_frames == 0:
                logger.error(
                    "[%s] ffmpeg is more than %d s behind; audio is being lost "
                    "until it catches up",
                    self.label,
                    MAX_BACKLOG_S,
                )
            self.dropped_frames += frames
            return

        if self.dropped_frames:
            logger.error(
                "[%s] ffmpeg has caught up; %.1f s of audio were lost meanwhile",
                self.label,
                self.dropped_frames / self.device.sample_rate,
            )
            self.dropped_frames = 0

    def write_to_sink(self) -> None:
        """Move audio from the queue into ffmpeg until the capture has ended."""
        silent_for = 0.0
        warned_silent = False
        try:
            fd = self.sink.fileno()
            while True:
                while self.gaps:
                    logger.warning(
                        "[%s] lost %.0f ms of audio: the capture buffer filled up "
                        "(raise audioBufferMs)",
                        self.label,
                        1000 * self.gaps.popleft() / self.device.sample_rate,
                    )
                try:
                    chunk = self.chunks.get(timeout=POLL_S)
                except queue.Empty:
                    if self.capture_ended.is_set():
                        return  # everything that was captured has been written
                    silent_for += POLL_S
                    if silent_for >= SILENT_DEVICE_WARNING_S and not warned_silent:
                        logger.warning(
                            "[%s] no audio has arrived from the device for %.0f s",
                            self.label,
                            SILENT_DEVICE_WARNING_S,
                        )
                        warned_silent = True
                    continue
                if warned_silent:
                    logger.info("[%s] audio is arriving again", self.label)
                silent_for, warned_silent = 0.0, False
                write_all(fd, chunk)
        except OSError as e:
            # ffmpeg has gone away, and has said why on its own stderr. Not
            # worth a line when that happened because the recording was being
            # stopped anyway.
            if not self.stopped:
                logger.warning("[%s] ffmpeg stopped reading audio: %s", self.label, e)
        except Exception:
            logger.exception("[%s] capture writer failed", self.label)
        finally:
            # Nothing will be written after this, so stop the queue being
            # refilled for nobody, and let ffmpeg see the end of its input.
            # Closed here and nowhere else: closing a pipe another thread is
            # still writing to would free its number for the next recording's
            # pipe, and this recording's audio would land in that one.
            self.winding_down.set()
            try:
                self.sink.close()
            except OSError:
                pass  # ffmpeg had already gone

    def stop(self) -> None:
        with self.stop_lock:
            if self.writer is None or self.stopped:
                return  # never started, or already stopped
            self.stopped = True

            audio_toolbox = frameworks().audio_toolbox
            self.winding_down.set()
            # Synchronous: the callback runs once more for each buffer that is
            # partly filled, so the last fraction of a second is not cut off.
            audio_toolbox.AudioQueueStop(self.queue_ref, True)
            self.capture_ended.set()

            self.writer.join(DRAIN_TIMEOUT_S)
            if self.writer.is_alive():
                # ffmpeg is not reading. The pipe stays open, because the
                # writer is still inside a write to it; the caller gives up on
                # ffmpeg next, and killing it is what ends that write.
                logger.warning(
                    "[%s] ffmpeg did not take the last of the audio within %.0f s",
                    self.label,
                    DRAIN_TIMEOUT_S,
                )
            audio_toolbox.AudioQueueDispose(self.queue_ref, True)


def write_all(fd: int, data: bytes) -> None:
    # A pipe takes less than one chunk at a time, so a single os.write would
    # silently drop the rest.
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


# --- Apple's frameworks -------------------------------------------------------
#
# Everything below is the plumbing: the C structures and calls of CoreAudio
# (which devices exist), AudioToolbox (the audio queue) and CoreFoundation
# (their strings), reached through ctypes.

KCF_STRING_ENCODING_UTF8 = 0x08000100
AUDIO_OBJECT_SYSTEM = 1  # kAudioObjectSystemObject


def fourcc(code: str) -> int:
    """CoreAudio names its properties with four characters packed in an int."""
    return struct.unpack(">I", code.encode())[0]


SCOPE_GLOBAL = fourcc("glob")
SCOPE_INPUT = fourcc("inpt")


class PropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    ]


class StreamDescription(ctypes.Structure):
    """AudioStreamBasicDescription: the PCM format asked of the queue."""

    _fields_ = [
        ("sample_rate", ctypes.c_double),
        ("format_id", ctypes.c_uint32),
        ("format_flags", ctypes.c_uint32),
        ("bytes_per_packet", ctypes.c_uint32),
        ("frames_per_packet", ctypes.c_uint32),
        ("bytes_per_frame", ctypes.c_uint32),
        ("channels_per_frame", ctypes.c_uint32),
        ("bits_per_channel", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
    ]


class QueueBuffer(ctypes.Structure):
    """AudioQueueBuffer: one filled buffer, as handed to the callback."""

    _fields_ = [
        ("capacity", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
        ("byte_size", ctypes.c_uint32),
        ("user_data", ctypes.c_void_p),
        ("packet_description_capacity", ctypes.c_uint32),
        ("packet_descriptions", ctypes.c_void_p),
        ("packet_description_count", ctypes.c_uint32),
    ]


class TimeStamp(ctypes.Structure):
    """The start of AudioTimeStamp, which is all that is read from it."""

    _fields_ = [("sample_time", ctypes.c_double), ("host_time", ctypes.c_uint64)]


INPUT_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p,  # user data
    ctypes.c_void_p,  # the queue
    ctypes.POINTER(QueueBuffer),
    ctypes.POINTER(TimeStamp),
    ctypes.c_uint32,  # packet description count, unused for PCM
    ctypes.c_void_p,  # packet descriptions, unused for PCM
)


class CaptureError(OSError):
    """A device could not be read, opened or started.

    An OSError so that the API reports it the way it reports ffmpeg failing to
    start: nothing the caller can fix by pressing the button again.
    """


class Frameworks(NamedTuple):
    core_audio: ctypes.CDLL
    core_foundation: ctypes.CDLL
    audio_toolbox: ctypes.CDLL


@functools.cache
def frameworks() -> Frameworks:
    """Load the three Apple frameworks and declare the calls made into them.

    The declarations matter: without them ctypes passes every argument as a
    32-bit int, which truncates the 64-bit pointers these calls take.
    """
    core_audio = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
    core_foundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    audio_toolbox = ctypes.CDLL(ctypes.util.find_library("AudioToolbox"))

    address = ctypes.POINTER(PropertyAddress)
    size = ctypes.POINTER(ctypes.c_uint32)
    core_audio.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p, size,
    ]  # fmt: skip
    core_audio.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, address, ctypes.c_uint32, ctypes.c_void_p, size,
        ctypes.c_void_p,
    ]  # fmt: skip

    core_foundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
    ]  # fmt: skip
    core_foundation.CFStringGetCString.restype = ctypes.c_bool
    core_foundation.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
    ]  # fmt: skip
    core_foundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None

    buffer = ctypes.POINTER(QueueBuffer)
    audio_toolbox.AudioQueueNewInput.argtypes = [
        ctypes.POINTER(StreamDescription), INPUT_CALLBACK, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
    ]  # fmt: skip
    audio_toolbox.AudioQueueSetProperty.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
    ]  # fmt: skip
    audio_toolbox.AudioQueueAllocateBuffer.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(buffer),
    ]  # fmt: skip
    audio_toolbox.AudioQueueEnqueueBuffer.argtypes = [
        ctypes.c_void_p, buffer, ctypes.c_uint32, ctypes.c_void_p,
    ]  # fmt: skip
    audio_toolbox.AudioQueueStart.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    audio_toolbox.AudioQueueStop.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    audio_toolbox.AudioQueueDispose.argtypes = [ctypes.c_void_p, ctypes.c_bool]

    return Frameworks(core_audio, core_foundation, audio_toolbox)


def describe_status(status: int) -> str:
    """An Apple error code, as the four characters it usually stands for."""
    packed = struct.pack(">i", status)
    if all(32 <= byte < 127 for byte in packed):
        return f"{status} ('{packed.decode()}')"
    return str(status)


def check(status: int, action: str) -> None:
    if status != 0:
        raise CaptureError(f"{action} failed with status {describe_status(status)}")


def property_size(object_id: int, selector: str, scope: int = SCOPE_GLOBAL) -> int:
    """How many bytes a property holds; 0 if the object does not have it."""
    address = PropertyAddress(fourcc(selector), scope, 0)
    size = ctypes.c_uint32(0)
    status = frameworks().core_audio.AudioObjectGetPropertyDataSize(
        object_id, ctypes.byref(address), 0, None, ctypes.byref(size)
    )
    return size.value if status == 0 else 0


def read_property(
    object_id: int, selector: str, size: int, scope: int = SCOPE_GLOBAL
) -> bytes:
    """Up to `size` bytes of a property: what is there may have shrunk since."""
    address = PropertyAddress(fourcc(selector), scope, 0)
    data = ctypes.create_string_buffer(size)
    actual = ctypes.c_uint32(size)
    check(
        frameworks().core_audio.AudioObjectGetPropertyData(
            object_id, ctypes.byref(address), 0, None, ctypes.byref(actual), data
        ),
        f"reading '{selector}' of audio object {object_id}",
    )
    return data.raw[: actual.value]


def read_fixed(
    object_id: int, selector: str, layout: str, scope: int = SCOPE_GLOBAL
) -> tuple:
    """A property of a known layout (a struct format), unpacked."""
    size = struct.calcsize(layout)
    data = read_property(object_id, selector, size, scope)
    if len(data) != size:
        raise CaptureError(
            f"'{selector}' of audio object {object_id} is {len(data)} bytes, "
            f"not the {size} expected"
        )
    return struct.unpack(layout, data)


def read_string(object_id: int, selector: str) -> str:
    """A property whose value is a CFString, as a Python string."""
    core_foundation = frameworks().core_foundation
    (reference,) = read_fixed(object_id, selector, "@P")
    if not reference:
        raise CaptureError(f"audio object {object_id} has no '{selector}'")
    try:
        text = ctypes.create_string_buffer(1024)
        if not core_foundation.CFStringGetCString(
            reference, text, len(text), KCF_STRING_ENCODING_UTF8
        ):
            raise CaptureError(
                f"'{selector}' of audio object {object_id} cannot be read as text"
            )
        return text.value.decode()
    finally:
        # Reading a string property hands over a reference that is ours to
        # release.
        core_foundation.CFRelease(reference)


def input_channel_count(device_id: int) -> int:
    size = property_size(device_id, "slay", SCOPE_INPUT)
    # An AudioBufferList: a count, then one 16-byte entry per stream of the
    # device, each starting with how many channels that stream carries.
    layout = read_property(device_id, "slay", size, SCOPE_INPUT) if size else b""
    if len(layout) < 8:
        return 0
    (stream_count,) = struct.unpack_from("@I", layout, 0)
    if len(layout) < 8 + 16 * stream_count:
        raise CaptureError(f"audio object {device_id} has a truncated stream list")
    return sum(
        struct.unpack_from("@I", layout, 8 + 16 * index)[0]
        for index in range(stream_count)
    )


def describe_device(device_id: int) -> InputDevice | None:
    """The device as an InputDevice, or None if it has no input channels."""
    channels = input_channel_count(device_id)
    if channels == 0:
        return None  # an output-only device, such as the speakers
    (sample_rate,) = read_fixed(device_id, "nsrt", "@d")
    return InputDevice(
        name=read_string(device_id, "lnam"),
        uid=read_string(device_id, "uid "),
        channels=channels,
        sample_rate=sample_rate,
    )


def list_input_devices() -> list[InputDevice]:
    """Every audio device on this Mac that has at least one input channel."""
    size = property_size(AUDIO_OBJECT_SYSTEM, "dev#")
    listing = read_property(AUDIO_OBJECT_SYSTEM, "dev#", size) if size else b""
    count = len(listing) // 4
    device_ids = struct.unpack(f"@{count}I", listing[: count * 4])

    devices = []
    for device_id in device_ids:
        try:
            device = describe_device(device_id)
        except CaptureError as e:
            # A headset that was switched off while the list was being read, or
            # a virtual device that does not answer. No reason to refuse a
            # recording from a different device, so leave it out and say so.
            logger.warning("leaving out an audio device that cannot be read: %s", e)
            continue
        if device is not None:
            devices.append(device)
    return devices
