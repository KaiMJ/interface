"""What perception produces.

One `Element` type regardless of where it came from. This is the seam that keeps
the artifact format independent of the surface: OmniParser on a screenshot, OCR
text lines, an accessibility node, and a DOM node all normalize into the same
record, and everything downstream (resolver, set-of-marks overlay, replay) only
ever sees `Element`.

Adding a desktop surface means adding an `ElementSource`, not changing the
artifact schema.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field

from .common import Bbox, Frozen, Viewport


class ElementSource(str, Enum):
    OMNIPARSER = "omniparser"   # icon/control detection over pixels
    OCR = "ocr"                 # text line + box
    DOM = "dom"                 # off by default — present for surfaces that have one
    AX = "ax"                   # accessibility tree — off by default


class Element(Frozen):
    """A candidate control or piece of text on the surface.

    `role` and `name` are the semantic handles the resolver prefers; `bbox` is the
    fallback. `source` and `conf` are carried through because a resolution that
    only succeeded via a low-confidence pixel match is a signal worth logging.
    """

    id: str                              # stable within one observation, e.g. "e12"
    role: str | None = None              # button | textbox | link | text | row | ...
    name: str | None = None              # accessible/derived name
    text: str | None = None              # visible text, OCR or otherwise
    bbox: Bbox
    source: ElementSource
    conf: float = Field(ge=0.0, le=1.0, default=1.0)

    @property
    def label(self) -> str:
        """Best available human-readable handle."""
        return self.name or self.text or f"<{self.role or 'element'} {self.id}>"


class Observation(Frozen):
    """One perceive() cycle: a frame plus everything found in it.

    The screenshot is of the whole X display, not of the browser viewport. That is
    deliberate: it is the same image the operator sees over VNC and the same
    coordinate space the input layer clicks in, so there is exactly one coordinate
    system and no translation bugs at the handoff.
    """

    screenshot_path: str
    viewport: Viewport
    elements: tuple[Element, ...] = ()
    url: str | None = None               # convenience only; never used for targeting
    frame_hash: str | None = None        # for settle-detection between frames
    taken_at: str

    def by_id(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)
