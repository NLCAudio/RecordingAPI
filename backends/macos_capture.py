"""
macOS audio capture helper (AudioQueue)
=======================================

Reads audio from one input device and writes it to stdout as raw PCM, for
ffmpeg to encode. It exists because ffmpeg's own macOS capture (avfoundation)
loses audio: macOS hands it a 512-sample chunk every 10.7 ms, ffmpeg keeps
only the most recent one and looks for it about every 10 ms, so a chunk is
overwritten whenever it looks slightly late. Measured on a MacBook's built-in
microphone that was a tenth of the audio, and ffmpeg has no option to change
it. Apple's Audio Queue Services, part of macOS itself, keep a queue of
buffers instead of one, so a late reader costs nothing until the whole queue
is full.

AudioQueueCapture below runs inside the server. For each recording it opens
a queue on the device and a thread that writes what the queue delivers into
ffmpeg's stdin, as raw PCM: every input channel of the device, interleaved, as
32-bit floats. Picking the two channels of a room is left to ffmpeg, exactly
as with the other backends.

Only the standard library is used, and nothing macOS-specific is loaded until
a function is called, so importing this file on Windows is harmless.
"""

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
from typing import BinaryIO

# Each buffer in the queue holds this much audio. Long enough that Python is
# called only ten times a second, short enough that the level meter, which is
# fed from this stream, does not visibly lag.
BUFFER_MS_EACH = 100

# If nothing has arrived from the device for this long, say so. A device that
# stops delivering (Dante Virtual Soundcard being stopped, say) does not end
# the queue or raise an error; the audio just ceases.
SILENT_DEVICE_WARNING_S = 5.0

# How long stop() waits for the last audio to be written into ffmpeg before
# closing the pipe regardless. Only reached if ffmpeg has stopped reading.
DRAIN_TIMEOUT_S = 5.0

KCF_STRING_ENCODING_UTF8 = 0x08000100
AUDIO_OBJECT_SYSTEM = 1  # kAudioObjectSystemObject

logger = logging.getLogger("recording_api")


def fourcc(code: str) -> int:
    """CoreAudio names its properties with four characters packed in an int."""
    return struct.unpack(">I", code.encode())[0]


SCOPE_GLOBAL = fourcc("glob")
SCOPE_INPUT = fourcc("inpt")


@dataclasses.dataclass(frozen=True)
class InputDevice:
    name: str
    # The identifier that stays the same across reboots, unlike the numeric
    # device id; it is what an audio queue is pointed at a device with.
    uid: str
    channels: int  # how many input channels the device has
    sample_rate: float


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
    """The device could not be opened or started.

    An OSError so that the API reports it the way it reports ffmpeg failing to
    start: nothing the caller can fix by pressing the button again.
    """


@functools.cache
def frameworks():
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

    return core_audio, core_foundation, audio_toolbox


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
    core_audio, _, _ = frameworks()
    address = PropertyAddress(fourcc(selector), scope, 0)
    size = ctypes.c_uint32(0)
    status = core_audio.AudioObjectGetPropertyDataSize(
        object_id, ctypes.byref(address), 0, None, ctypes.byref(size)
    )
    return size.value if status == 0 else 0


def read_property(
    object_id: int, selector: str, size: int, scope: int = SCOPE_GLOBAL
) -> bytes:
    core_audio, _, _ = frameworks()
    address = PropertyAddress(fourcc(selector), scope, 0)
    data = ctypes.create_string_buffer(size)
    actual = ctypes.c_uint32(size)
    check(
        core_audio.AudioObjectGetPropertyData(
            object_id, ctypes.byref(address), 0, None, ctypes.byref(actual), data
        ),
        f"reading '{selector}' of audio object {object_id}",
    )
    return data.raw[: actual.value]


def read_string(object_id: int, selector: str) -> str:
    """A property whose value is a CFString, as a Python string."""
    _, core_foundation, _ = frameworks()
    (reference,) = struct.unpack("@P", read_property(object_id, selector, 8))
    try:
        text = ctypes.create_string_buffer(1024)
        core_foundation.CFStringGetCString(
            reference, text, len(text), KCF_STRING_ENCODING_UTF8
        )
        return text.value.decode()
    finally:
        # Reading a string property hands over a reference that is ours to
        # release.
        core_foundation.CFRelease(reference)


def input_channel_count(device_id: int) -> int:
    size = property_size(device_id, "slay", SCOPE_INPUT)
    if size == 0:
        return 0
    # An AudioBufferList: a count, then one 16-byte entry per stream of the
    # device, each starting with how many channels that stream carries.
    layout = read_property(device_id, "slay", size, SCOPE_INPUT)
    (stream_count,) = struct.unpack_from("@I", layout, 0)
    return sum(
        struct.unpack_from("@I", layout, 8 + 16 * index)[0]
        for index in range(stream_count)
    )


def list_input_devices() -> list[InputDevice]:
    """Every audio device on this Mac that has at least one input channel."""
    size = property_size(AUDIO_OBJECT_SYSTEM, "dev#")
    device_ids = struct.unpack(
        f"@{size // 4}I", read_property(AUDIO_OBJECT_SYSTEM, "dev#", size)
    )

    devices = []
    for device_id in device_ids:
        channels = input_channel_count(device_id)
        if channels == 0:
            continue  # an output-only device, such as the speakers
        (sample_rate,) = struct.unpack("@d", read_property(device_id, "nsrt", 8))
        devices.append(
            InputDevice(
                name=read_string(device_id, "lnam"),
                uid=read_string(device_id, "uid "),
                channels=channels,
                sample_rate=sample_rate,
            )
        )
    return devices


def write_all(fd: int, data: bytes) -> None:
    # A pipe takes far less than one buffer at a time, so a single os.write
    # would silently drop the rest.
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


