"""X display capture.

We photograph the whole virtual display, not the browser viewport. `page.screenshot()`
would be easier and is browser-only, which means two capture paths and two coordinate
spaces the moment a desktop surface appears — and the operator sees the X display over
VNC, so the evidence should be the picture they are looking at.
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
        # One mss instance per screen, created on first use. Constructing one per
        # capture reopens the X connection every frame, which at a 120ms settle
        # poll is a measurable share of the loop.
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

        The hash is over the raw pixel buffer, not the encoded file: PNG encoders
        are allowed to vary their output for identical input, which would make
        settle-detection never converge.
        """
        sct = self._session()
        # monitors[0] is the union of all screens; monitors[1] is the first real
        # one. We run a single-screen Xvfb, so [1] is the display.
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
    """Replays a fixed sequence of pre-captured PNGs.

    Exists so replay, resolver and checkpoint logic can be tested without an X
    server, and so a reviewer with no API key can exercise the deterministic path
    against recorded frames.
    """

    def __init__(self, frames: list[Path], viewport: Viewport) -> None:
        if not frames:
            raise ValueError("ImageFileScreen needs at least one frame")
        self.frames = frames
        self.viewport = viewport
        self._i = 0

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        """Return the current frame. Only `advance()` moves the sequence on.

        Capture is not what changes a screen — an action is. Advancing here would
        mean every settle poll consumed a frame, and `settle()` would never see
        two equal ones in a row. The offline driver calls `advance()` when it
        "acts", which is the same causality the live pair has.
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
        """Move to the next frame, holding on the last one once exhausted.

        Holding rather than wrapping or raising is what makes a replay against a
        recorded sequence terminate: the tail of the sequence is by definition the
        state the run ended in.
        """
        self._i = min(self._i + 1, len(self.frames) - 1)
