"""The perception seam.

`Perceiver.observe()` is the only way anything in this system learns what is on
the screen. Everything above it — resolver, discovery loop, replay engine —
consumes `Observation` and knows nothing about screenshots, OmniParser, OCR, DOM,
or accessibility APIs.

That is the seam §3.7 asks about. Extending to a legacy frameset app or a native
desktop application means writing a new `Detector` / `Screen` pair; the artifact
format, the resolver ladder, and the replay engine are untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..schema import Bbox, Element, Observation, Viewport


@runtime_checkable
class Screen(Protocol):
    """Something that can be photographed."""

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        """Write a screenshot; return its geometry and a content hash.

        The hash is what settle-detection compares between frames.
        """
        ...


@runtime_checkable
class Detector(Protocol):
    """Finds interactable controls. Pixels in, elements out."""

    def detect(self, image_path: Path, viewport: Viewport) -> list[Element]:
        ...


@runtime_checkable
class TextReader(Protocol):
    """Finds text and where it is."""

    def read(
        self, image_path: Path, viewport: Viewport, region: Bbox | None = None
    ) -> list[Element]:
        ...


class Perceiver:
    """Composes a screen, a detector and a text reader into one observation.

    Kept as a concrete class rather than a protocol because the composition itself
    — capture, detect, read, merge, assign stable ids — is the same for every
    surface. Only the three collaborators change.
    """

    def __init__(self, screen: Screen, detector: Detector, reader: TextReader) -> None:
        self.screen = screen
        self.detector = detector
        self.reader = reader

    def observe(self, out_path: Path, region: Bbox | None = None) -> Observation:
        """Capture and interpret one frame.

        Contract:
          - `region`, when given, restricts *text reading* only. Detection always
            runs full-frame, because a control partially outside the region still
            matters for overlay detection.
          - Element ids are stable within one observation and meaningless across
            observations. Nothing may persist them.
        """
        raise NotImplementedError

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        """Observe repeatedly until two consecutive frames hash-equal.

        Deterministic replacement for `sleep()`. Raises on timeout rather than
        returning a possibly-mid-reflow frame — resolving coordinates against a
        page that is still laying out is how a click lands on the wrong control.
        """
        raise NotImplementedError