class AudioQueueCapture:
    """One recording's capture: a device, read into ffmpeg's stdin.

    start() begins recording and returns; stop() ends it, writes what is left
    and closes the pipe, which is what tells ffmpeg the recording is over and
    lets it finalise the file. stop() may be called more than once and from
    any thread.
    """

    def __init__(self, device: InputDevice, buffer_ms: int, label: str):
        self.device = device
        self.buffer_ms = buffer_ms
        self.label = label  # prefixes log lines, e.g. the device name

        self.bytes_per_frame = 4 * device.channels  # 32-bit float per channel
        # Filled by the callback on a thread AudioQueue owns, emptied by the
        # writer thread. Unbounded on purpose: if ffmpeg stops reading for a
        # while the audio waits here, in memory, instead of being lost. None
        # marks the end.
        self.chunks: queue.Queue[bytes | None] = queue.Queue()
        self.lost_frames: collections.deque[float] = collections.deque()
        self.next_sample_time: float | None = None
        # Set once no more buffers should be handed back to the queue: while
        # stopping, and when ffmpeg has gone away and there is nobody to
        # record for.
        self.winding_down = threading.Event()

        self.queue_ref = ctypes.c_void_p()
        # ctypes only keeps the callback alive as long as we hold on to it.
        self.callback = INPUT_CALLBACK(self.on_buffer)
        self.sink: BinaryIO | None = None
        self.writer: threading.Thread | None = None
        self.stop_lock = threading.Lock()
        self.stopped = False

    def on_buffer(self, _user, _queue, buffer, timestamp, _count, _descriptions):
        """Called by AudioQueue, on its own thread, with each filled buffer."""
        try:
            filled = buffer.contents.byte_size
            if filled:
                # Each buffer says where in the device's stream it starts. If
                # that is not where the previous one ended, audio in between
                # was never delivered — which is the thing ffmpeg's capture
                # did constantly and could not report.
                start = timestamp.contents.sample_time
                expected = self.next_sample_time
                if expected is not None and start - expected > 1:
                    self.lost_frames.append(start - expected)
                self.next_sample_time = start + filled // self.bytes_per_frame
                self.chunks.put(ctypes.string_at(buffer.contents.data, filled))
            if not self.winding_down.is_set():
                # Hand the buffer back to be filled again.
                _, _, audio_toolbox = frameworks()
                audio_toolbox.AudioQueueEnqueueBuffer(self.queue_ref, buffer, 0, None)
        except Exception:  # must not escape into AudioQueue's thread
            logger.exception("[%s] capture callback failed", self.label)

    def start(self, sink: BinaryIO) -> None:
        """Start recording into sink (ffmpeg's stdin). Raises CaptureError."""
        _, core_foundation, audio_toolbox = frameworks()
        device = self.device
        self.sink = sink

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
                ctypes.byref(description), self.callback, None, None, None, 0,
                ctypes.byref(self.queue_ref),
            ),  # fmt: skip
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
                        self.queue_ref, fourcc("aqcd"), ctypes.byref(uid_ref),
                        ctypes.sizeof(uid_ref),
                    ),  # fmt: skip
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
            for _ in range(max(3, math.ceil(self.buffer_ms / BUFFER_MS_EACH))):
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
            self.stopped = True
            raise

        self.writer = threading.Thread(
            target=self.write_to_sink, daemon=True, name=f"capture-{self.label}"
        )
        self.writer.start()

    def write_to_sink(self) -> None:
        """Move audio from the queue into ffmpeg until told it has ended."""
        fd = self.sink.fileno()
        silent_for = 0.0
        warned_silent = False
        try:
            while True:
                while self.lost_frames:
                    lost = self.lost_frames.popleft()
                    logger.warning(
                        "[%s] lost %.0f ms of audio: the capture buffer filled up "
                        "(raise audioBufferMs)",
                        self.label,
                        1000 * lost / self.device.sample_rate,
                    )
                try:
                    chunk = self.chunks.get(timeout=0.2)
                except queue.Empty:
                    silent_for += 0.2
                    if silent_for >= SILENT_DEVICE_WARNING_S and not warned_silent:
                        logger.warning(
                            "[%s] no audio has arrived from the device for %.0f s",
                            self.label,
                            SILENT_DEVICE_WARNING_S,
                        )
                        warned_silent = True
                    continue
                if chunk is None:
                    return
                if warned_silent:
                    logger.info("[%s] audio is arriving again", self.label)
                silent_for, warned_silent = 0.0, False
                write_all(fd, chunk)
        except OSError as e:
            # ffmpeg has gone away, and has said why on its own stderr. Stop
            # feeding the queue so the audio nobody reads does not pile up;
            # stop() tidies the rest once the recording is taken down.
            self.winding_down.set()
            if not self.stopped:
                logger.warning("[%s] ffmpeg stopped reading audio: %s", self.label, e)
        except Exception:
            self.winding_down.set()
            logger.exception("[%s] capture writer failed", self.label)

    def stop(self) -> None:
        with self.stop_lock:
            if self.stopped:
                return
            self.stopped = True

            _, _, audio_toolbox = frameworks()
            self.winding_down.set()
            # Synchronous: the callback runs once more for each buffer that is
            # partly filled, so the last fraction of a second is not cut off.
            audio_toolbox.AudioQueueStop(self.queue_ref, True)
            self.chunks.put(None)
            if self.writer is not None and self.writer is not threading.current_thread():
                self.writer.join(DRAIN_TIMEOUT_S)
            audio_toolbox.AudioQueueDispose(self.queue_ref, True)
            try:
                # End of input is what tells ffmpeg the recording is over.
                self.sink.close()
            except OSError:
                pass  # ffmpeg had already gone
