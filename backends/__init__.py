"""
Capture backends
================

Picks the right capture backend for the machine we are running on. Set
`framework` in config.yaml to force one (useful for testing), or leave it at
"auto" to let the platform decide — the same config file then works on both
the Mac and the Windows box.
"""

import sys

from config import Config

from .avfoundation import AVFoundationBackend
from .base import (
    CaptureBackend,
    CaptureInput,
    ChannelNotAvailableError,
    DeviceNotFoundError,
)
from .dshow import DShowBackend

BACKENDS = {
    AVFoundationBackend.framework: AVFoundationBackend,
    DShowBackend.framework: DShowBackend,
}

FRAMEWORK_BY_PLATFORM = {
    "darwin": AVFoundationBackend.framework,
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
