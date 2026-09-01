"""What perception produces: one `Element` type regardless of where it came from.

The seam that keeps the artifact format independent of the surface. An OmniParser icon, an OCR
text line, an accessibility node and a DOM node all normalize into the same record, and
everything downstream — resolver, set-of-marks overlay, replay — sees only `Element`. Adding a
desktop surface means adding an `ElementSource`, not changing the schema.
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


class SettledBy(str, Enum):
    """How the surface was judged to have stopped changing.

    Recorded rather than inferred: a run settling by TEXT on every step is telling you the
    surface animates, which is worth knowing before anyone reaches for the thresholds.
    """

    PIXELS = "pixels"   # two consecutive frames byte-identical
    TEXT = "text"       # pixels never converged; readable text and boxes did
    UNSET = "unset"     # a single un-settled observe()


class Element(Frozen):
    """A candidate control or piece of text on the surface.

    `role` and `name` are the semantic handles the resolver prefers, `bbox` the fallback.
    `source` and `conf` ride along because a resolution that only succeeded on a
    low-confidence pixel match is worth logging.
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

    The screenshot is the whole X display, not the browser viewport — the same image the
    operator sees over VNC and the same space the input layer clicks in, so there is one
    coordinate system and no translation bugs at the handoff.
    """

    screenshot_path: str
    viewport: Viewport
    elements: tuple[Element, ...] = ()
    url: str | None = None               # convenience only; never used for targeting
    frame_hash: str | None = None        # for settle-detection between frames
    settled_by: SettledBy = SettledBy.UNSET
    taken_at: str

    def by_id(self, element_id: str) -> Element | None:
        return next((e for e in self.elements if e.id == element_id), None)
