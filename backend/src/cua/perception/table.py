"""Rows and columns, from a flat list of elements.

A table on a screen is a visual convention, not a structure the way it is in a DOM, so reading
one back out of detected boxes is geometry. Kept here rather than beside either consumer
because the scan loop and the resolver both need it: a value in a grid is addressed the same
way whether its row was found by predicate or by anchor.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..schema import Element, Observation
from .index import ElementIndex


def column_span(obs: Observation, name: str, above: float) -> tuple[float, float] | None:
    """Where the column headed `name` starts and ends, horizontally.

    A column's boundaries come from its *header row*, not from the width of any one piece of
    text: the span runs from this header to the next one along. Matching cell to header by
    overlapping text boxes fails whenever a header is left-aligned and its values are
    right-aligned. The first column starts at the left edge and the last ends at the right.
    """
    header = find_header(obs, name, above)
    if header is None:
        return None

    rows = ElementIndex(obs.elements).rows()
    header_row = next((r for r in rows if any(e.id == header.id for e in r)), [header])
    ordered = sorted(header_row, key=lambda e: e.bbox.x)
    index = next(i for i, e in enumerate(ordered) if e.id == header.id)
    start = ordered[index].bbox.x if index > 0 else 0.0
    end = ordered[index + 1].bbox.x if index + 1 < len(ordered) else 1.0
    return (start, end)


def cell_in_column(row: Sequence[Element], span: tuple[float, float]) -> Element | None:
    """The cell of `row` whose centre falls inside the column's span. A column exists in pixels
    no more than a row does — it is the band between one header and the next, and membership is
    decided by where the cell sits, not by how wide its text happens to be."""
    start, end = span
    inside = [c for c in row if start <= c.bbox.center.x < end]
    return min(inside, key=lambda c: c.bbox.x) if inside else None


def find_header(obs: Observation, name: str, above: float) -> Element | None:
    """The column header with this text, nearest above the matched row.

    Above means *starts* higher, not *clears entirely*: detected boxes are taller than the gap
    between rows and overlap by a pixel or two, so requiring a header to clear its row rejects
    it.
    """
    wanted = " ".join(name.casefold().split())
    candidates = [
        e
        for e in obs.elements
        if " ".join((e.text or e.name or "").casefold().split()) == wanted
        and e.bbox.y < above - 1e-6
    ]
    return max(candidates, key=lambda e: e.bbox.y) if candidates else None
