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

from typing import Any

from ..calibration import CALIBRATION
from ..schema import Bbox, Element
from .ocr import group_rows


class ElementIndex:
    """Spatial queries over one observation's elements."""

    def __init__(self, elements: tuple[Element, ...]) -> None:
        self.elements = elements
        self._tree: Any | None = None

    # --- plumbing ------------------------------------------------------------

    def _index(self) -> Any:
        """Build the R-tree on first query.

        Lazy because a great many observations are captured and never queried
        spatially — every settle poll, every evidence frame — and building an
        index over a few hundred boxes that nobody asks about is pure cost.
        """
        if self._tree is None:
            from rtree import index as rindex

            tree = rindex.Index(interleaved=True)
            for i, el in enumerate(self.elements):
                b = el.bbox
                tree.insert(i, (b.x, b.y, b.x + b.w, b.y + b.h))
            self._tree = tree
        return self._tree

    def _hits(self, region: Bbox) -> list[Element]:
        """Elements whose box intersects `region` at all."""
        box = (region.x, region.y, region.x + region.w, region.y + region.h)
        return [self.elements[i] for i in self._index().intersection(box)]

    # --- queries -------------------------------------------------------------

    def within(
        self, region: Bbox, containment: float = CALIBRATION.region_containment
    ) -> list[Element]:
        """Elements mostly inside `region`, in reading order.

        Containment rather than intersection: a scope is "the table below this
        header", and a sidebar that merely clips its edge is not in the table.
        """
        found = [el for el in self._hits(region) if el.bbox.contained_by(region) >= containment]
        return sorted(found, key=lambda e: (e.bbox.center.y, e.bbox.x))

    def rows(self, region: Bbox | None = None) -> list[list[Element]]:
        """Elements clustered into horizontal bands, top to bottom."""
        pool = self.within(region) if region is not None else list(self.elements)
        return group_rows(pool)

    def right_of(self, el: Element) -> list[Element]:
        """Everything sharing `el`'s band and starting to its right, nearest first.

        Ordered and unfiltered on purpose. An earlier version bounded this by a
        maximum gap, which is a threshold pretending to be a rule: on a form the
        value sits next to its label, and on a full-width table the next cell in
        the same row can be a third of the screen away. Fixing the target app's
        table widths moved every accounts cell past the bound at once and broke
        reads that had worked for days.

        Distance does not decide which neighbour is meant — the recorded
        `relation_index` does, and it was measured against the same screen.
        """
        edge = el.bbox.x + el.bbox.w
        band = Bbox(x=min(1.0, edge), y=el.bbox.y, w=max(0.0, 1.0 - edge), h=el.bbox.h)
        found = [
            other
            for other in self._hits(band)
            if other.id != el.id
            and other.bbox.x + 1e-6 >= edge
            and _shares_band(other.bbox, el.bbox)
        ]
        return sorted(found, key=lambda e: e.bbox.x)

    def left_of(self, el: Element) -> list[Element]:
        """Everything sharing `el`'s band and ending to its left, nearest first.

        The mirror of `right_of`, and the one recording needs: a value has no
        stable text of its own — a balance changes — so what identifies it is the
        label beside it.
        """
        edge = el.bbox.x
        band = Bbox(x=0.0, y=el.bbox.y, w=edge, h=el.bbox.h)
        found = [
            other
            for other in self._hits(band)
            if other.id != el.id
            and other.bbox.x + other.bbox.w <= edge + 1e-6
            and _shares_band(other.bbox, el.bbox)
        ]
        return sorted(found, key=lambda e: -(e.bbox.x + e.bbox.w))

    def below(self, el: Element) -> list[Element]:
        """Everything under `el`, in reading order.

        The scope of a `find_and_act` is anchored on a column-header row and
        extends downward, so "below" is a scope constructor, not a nicety.
        """
        top = el.bbox.y + el.bbox.h
        region = Bbox(x=0.0, y=min(1.0, top), w=1.0, h=max(0.0, 1.0 - top))
        found = [other for other in self._hits(region) if other.bbox.center.y > top]
        return sorted(found, key=lambda e: (e.bbox.center.y, e.bbox.x))

    def overlapping(self, region: Bbox, min_iou: float) -> list[Element]:
        """Used by overlay detection: is something sitting on top of my target?

        An unexpected modal moves nothing — it lands on top. The recorded
        coordinate is still 'correct' and the click hits the dialog. Detecting that
        requires knowing what is at the point, not just where the point is.
        """
        found = [el for el in self._hits(region) if el.bbox.iou(region) >= min_iou]
        return sorted(found, key=lambda e: -e.bbox.iou(region))


def _shares_band(a: Bbox, b: Bbox) -> bool:
    """Are these two boxes on the same visual line?

    A fraction of the shorter box rather than "overlaps at all" — see
    `Calibration.band_overlap` for the measurement and for what a strict-overlap
    test got wrong.
    """
    overlap = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    shorter = min(a.h, b.h)
    return shorter > 0 and overlap / shorter >= CALIBRATION.band_overlap
