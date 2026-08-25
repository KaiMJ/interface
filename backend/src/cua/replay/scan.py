"""The `find_and_act` scan loop. Fully deterministic; no model in it.

    locate scope (by anchor text, not a fixed box)
      └─ loop, bounded by scan.max_advances:
           observe scope region, group into rows, normalize, test predicate
           match -> act / collect;  no match -> advance

Termination is the judgement that matters: exhausting the list is a business outcome,
while hitting `max_advances` with the region still changing is a hard failure, because
"not found" would be a confidently wrong answer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..perception import ElementIndex
from ..resolve import Unresolvable, apply, render
from ..schema import (
    Bbox,
    Element,
    FindAndActStep,
    Observation,
    Point,
    PredicateMatch,
    ScanAdvance,
    ScopeExtent,
    Target,
)


class Untestable(Exception):
    """The predicate cannot be answered against what is on screen.

    Distinct from "no match" on purpose. `cell_equals` against a cell the app
    truncated is not false — it is unanswerable, and returning False would report
    a record as absent because its name was too long to fit in a column.
    """


@dataclass(frozen=True)
class ScanResult:
    matches: list[list[Element]]         # each match is a row (list of elements)
    advances: int
    exhausted: bool                      # region stopped changing -> we saw everything
    inconclusive: bool                   # hit max_advances with content still moving
    # The frame the matches were found on. Carried because a row on its own does
    # not say which column is which — that is a property of the screen it came
    # from, and reading a named cell needs both.
    observation: Observation | None = None


class Scanner:
    def __init__(self, perceiver: Any, driver: Any, resolver: Any) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.resolver = resolver

    def locate_scope(self, step: FindAndActStep, obs: Observation, params: dict[str, Any]) -> Bbox:
        """Resolve the scope anchor and derive the region from `scope_extent`.

        Anchor-based rather than a recorded box so that a banner appearing above
        the table does not silently shift the scan window off the data.
        """
        anchor = self.resolver.resolve(step.scope, obs, params)
        box: Bbox = anchor.bbox
        if step.scope_extent is ScopeExtent.WITHIN:
            return box
        if step.scope_extent is ScopeExtent.ABOVE:
            return Bbox(x=0.0, y=0.0, w=1.0, h=max(0.0, box.y))
        top = min(1.0, box.y + box.h)
        return Bbox(x=0.0, y=top, w=1.0, h=max(0.0, 1.0 - top))

    async def scan(
        self,
        step: FindAndActStep,
        params: dict[str, Any],
        observe: Callable[[], Awaitable[Observation]],
    ) -> ScanResult:
        """Look until found, until the region stops changing, or until the cap.

        `observe` is injected rather than called on the perceiver directly so the
        engine keeps one settle-and-record path — every frame this loop looks at
        lands in evidence like any other.
        """
        matches: list[list[Element]] = []
        seen: set[str] = set()
        collected: set[str] = set()
        found_on: Observation | None = None
        advances = 0

        while True:
            obs = await observe()
            scope = self.locate_scope(step, obs, params)
            rows = ElementIndex(obs.elements).rows(scope)

            for row in rows:
                key = _row_key(row)
                if key in collected or not self._test(row, step, params):
                    continue
                collected.add(key)
                matches.append(row)
                found_on = obs
            if matches and not step.collect_all:
                return ScanResult(matches, advances, False, False, found_on)
            if step.limit is not None and len(matches) >= step.limit:
                return ScanResult(matches, advances, False, False, found_on)

            # Nothing new on screen means we have seen the whole list. This is the
            # only signal that distinguishes "the record is not there" from "we
            # stopped looking", so it is compared against every previous frame
            # rather than only the last: a list that bounces back to the top
            # would otherwise scroll forever.
            signature = _signature(rows)
            if signature in seen:
                return ScanResult(matches, advances, True, False, found_on or obs)
            seen.add(signature)

            if advances >= step.scan.max_advances:
                # Content was still changing when we ran out of budget. We do not
                # know whether the record is absent, and saying "not found" here
                # would be a confidently wrong answer.
                return ScanResult(matches, advances, False, True, found_on or obs)

            if not await self._advance(step, obs, scope, params):
                return ScanResult(matches, advances, True, False, found_on or obs)
            advances += 1

    async def _advance(
        self,
        step: FindAndActStep,
        obs: Observation,
        scope: Bbox,
        params: dict[str, Any],
    ) -> bool:
        """Move to the next screenful. False means there is no next one."""
        if step.scan.advance is ScanAdvance.NONE:
            return False

        if step.scan.advance is ScanAdvance.CLICK_ANCHOR:
            if not step.scan.anchor:
                return False
            target = Target(
                intent=f"advance the list via {step.scan.anchor!r}",
                target_desc="the pagination control",
                anchor_text=step.scan.anchor,
            )
            try:
                found = self.resolver.resolve(target, obs, params)
            except Unresolvable:
                # The "Next" link is gone: that is the end of the list, and it is
                # exhaustion rather than a failure to advance.
                return False
            await self.driver.click(found.point)
            return True

        # Never a full region height. A row straddling the boundary would be
        # skipped, and the run would report a false not-found — the same wrong
        # answer as quitting early, arrived at more subtly.
        await self.driver.scroll(
            Point(x=0.5, y=min(1.0, scope.y + scope.h / 2)),
            scope.h * (1.0 - step.scan.overlap),
        )
        return True

    def _test(self, row: list[Element], step: FindAndActStep, params: dict[str, Any]) -> bool:
        """Evaluate the predicate against one row, with the artifact's normalizers.

        Note the truncation asymmetry: after `strip_ellipsis`, a truncated cell can
        only be compared by prefix. `cell_equals` against a value we know was
        truncated is unanswerable, and must raise rather than quietly return False.
        """
        predicate = step.predicate
        norm = predicate.normalize
        terms = [apply(render(t, params) or "", norm) for t in predicate.terms]
        if not terms:
            return False

        if predicate.match is PredicateMatch.CELL_EQUALS:
            for cell in row:
                raw = (cell.text or cell.name or "").strip()
                value = apply(raw, norm)
                if value != terms[0]:
                    continue
                if _truncated(raw):
                    raise Untestable(
                        f"cell {raw!r} is truncated: it cannot be compared for equality "
                        f"with {terms[0]!r}, only by prefix"
                    )
                return True
            return False

        haystack = apply(" ".join((c.text or c.name or "") for c in row), norm)
        if predicate.match is PredicateMatch.ROW_CONTAINS_ANY:
            return any(t in haystack for t in terms)
        return all(t in haystack for t in terms)


def _truncated(s: str) -> bool:
    return s.rstrip().endswith(("...", "…"))


def _row_key(row: Sequence[Element]) -> str:
    return "|".join((e.text or e.name or "").strip() for e in row)


def _signature(rows: Sequence[Sequence[Element]]) -> str:
    """What is currently in the scope region, independent of where it sits.

    Text rather than pixels: scrolling moves every box by design, so a geometric
    hash would never repeat and the loop would never terminate.
    """
    return hashlib.sha256("\n".join(_row_key(r) for r in rows).encode()).hexdigest()[:16]
