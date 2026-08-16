"""Spatial index over an observation — the replay-time view.

Discovery asks "show me everything" (set-of-marks). Replay asks pointed spatial
questions many times per step:

  - which elements are inside this scope region?
  - what is immediately to the right of the cell matching this anchor?
  - which elements share a horizontal band with this one (i.e. form a row)?
  - is anything overlapping and above the target I am about to click?

Linear scans over a few hundred elements are fine once, but a `find_and_act` over
ten screens does this thousands of times. An R-tree keeps it flat.
"""

from __future__ import annotations

from ..schema import Bbox, Element


class ElementIndex:
    """Spatial queries over one observation's elements."""

    def __init__(self, elements: tuple[Element, ...]) -> None:
        self.elements = elements
        self._tree: object | None = None

    def within(self, region: Bbox, containment: float = 0.6) -> list[Element]:
        raise NotImplementedError

    def rows(self, region: Bbox | None = None) -> list[list[Element]]:
        """Elements clustered into horizontal bands, top to bottom."""
        raise NotImplementedError

    def right_of(self, el: Element, max_gap: float = 0.25) -> list[Element]:
        raise NotImplementedError

    def below(self, el: Element) -> list[Element]:
        raise NotImplementedError

    def overlapping(self, region: Bbox, min_iou: float = 0.05) -> list[Element]:
        """Used by overlay detection: is something sitting on top of my target?

        An unexpected modal moves nothing — it lands on top. The recorded
        coordinate is still 'correct' and the click hits the dialog. Detecting that
        requires knowing what is at the point, not just where the point is.
        """
        raise NotImplementedError
