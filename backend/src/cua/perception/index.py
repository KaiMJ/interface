"""Spatial index over an observation — the replay-time view.

Discovery asks "show me everything" (set-of-marks). Replay asks pointed questions:
what is inside this scope, right of this anchor, sharing this band, stacked on this
target. Linear scans are fine once; a `find_and_act` over ten screens does it
thousands of times.
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
        """Built on first query. Most observations — settle polls, evidence
        frames — are never queried spatially."""
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

        Containment, not intersection: a sidebar clipping the table's edge is not
        in the table.
        """
        found = [el for el in self._hits(region) if el.bbox.contained_by(region) >= containment]
        return sorted(found, key=lambda e: (e.bbox.center.y, e.bbox.x))

    def rows(self, region: Bbox | None = None) -> list[list[Element]]:
        """Elements clustered into horizontal bands, top to bottom."""
        pool = self.within(region) if region is not None else list(self.elements)
        return group_rows(pool)

    def right_of(self, el: Element) -> list[Element]:
        """Everything sharing `el`'s band and starting to its right, nearest first.

        Unfiltered on purpose. A maximum-gap bound is a threshold pretending to be
        a rule — a form value sits beside its label, a table cell a third of the
        screen away — and widening the target app's table moved every accounts cell
        past it at once. `relation_index` decides which neighbour is meant.
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
        """Mirror of `right_of`, and the one recording needs: a balance has no
        stable text of its own, so what identifies it is the label beside it."""
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
        """Everything under `el`, in reading order. A `find_and_act` scope is
        anchored on a header row and extends down, so this is a scope constructor."""
        top = el.bbox.y + el.bbox.h
        region = Bbox(x=0.0, y=min(1.0, top), w=1.0, h=max(0.0, 1.0 - top))
        found = [other for other in self._hits(region) if other.bbox.center.y > top]
        return sorted(found, key=lambda e: (e.bbox.center.y, e.bbox.x))

    def overlapping(self, region: Bbox, min_iou: float) -> list[Element]:
        """Overlay detection: is something sitting on top of my target?

        A modal moves nothing — it lands on top, the recorded coordinate is still
        "correct", and the click hits the dialog.
        """
        found = [el for el in self._hits(region) if el.bbox.iou(region) >= min_iou]
        return sorted(found, key=lambda e: -e.bbox.iou(region))


def _shares_band(a: Bbox, b: Bbox) -> bool:
    """Same visual line? A fraction of the shorter box, not "overlaps at all" —
    `Calibration.band_overlap` has the measurement."""
    overlap = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    shorter = min(a.h, b.h)
    return shorter > 0 and overlap / shorter >= CALIBRATION.band_overlap
