"""The `find_and_act` scan loop.

A record's position in a list is a function of the data, so the predicate is recorded rather
than a scroll-and-click.

The load-bearing behaviour is termination. "I scanned the whole list and it is not there" is an
answer the caller acts on; "I ran out of budget while the list was still moving" is not, and
reporting the second as the first is the mistake the brief calls out by name.
"""

from __future__ import annotations

from typing import Any

import pytest
from fakes import VIEWPORT, el

from cua.perception import cell_in_column, column_span, find_header
from cua.replay.scan import Scanner, Untestable
from cua.resolve import Resolver
from cua.schema import (
    FindAndActStep,
    MatchMode,
    Normalizer,
    Observation,
    Predicate,
    Primitive,
    Scan,
    ScanAdvance,
    Target,
)


def screen(*rows: list[tuple[str, float, float]], hash_: str = "h") -> Observation:
    """A screen of rows, each cell given as (text, x, width) in normalized units."""
    elements = []
    for r, row in enumerate(rows):
        for c, (text, x, w) in enumerate(row):
            elements.append(el(f"e{r}-{c}", x, 0.2 + r * 0.04, w, 0.02, text))
    return Observation(
        screenshot_path="/nonexistent.png",
        viewport=VIEWPORT,
        elements=tuple(elements),
        url="http://targetapp:8080/members/12345",
        frame_hash=hash_,
        taken_at="2026-08-16T00:00:00+00:00",
    )


HEADERS = [("Date", 0.02, 0.05), ("Description", 0.29, 0.09), ("Amount", 0.85, 0.05)]


def step(**overrides: Any) -> FindAndActStep:
    base = FindAndActStep(
        id=2,
        scope=Target(
            intent="the transaction table",
            target_desc="rows below the headers",
            anchor_text="Description",
            anchor_match=MatchMode.EXACT,
        ),
        predicate=Predicate(match="row_contains_all", terms=("PACIFIC WIRELESS",)),
        scan=Scan(advance=ScanAdvance.SCROLL, overlap=0.15, max_advances=3),
        on_found_action=Primitive.EXTRACT,
        on_found_extract_as="amount",
        on_found_extract_column="Amount",
        on_not_found_outcome="transaction_not_found",
    )
    return base.model_copy(update=overrides)


class Screens:
    """Scripted frames plus the driver the scanner advances through them."""

    def __init__(self, frames: list[Observation]) -> None:
        self.frames = frames
        self.index = 0
        self.scrolls: list[float] = []

    async def observe(self) -> Observation:
        return self.frames[min(self.index, len(self.frames) - 1)]

    async def scroll(self, p: Any, dy: float) -> None:
        self.scrolls.append(dy)
        self.index += 1

    async def click(self, p: Any, button: str = "left") -> None:
        self.index += 1


def scanner(screens: Screens) -> Scanner:
    return Scanner(perceiver=None, driver=screens, resolver=Resolver(allow_vlm=False))


# ---------------------------------------------------------------------------
# finding
# ---------------------------------------------------------------------------


async def test_a_match_below_the_fold_is_found_by_advancing() -> None:
    screens = Screens(
        [
            screen(HEADERS, [("08/14", 0.02, 0.05), ("NORTHGATE GROCERY", 0.29, 0.12)], hash_="a"),
            screen(HEADERS, [("08/10", 0.02, 0.05), ("PACIFIC WIRELESS", 0.29, 0.10)], hash_="b"),
        ]
    )
    found = await scanner(screens).scan(step(), {}, screens.observe)

    assert len(found.matches) == 1
    assert found.advances == 1
    # Never a whole region height: a row straddling the boundary would be skipped
    # and reported as a false not-found.
    assert screens.scrolls and screens.scrolls[0] < 0.8


async def test_seeing_the_whole_list_without_a_match_is_exhaustion() -> None:
    # The region stops changing, so we know we have seen everything. That is an
    # answer, and the engine turns it into the declared business outcome.
    same = screen(HEADERS, [("08/14", 0.02, 0.05), ("NORTHGATE GROCERY", 0.29, 0.12)], hash_="a")
    screens = Screens([same, same, same])
    found = await scanner(screens).scan(step(), {}, screens.observe)

    assert found.matches == []
    assert found.exhausted is True
    assert found.inconclusive is False


async def test_running_out_of_budget_while_the_list_moves_is_not_an_answer() -> None:
    # Every advance shows something new and the budget runs out, so whether the record is
    # absent is unknown and the engine reports SCAN_INCONCLUSIVE.
    screens = Screens(
        [
            screen(HEADERS, [(f"ROW {i}", 0.29, 0.10)], hash_=f"h{i}")
            for i in range(12)
        ]
    )
    found = await scanner(screens).scan(step(), {}, screens.observe)

    assert found.matches == []
    assert found.inconclusive is True
    assert found.exhausted is False
    assert found.advances == 3  # the artifact's declared max_advances


async def test_a_list_that_bounces_back_to_the_top_still_terminates() -> None:
    # Compared against every frame seen, not just the last: a list that bounces back to a
    # screen already visited would otherwise scroll forever.
    a = screen(HEADERS, [("ROW A", 0.29, 0.10)], hash_="a")
    b = screen(HEADERS, [("ROW B", 0.29, 0.10)], hash_="b")
    screens = Screens([a, b, a, b, a, b])
    found = await scanner(screens).scan(step(), {}, screens.observe)

    assert found.exhausted is True
    assert found.advances < 3


