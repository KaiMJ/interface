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
    ResolutionTier,
    ScanAdvance,
    ScopeExtent,
    Target,
)


class Untestable(Exception):
    """The predicate cannot be answered against what is on screen.

    Distinct from "no match": `cell_equals` against a cell the app truncated is unanswerable,
    and returning False would report a record as absent because its name was too long.
    """


@dataclass(frozen=True)
class ScanResult:
    # each match is a row (list of elements)
    matches: list[list[Element]]
    advances: int
    exhausted: bool                      # region stopped changing -> we saw everything
    inconclusive: bool                   # hit max_advances with content still moving
    # The weakest tier any scope resolution used. An absence is only evidence about the data
    # if the region was found semantically: fallen through to a recorded box, the loop may
    # have been reading a different table, and "not there" would be about the wrong list.
    scope_tier: ResolutionTier = ResolutionTier.NONE
    # The frame the matches were found on: a row alone does not say which column is which.
    observation: Observation | None = None


class Scanner:
    def __init__(self, perceiver: Any, driver: Any, resolver: Any) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.resolver = resolver

    def locate_scope(
        self, step: FindAndActStep, obs: Observation, params: dict[str, Any]
    ) -> tuple[Bbox, ResolutionTier]:
        """Resolve the scope anchor and derive the region from `scope_extent`. Anchor-based
        rather than a recorded box, so a banner above the table does not shift the scan window
        off the data. The tier is returned with it, because how the region was found decides
        whether an empty scan means anything."""
        anchor = self.resolver.resolve(step.scope, obs, params)
        tier: ResolutionTier = anchor.tier
        box: Bbox = anchor.bbox
        if step.scope_extent is ScopeExtent.WITHIN:
            return box, tier
        if step.scope_extent is ScopeExtent.ABOVE:
            return Bbox(x=0.0, y=0.0, w=1.0, h=max(0.0, box.y)), tier
        top = min(1.0, box.y + box.h)
        return Bbox(x=0.0, y=top, w=1.0, h=max(0.0, 1.0 - top)), tier

    async def scan(
        self,
        step: FindAndActStep,
        params: dict[str, Any],
        observe: Callable[[], Awaitable[Observation]],
    ) -> ScanResult:
        """Look until found, until the region stops changing, or until the cap.

        `observe` is injected rather than called on the perceiver directly so the engine keeps
        one settle-and-record path: every frame this loop sees lands in evidence like any other.
        """
        matches: list[list[Element]] = []
        seen: set[str] = set()
        collected: set[str] = set()
        found_on: Observation | None = None
        advances = 0
        weakest = ResolutionTier.ANCHOR_TEXT

        while True:
            obs = await observe()
            scope, tier = self.locate_scope(step, obs, params)
            weakest = _weaker(weakest, tier)
            rows = ElementIndex(obs.elements).rows(scope)

            for row in rows:
                key = _row_key(row)
                if key in collected or not self._test(row, step, params):
                    continue
                collected.add(key)
                matches.append(row)
                found_on = obs
            if matches and not step.collect_all:
                return ScanResult(
                    matches, advances, False, False,
                    observation=found_on,
                    scope_tier=weakest,
                )
            if step.limit is not None and len(matches) >= step.limit:
                return ScanResult(
                    matches, advances, False, False,
                    observation=found_on,
                    scope_tier=weakest,
                )

            signature = _signature(rows)
            if signature in seen:
                return ScanResult(
                    matches, advances, True, False,
                    observation=found_on or obs,
                    scope_tier=weakest,
                )
            seen.add(signature)

            if advances >= step.scan.max_advances:
                return ScanResult(
                    matches, advances, False, True,
                    observation=found_on or obs,
                    scope_tier=weakest,
                )

            if not await self._advance(step, obs, scope, params):
                return ScanResult(
                    matches, advances, True, False,
                    observation=found_on or obs,
                    scope_tier=weakest,
                )
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
                # The "Next" link is gone: the end of the list, which is exhaustion rather
                # than a failure to advance.
                return False
            await self.driver.click(found.point)
            return True

        # Never a full region height: a row straddling the boundary would be skipped and
        # reported as a false not-found.
        await self.driver.scroll(
            Point(x=0.5, y=min(1.0, scope.y + scope.h / 2)),
            scope.h * (1.0 - step.scan.overlap),
        )
        return True

    def _test(self, row: list[Element], step: FindAndActStep, params: dict[str, Any]) -> bool:
        """Evaluate the predicate against one row, with the artifact's normalizers.

        `strip_ellipsis` is what makes a truncated cell compare equal to a shorter term, so the
        raise below fires exactly when an artifact declares it: the comparison came out True on
        evidence that only supports a prefix match. Without that normalizer the cell simply
        does not match.
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
    """What is currently in the scope region, independent of where it sits. Text rather than
    pixels: scrolling moves every box, so a geometric hash would never repeat."""
    return hashlib.sha256("\n".join(_row_key(r) for r in rows).encode()).hexdigest()[:16]


# Weakest-wins ordering for scope resolution across a multi-frame scan.
_TIER_RANK = {
    ResolutionTier.ANCHOR_TEXT: 3,
    ResolutionTier.ROLE_NAME: 2,
    ResolutionTier.VLM_GATED: 2,
    ResolutionTier.RECORDED_BBOX: 1,
    ResolutionTier.NONE: 0,
}


def _weaker(a: ResolutionTier, b: ResolutionTier) -> ResolutionTier:
    return a if _TIER_RANK.get(a, 0) <= _TIER_RANK.get(b, 0) else b
