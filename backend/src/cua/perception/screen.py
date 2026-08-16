"""X display capture.

We photograph the whole virtual display, not the browser viewport.

`page.screenshot()` would be easier and higher fidelity, and it is the wrong
choice here for two reasons. First, it is browser-only — the moment a desktop
surface enters the picture there are two capture paths and two coordinate spaces.
Second, and more immediately: the operator sees the X display over VNC, so if the
model reasons about a different image than the human sees, every escalation
becomes an argument about which picture was right.

One display, one image, one coordinate space.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ..schema import Viewport


class XDisplayScreen:
    """Captures the X display via mss. Implements `perception.base.Screen`."""

    def __init__(self, display: str, viewport: Viewport) -> None:
        self.display = display
        self.viewport = viewport

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        """Grab the full display, write a PNG, return geometry + content hash.

        The hash is over the raw pixel buffer, not the encoded file: PNG encoders
        are allowed to vary their output for identical input, which would make
        settle-detection never converge.
        """
        raise NotImplementedError

    @staticmethod
    def _hash(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()[:16]


class ImageFileScreen:
    """Replays a fixed sequence of pre-captured PNGs.

    Exists so replay, resolver and checkpoint logic can be tested without an X
    server, and so a reviewer with no API key can exercise the deterministic path
    against recorded frames.
    """

    def __init__(self, frames: list[Path], viewport: Viewport) -> None:
        self.frames = frames
        self.viewport = viewport
        self._i = 0

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        raise NotImplementedError