async def test_every_matching_row_is_collected_when_asked() -> None:
    screens = Screens(
        [
            screen(
                HEADERS,
                [("08/14", 0.02, 0.05), ("HARBORVIEW PROPERTY MGMT", 0.29, 0.16)],
                [("08/01", 0.02, 0.05), ("HARBORVIEW PROPERTY MGMT", 0.29, 0.16)],
                hash_="a",
            )
        ]
    )
    found = await scanner(screens).scan(
        step(
            predicate=Predicate(terms=("HARBORVIEW PROPERTY MGMT",)),
            collect_all=True,
        ),
        {},
        screens.observe,
    )
    assert len(found.matches) == 2


async def test_the_predicate_is_parameterized_by_the_callers_inputs() -> None:
    screens = Screens(
        [screen(HEADERS, [("08/10", 0.02, 0.05), ("PACIFIC WIRELESS", 0.29, 0.10)], hash_="a")]
    )
    found = await scanner(screens).scan(
        step(predicate=Predicate(terms=("{{merchant}}",))),
        {"merchant": "PACIFIC WIRELESS"},
        screens.observe,
    )
    assert len(found.matches) == 1


def test_equality_against_a_truncated_cell_is_unanswerable() -> None:
    # After strip_ellipsis a truncated value can only be compared by prefix; returning False
    # would report a record as absent because its name was too long for the column.
    row = [el("e0", 0.29, 0.2, 0.16, 0.02, "CROSSROADS HARDWARE & SUPPL...")]
    predicate = Predicate(
        match="cell_equals",
        terms=("CROSSROADS HARDWARE & SUPPL",),
        normalize=(Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS, Normalizer.STRIP_ELLIPSIS),
    )
    with pytest.raises(Untestable):
        Scanner(None, None, Resolver())._test(row, step(predicate=predicate), {})


# ---------------------------------------------------------------------------
# reading a named column out of a matched row
# ---------------------------------------------------------------------------


def test_a_column_is_the_span_between_its_header_and_the_next() -> None:
    # Real geometry from the transaction table: the header "Amount" renders left-aligned at
    # x=1223 and its values right-aligned at x=1336 inside one column, so their text boxes
    # never overlap and matching header-to-cell by overlap reads the wrong column.
    obs = screen(
        [("Date", 22 / 1440, 43 / 1440), ("Amount", 1223 / 1440, 57 / 1440)],
        [("08/10/2026", 25 / 1440, 73 / 1440), ("($441.56)", 1336 / 1440, 61 / 1440)],
    )
    row = [e for e in obs.elements if e.id.startswith("e1")]
    span = column_span(obs, "Amount", above=min(e.bbox.y for e in row))

    assert span is not None
    cell = cell_in_column(row, span)
    assert cell is not None
    assert cell.text == "($441.56)"


def test_a_header_below_the_row_is_not_that_rows_header() -> None:
    obs = screen(HEADERS, [("08/10", 0.02, 0.05), ("PACIFIC WIRELESS", 0.29, 0.10)])
    # Nothing above the header row itself, so a lookup anchored above it finds
    # nothing rather than reaching down the page for a match.
    assert find_header(obs, "Amount", above=0.0) is None


def test_a_header_whose_box_overlaps_the_row_is_still_its_header() -> None:
    """Real geometry, taken off a live frame.

    Detected boxes are taller than the gap between rows, so a header's box ends below the top
    of the row beneath it — here by about a pixel. Requiring the header to clear the row
    rejects the only header there is.
    """
    obs = Observation(
        screenshot_path="/nonexistent.png",
        viewport=VIEWPORT,
        url="http://targetapp:8080/members/22841",
        frame_hash="h",
        taken_at="2026-08-16T00:00:00+00:00",
        elements=(
            el("h0", 0.017, 0.2622, 0.044, 0.0244, "Account"),
            el("h1", 0.490, 0.2622, 0.071, 0.0244, "Current Balance"),
            el("h2", 0.679, 0.2630, 0.076, 0.0244, "Available Balance"),
            el("c0", 0.017, 0.2880, 0.033, 0.0244, "30117"),
            # y=0.2856 is the row's top edge, and it sits above the header's bottom
            # at 0.2866 — the overlap this test exists for.
            el("c3", 0.126, 0.2856, 0.044, 0.0311, "Checking"),
            el("c1", 0.638, 0.2878, 0.039, 0.0267, "$712.04"),
            el("c2", 0.842, 0.2889, 0.038, 0.0244, "$1,020.00"),
        ),
    )
    row = [e for e in obs.elements if e.id.startswith("c")]
    header = obs.elements[1]
    # The overlap is the whole point: the header ends below where the row starts.
    assert min(e.bbox.y for e in row) < header.bbox.y + header.bbox.h

    span = column_span(obs, "Current Balance", above=min(e.bbox.y for e in row))
    assert span is not None
    cell = cell_in_column(row, span)
    assert cell is not None
    assert cell.text == "$712.04"
