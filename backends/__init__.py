"""
Capture backends
================

Picks the capture backend for the machine we are running on. `framework` in
config.yaml can force one (useful for testing); left at "auto", the platform
decides, so the setting never has to differ between the Mac and the Windows box.
"""

import sys

from config import Config

from .audioqueue import AudioQueueBackend
from .avfoundation import AVFoundationBackend
from .base import (
    CaptureBackend,
    CaptureInput,
    ChannelNotAvailableError,
    DeviceNotFoundError,
)
from .dshow import DShowBackend

BACKENDS = {
    AudioQueueBackend.framework: AudioQueueBackend,
    AVFoundationBackend.framework: AVFoundationBackend,
    DShowBackend.framework: DShowBackend,
}

FRAMEWORK_BY_PLATFORM = {
    # Not avfoundation: ffmpeg loses audio capturing through it. It can still
    # be asked for by name, for comparison.
    "darwin": AudioQueueBackend.framework,
    "win32": DShowBackend.framework,
}


def get_backend(config: Config) -> CaptureBackend:
    framework = config.framework.lower()

    if framework == "auto":
        if sys.platform not in FRAMEWORK_BY_PLATFORM:
            raise RuntimeError(
                f"No capture backend for platform '{sys.platform}'. Set "
                f"framework in config.yaml to one of: {', '.join(BACKENDS)}"
            )
        framework = FRAMEWORK_BY_PLATFORM[sys.platform]

    if framework not in BACKENDS:
        raise RuntimeError(
            f"Unknown framework '{framework}'. Expected one of: "
            f"{', '.join(BACKENDS)}"
        )

    return BACKENDS[framework](config)


__all__ = [
    "CaptureBackend",
    "CaptureInput",
    "ChannelNotAvailableError",
    "DeviceNotFoundError",
    "get_backend",
]
