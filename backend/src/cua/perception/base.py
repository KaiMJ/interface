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

import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..calibration import CALIBRATION
from ..clock import monotonic_ms, now_iso
from ..schema import Bbox, Element, Observation, Viewport
from .merge import merge


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


class Unsettled(Exception):
    """The surface never stopped changing within the timeout.

    Terminal for the step. Deliberately not "carry on with the last frame": the
    frames disagreed about where things are, and resolving a coordinate against a
    page that is still laying out is how a click lands on the wrong control.
    """


class Perceiver:
    """Composes a screen, a detector and a text reader into one observation.

    Kept as a concrete class rather than a protocol because the composition itself
    — capture, detect, read, merge, assign stable ids — is the same for every
    surface. Only the three collaborators change.
    """

    def __init__(
        self,
        screen: Screen,
        detector: Detector,
        reader: TextReader,
        merge_iou: float = 0.60,
        containment: float = CALIBRATION.label_containment,
        url_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.screen = screen
        self.detector = detector
        self.reader = reader
        self.merge_iou = merge_iou
        self.containment = containment
        # Supplied by the action layer when the surface happens to have a URL.
        # A callable rather than a value so perception never holds a reference to
        # a browser, and so a desktop surface simply passes nothing.
        self.url_provider = url_provider

    def observe(self, out_path: Path, region: Bbox | None = None) -> Observation:
        """Capture and interpret one frame.

        Contract:
          - `region`, when given, restricts *text reading* only. Detection always
            runs full-frame, because a control partially outside the region still
            matters for overlay detection.
          - Element ids are stable within one observation and meaningless across
            observations. Nothing may persist them.
        """
        viewport, frame_hash = self.screen.capture(out_path)
        return self._interpret(out_path, viewport, frame_hash, region)

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        """Observe repeatedly until two consecutive frames hash-equal.

        Deterministic replacement for `sleep()`. Raises on timeout rather than
        returning a possibly-mid-reflow frame — resolving coordinates against a
        page that is still laying out is how a click lands on the wrong control.
        """
        deadline = monotonic_ms() + timeout_ms
        previous: str | None = None
        while True:
            viewport, frame_hash = self.screen.capture(out_path)
            if previous is not None and frame_hash == previous:
                # Interpret the frame we already have rather than capturing a
                # third time: detection and OCR must describe the exact image
                # whose stability we just established.
                return self._interpret(out_path, viewport, frame_hash, None)
            if monotonic_ms() >= deadline:
                raise Unsettled(
                    f"display still changing after {timeout_ms}ms "
                    f"(last two hashes {previous} -> {frame_hash})"
                )
            previous = frame_hash
            time.sleep(poll_ms / 1000.0)

    # --- internals -----------------------------------------------------------

    def _interpret(
        self,
        image_path: Path,
        viewport: Viewport,
        frame_hash: str,
        region: Bbox | None,
    ) -> Observation:
        controls: list[Element] = self.detector.detect(image_path, viewport)
        texts: list[Element] = self.reader.read(image_path, viewport, region)
        elements = merge(controls, texts, self.merge_iou, self.containment)
        return Observation(
            screenshot_path=str(image_path),
            viewport=viewport,
            elements=tuple(elements),
            url=self.url_provider() if self.url_provider is not None else None,
            frame_hash=frame_hash,
            taken_at=now_iso(),
        )
