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

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..calibration import CALIBRATION
from ..clock import monotonic_ms, now_iso
from ..schema import Bbox, Element, Observation, SettledBy, Viewport
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
        volatile: tuple[str, ...] = (),
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
        # Text that is *expected* to change on this surface while nothing is
        # happening: a session countdown, a clock, a "last refreshed" stamp. Facts
        # about the application, declared in its policy and handed down here,
        # because deciding what counts as motion is perception's job and knowing
        # which lines tick is the application's.
        self._volatile = tuple(re.compile(p) for p in volatile)

    def _reads_the_same(self, a: Observation, b: Observation) -> bool:
        """Do two observations describe the same screen, for settling purposes?"""
        return _reading(a, self._volatile) == _reading(b, self._volatile)

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
        """Observe repeatedly until the surface stops changing.

        Deterministic replacement for `sleep()`. Raises rather than returning a
        possibly-mid-reflow frame — resolving coordinates against a page that is
        still laying out is how a click lands on the wrong control.

        Two definitions of "stopped", tried in that order:

          pixels  two consecutive frames hash-equal. The cheapest correct answer,
                  and on a static enterprise screen it is the one that fires.
          text    two consecutive *observations* whose readable text and control
                  boxes match, ignoring lines the application declares volatile.
                  Costs two extra observations (~4s) and only runs when the pixel
                  test has already given up.

        The fallback exists because a blinking caret, a spinner, a carousel or a
        live clock means no two frames are ever byte-identical, and on such a
        surface the pixel test alone fails every step. Identical pixels was only
        ever a cheap proxy for the property the resolver actually depends on:
        that the words and the boxes have stopped moving.

        A session countdown is the case that defeats *both* tests, and it is not
        exotic — a back-office banking application that declares session expiry as
        a condition is very likely to render the countdown. `14:59` becoming
        `14:58` changes the pixels and changes the text, so the page never settles
        by either measure and every step burns two full timeouts before failing on
        a screen that was ready the whole time. `volatile` is how an application
        says which lines those are; they are excluded from the comparison and from
        nothing else, so a countdown is still readable, still perceivable, and
        still no longer evidence that the page is moving.

        Which one fired is recorded on the observation, because a run that settles
        by text on every step is telling you something about the surface.
        """
        deadline = monotonic_ms() + timeout_ms
        previous: str | None = None
        while True:
            viewport, frame_hash = self.screen.capture(out_path)
            if previous is not None and frame_hash == previous:
                # Interpret the frame we already have rather than capturing a
                # third time: detection and OCR must describe the exact image
                # whose stability we just established.
                return self._interpret(
                    out_path, viewport, frame_hash, None, SettledBy.PIXELS
                )
            if monotonic_ms() >= deadline:
                return self._settle_by_text(out_path, timeout_ms, poll_ms, frame_hash)
            previous = frame_hash
            time.sleep(poll_ms / 1000.0)

    def peek(self, out_path: Path) -> str:
        """The frame's hash, without interpreting it.

        Two orders of magnitude cheaper than an observation — a screen grab and a
        digest against ~2.4s of text recognition on a dense page — and it answers
        one question exactly: *has anything at all changed?*

        That is enough for a poll loop. A checkpoint evaluated against a
        byte-identical frame cannot reach a different verdict, so re-interpreting
        one is work whose answer is already known. This is what makes waiting on a
        slow page cost roughly nothing instead of a full perception per poll.
        """
        return self.screen.capture(out_path)[1]

    def _settle_by_text(
        self, out_path: Path, timeout_ms: int, poll_ms: int, last_hash: str | None
    ) -> Observation:
        """Fallback: settled when what the frame *says* stops changing.

        Bounded by the same budget again rather than by a new one, so the worst
        case is twice the declared timeout and not an open-ended wait. Two
        observations that agree is the minimum: one is not evidence of anything.
        """
        deadline = monotonic_ms() + timeout_ms
        previous: Observation | None = None
        while True:
            current = self.observe(out_path)
            if previous is not None and self._reads_the_same(previous, current):
                return current.model_copy(update={"settled_by": SettledBy.TEXT})
            if monotonic_ms() >= deadline:
                raise Unsettled(
                    f"display still changing after {timeout_ms}ms by pixels "
                    f"(last hash {last_hash}) and a further {timeout_ms}ms by text"
                )
            previous = current
            time.sleep(poll_ms / 1000.0)

    # --- internals -----------------------------------------------------------

    def _interpret(
        self,
        image_path: Path,
        viewport: Viewport,
        frame_hash: str,
        region: Bbox | None,
        settled_by: SettledBy = SettledBy.UNSET,
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
            settled_by=settled_by,
            taken_at=now_iso(),
        )


def _reading(
    obs: Observation, volatile: tuple[re.Pattern[str], ...] = ()
) -> frozenset[tuple[str, int, int]]:
    """A screen reduced to what "has it stopped moving" should be asked about.

    Quantized boxes rather than raw ones: OCR box edges wobble by a pixel or two
    between identical frames, and an exact comparison would report a static page as
    never settling — the failure the text fallback exists to avoid. Quantizing to
    0.5% of the frame (~7px wide, ~4px tall at 1440x900) is well inside a line of
    text and well outside that jitter.

    Declared-volatile lines are dropped entirely rather than blanked, because a
    countdown does not only change its text — it changes its width, and therefore
    the boxes beside it.
    """
    reading = set()
    for element in obs.elements:
        text = (element.text or element.name or "").strip().casefold()
        if any(pattern.search(text) for pattern in volatile):
            continue
        reading.add((text, round(element.bbox.x * 200), round(element.bbox.y * 200)))
    return frozenset(reading)
