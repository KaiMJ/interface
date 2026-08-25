"""Rows and columns, from a flat list of elements.

A table on a screen is not a structure the way it is in a DOM — it is a visual
convention, and reading one back out of detected boxes is geometry. Kept here
rather than beside either consumer because both the scan loop and the resolver
need it: a value in a grid is addressed the same way whether the row was found by
predicate or by anchor.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..schema import Element, Observation
from .index import ElementIndex


def column_span(obs: Observation, name: str, above: float) -> tuple[float, float] | None:
    """Where the column headed `name` starts and ends, horizontally.

    A column's boundaries are set by its *header row*, not by the width of any one
    piece of text: the span runs from this header to the next one along. Matching a
    cell to a header by overlapping their text boxes looks equivalent and is not —
    measured on the transaction table, the header "Amount" renders left-aligned at
    x=1223 and its values right-aligned at x=1336, inside one column spanning
    1236..1415. The two never overlap, and every read of that column failed.

    The first column starts at the left edge and the last ends at the right, so a
    cell that begins fractionally outside its header still lands in it.
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
    """The cell of `row` whose centre falls inside the column's span.

    A table column is not a thing that exists in pixels any more than a row is —
    it is the vertical band between one header and the next, and which cell is in
    it is decided by where the cell sits, not by how wide its text happens to be.
    """
    start, end = span
    inside = [c for c in row if start <= c.bbox.center.x < end]
    return min(inside, key=lambda c: c.bbox.x) if inside else None


def find_header(obs: Observation, name: str, above: float) -> Element | None:
    """The column header with this text, nearest above the matched row.

    Above means *starts* higher, not *clears entirely*. Detected boxes are taller
    than the gap between rows and overlap by a pixel or two — measured on the
    accounts grid, the header "Current Balance" ends at y=0.2867 and the row under
    it begins at y=0.2856. Requiring the header to clear the row rejected the only
    header there was, and every read of that column fell back to counting cells.
    """
    wanted = " ".join(name.casefold().split())
    candidates = [
        e
        for e in obs.elements
        if " ".join((e.text or e.name or "").casefold().split()) == wanted
        and e.bbox.y < above - 1e-6
    ]
    return max(candidates, key=lambda e: e.bbox.y) if candidates else None
