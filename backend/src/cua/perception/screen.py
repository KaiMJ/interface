"""X display capture.

We photograph the whole virtual display, not the browser viewport. `page.screenshot()` is
easier and browser-only, which would mean two coordinate spaces the moment a desktop surface
appears — and the operator watches the X display over VNC, so the evidence is what they see.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from PIL import Image

from ..schema import Viewport


class XDisplayScreen:
    """Captures the X display via mss. Implements `perception.base.Screen`."""

    def __init__(self, display: str, viewport: Viewport) -> None:
        self.display = display
        self.viewport = viewport
        # One mss instance per screen, created on first use: constructing one per capture
        # reopens the X connection every frame, measurable at a 120ms settle poll.
        self._sct: Any | None = None

    def _session(self) -> Any:
        if self._sct is None:
            import mss

            # `mss.mss` is the deprecated factory; `MSS` is the class it returns.
            factory = getattr(mss, "MSS", None) or mss.mss
            self._sct = factory(display=self.display)
        return self._sct

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        """Grab the full display, write a PNG, return geometry + content hash.

        The hash is over the raw pixel buffer, not the encoded file: PNG encoders may vary
        their output for identical input, which would make settle-detection never converge.
        """
        sct = self._session()
        # monitors[0] is the union of all screens, [1] the first real one. Single-screen Xvfb.
        shot = sct.grab(sct.monitors[1])
        out_path.parent.mkdir(parents=True, exist_ok=True)
        Image.frombytes("RGB", shot.size, shot.rgb).save(out_path)
        return Viewport(width=shot.width, height=shot.height), self._hash(bytes(shot.bgra))

    def close(self) -> None:
        if self._sct is not None:
            self._sct.close()
            self._sct = None

    @staticmethod
    def _hash(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()[:16]


class ImageFileScreen:
    """Replays a fixed sequence of pre-captured PNGs, so replay, resolver and checkpoint logic
    run without an X server — and the deterministic path can be exercised with no API key."""

    def __init__(self, frames: list[Path], viewport: Viewport) -> None:
        if not frames:
            raise ValueError("ImageFileScreen needs at least one frame")
        self.frames = frames
        self.viewport = viewport
        self._i = 0

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        """Return the current frame; only `advance()` moves the sequence on.

        Capture is not what changes a screen, an action is. Advancing here would let every
        settle poll consume a frame and `settle()` would never see two equal ones in a row. The
        offline driver calls `advance()` when it acts, the same causality the live pair has.
        """
        src = self.frames[self._i]
        img = Image.open(src).convert("RGB")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)
        return (
            Viewport(width=img.width, height=img.height),
            XDisplayScreen._hash(img.tobytes()),
        )

    def advance(self) -> None:
        """Move to the next frame, holding on the last once exhausted. Holding rather than
        wrapping or raising is what lets a replay against a recorded sequence terminate: the
        tail is by definition the state the run ended in."""
        self._i = min(self._i + 1, len(self.frames) - 1)
