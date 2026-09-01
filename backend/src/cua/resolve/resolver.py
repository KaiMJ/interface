"""Target -> coordinate. The semantic-to-pixel ladder.

    anchor_text hit ────────────────► matched bbox + recorded offset
        │ miss
    role + name match ──────────────► element bbox
        │ miss
    recorded bbox ──────────────────► recorded bbox   (+ drift event)

Most portable first, and the winning tier is recorded on every step, so anchors decaying into
`recorded_bbox` are an early warning long before a hard failure. A fourth VLM rung exists as an
enum value and returns nothing, which makes its absence checkable on the replay path.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..perception import ElementIndex, cell_in_column, column_span
from ..schema import (
    Bbox,
    Element,
    MatchMode,
    Observation,
    Point,
    Relation,
    ResolutionTier,
    ResolutionTrace,
    Target,
    TierAttempt,
)
from .normalize import apply
from .template import render


@dataclass(frozen=True)
class Resolution:
    point: Point
    bbox: Bbox
    tier: ResolutionTier
    matched_text: str | None = None
    candidates: int = 1          # how many elements the tier had to choose between
    drift: bool = False          # true when a lower tier than anchor_text was used
    # Whether that choice was a real one. `candidates` alone is the wrong signal: the default
    # match mode is `contains`, so "Search" counts both the button and the heading "Member
    # Search". This is the count *after* `_narrow`.
    ambiguous: bool = False


class Unresolvable(Exception):
    """No tier produced a coordinate. Always terminal for the step."""


class Resolver:
    """Stateless. Observation + target in, coordinate out.

    `allow_vlm` is a constructor argument, not a per-call flag, so replay's resolver is
    structurally incapable of reaching a model rather than merely promising not to.
    """

    def __init__(self, allow_vlm: bool = False) -> None:
        self.allow_vlm = allow_vlm

    def resolve(
        self,
        target: Target,
        obs: Observation,
        params: dict[str, object] | None = None,
    ) -> Resolution:
        """Walk the ladder. `params` fills `{{placeholders}}` in `anchor_text`.

        Raises rather than returning the recorded bbox as a last-ditch guess: a step that
        cannot find its target does not know where it is, and clicking anyway is how a banking
        automation submits the wrong form.
        """
        return self.resolve_traced(target, obs, params)[0]

    def resolve_traced(
        self,
        target: Target,
        obs: Observation,
        params: dict[str, object] | None = None,
    ) -> tuple[Resolution, ResolutionTrace]:
        """The same walk, with a record of every rung. The winning tier alone cannot say why an
        anchor missed: text gone and text matching three elements both arrive as
        `recorded_bbox`."""
        p = params or {}
        attempts: list[TierAttempt] = []
        found = self._by_anchor_text(target, obs, p, attempts)
        # A `{{param}}` in the anchor asks about *data*; the lower rungs answer about
        # *position*. The recorded box is where the recording's row sat, so falling through
        # would read a neighbouring record with full confidence. Not found means not on this
        # screen, which is information.
        if found is None and not _is_data_dependent(target):
            found = self._by_role_name(target, obs, attempts) or self._by_recorded_bbox(
                target, obs, attempts
            )
        if found is None and self.allow_vlm:
            found = self._by_vlm(target, obs)
        if found is None:
            raise Unresolvable(
                f"no tier located {render(target.target_desc, p)!r} "
                f"(anchor={render(target.anchor_text, p)!r} role={target.role!r} "
                f"name={target.name!r} bbox={'yes' if target.bbox else 'no'})"
            )
        return found, _trace(target, p, attempts, found)

    # --- tiers ---------------------------------------------------------------

    def _by_anchor_text(
        self,
        target: Target,
        obs: Observation,
        params: dict[str, object],
        attempts: list[TierAttempt] | None = None,
    ) -> Resolution | None:
        """Most portable tier: survives rebranding, relayout and per-tenant skins. Ambiguity is
        reported rather than silently resolved — on a write, acting on the wrong record is
        unrecoverable."""
        note = _noting(attempts, ResolutionTier.ANCHOR_TEXT)
        if not target.anchor_text:
            return note("skipped", detail="the step's target declares no anchor text")
        needle = apply(render(target.anchor_text, params) or "", target.normalize)
        if not needle:
            # Not the same as declaring no anchor. The step said *find this text* and there is
            # none, so there is nothing to degrade to: role_name matches any text element, and
            # the recorded box is where another record sat.
            note("miss", detail="the anchor rendered empty against these inputs")
            raise Unresolvable(
                f"{target.anchor_text!r} rendered empty against these inputs; "
                f"the step cannot say what it is looking for"
            )

        matches = [
            el
            for el in obs.elements
            if _matches(apply(_label(el), target.normalize), needle, target.anchor_match)
        ]
        # A declared role narrows the field but never decides alone: `infer_role` is a
        # geometric guess, and a bad one must not veto a correct text match.
        if target.role:
            narrowed = [el for el in matches if el.role == target.role]
            if narrowed:
                matches = narrowed
        if not matches:
            return note("miss", detail=f"no element on this frame reads {needle!r}")
        raw = len(matches)
        matches = _narrow(matches, needle, target)

        anchor = self._pick(matches, target)
        best = _follow(anchor, target, obs)
        if best is None:
            return note(
                "miss",
                candidates=raw,
                matched_text=_label(anchor) or None,
                detail=(
                    f"matched the anchor but found no element {target.relation.value} "
                    f"of it at index {target.relation_index}"
                ),
            )
        note("matched", candidates=raw, matched_text=_label(anchor) or None)
        return Resolution(
            point=point_in(best.bbox, target.offset),
            bbox=best.bbox,
            tier=ResolutionTier.ANCHOR_TEXT,
            matched_text=_label(anchor) or None,
            candidates=raw,
            ambiguous=len(matches) > 1,
        )

    def _by_role_name(
        self, target: Target, obs: Observation, attempts: list[TierAttempt] | None = None
    ) -> Resolution | None:
        note = _noting(attempts, ResolutionTier.ROLE_NAME)
        if not target.role and not target.name:
            return note("skipped", detail="the step's target declares neither role nor name")
        # Name is the key; role only narrows it, as in `_by_anchor_text`. With no name, role
        # is the only key there is and has to stand alone.
        matches = [
            el
            for el in obs.elements
            if not target.name
            or apply(el.name or "", target.normalize) == apply(target.name, target.normalize)
        ]
        if target.role:
            narrowed = [el for el in matches if el.role == target.role]
            if narrowed or not target.name:
                matches = narrowed
        if not matches:
            return note(
                "miss",
                detail=(
                    f"nothing on this frame is a {target.role or 'element'} "
                    f"named {target.name!r}"
                ),
            )

        anchor = self._pick(matches, target)
        best = _follow(anchor, target, obs)
        if best is None:
            return note(
                "miss",
                candidates=len(matches),
                detail=f"no element {target.relation.value} of the match",
            )
        note("matched", candidates=len(matches), matched_text=_label(anchor) or None)
        return Resolution(
            point=point_in(best.bbox, target.offset),
            bbox=best.bbox,
            tier=ResolutionTier.ROLE_NAME,
            matched_text=_label(anchor) or None,
            candidates=len(matches),
            # Still a semantic tier: it matched something the frame says, so it is not drift.
            drift=False,
            # `name` is compared exactly, so more than one match is more than one candidate.
            ambiguous=len(matches) > 1,
        )

    def _by_recorded_bbox(
        self, target: Target, obs: Observation, attempts: list[TierAttempt] | None = None
    ) -> Resolution | None:
        """Always `drift=True`: aggregated across runs, this is the drift signal."""
        note = _noting(attempts, ResolutionTier.RECORDED_BBOX)
        if target.bbox is None:
            return note("skipped", detail="the recording carries no box for this target")
        note("matched", detail="fell through to the recorded position — a drift event")
        return Resolution(
            point=point_in(target.bbox, target.offset),
            bbox=target.bbox,
            tier=ResolutionTier.RECORDED_BBOX,
            matched_text=None,
            candidates=1,
            drift=True,
        )

    def _by_vlm(self, target: Target, obs: Observation) -> Resolution | None:
        """A seam rather than an implementation (REPORT §7), and unreached today: no resolver
        anywhere is built with `allow_vlm=True`.

        Discovery does not need it — the model picks from enumerated marks — and replay must
        not have it. The rung is named so that "replay never calls a model" is checkable.
        """
        if not self.allow_vlm:
            # A programming error, not a resolution outcome, so it must be loud rather than
            # degrade into a failed step.
            raise RuntimeError("VLM resolution requested on a resolver built without it")
        return None

    # --- shared --------------------------------------------------------------

    def _pick(self, matches: list[Element], target: Target) -> Element:
        """Choose among equally valid matches: nearest the recorded position, then smallest
        box. Ambiguity is still reported, so using the hint costs nothing."""
        if len(matches) == 1:
            return matches[0]
        if target.bbox is not None:
            anchor = target.bbox.center
            return min(
                matches,
                key=lambda el: (el.bbox.center.x - anchor.x) ** 2
                + (el.bbox.center.y - anchor.y) ** 2,
            )
        return min(matches, key=lambda el: el.bbox.w * el.bbox.h)


def _noting(
    attempts: list[TierAttempt] | None, tier: ResolutionTier
) -> Callable[..., None]:
    """Record one rung's outcome and return None, so a tier can `return note(...)` and no rung
    can be added that forgets to say what it did."""

    def note(
        outcome: str,
        candidates: int = 0,
        matched_text: str | None = None,
        detail: str | None = None,
    ) -> None:
        if attempts is not None:
            attempts.append(
                TierAttempt(
                    tier=tier,
                    outcome=outcome,
                    candidates=candidates,
                    matched_text=matched_text,
                    detail=detail,
                )
            )
        return None

    return note


def _trace(
    target: Target,
    params: dict[str, object],
    attempts: list[TierAttempt],
    found: Resolution,
) -> ResolutionTrace:
    return ResolutionTrace(
        # Rendered, not the template: the trace has to name the string looked for.
        target_desc=render(target.target_desc, params),
        anchor_text=render(target.anchor_text, params) if target.anchor_text else None,
        relation=target.relation.value,
        attempts=tuple(attempts),
        tier=found.tier,
        candidates=found.candidates,
        drift=found.drift,
        bbox=found.bbox,
        point=(found.point.x, found.point.y),
    )


def _is_data_dependent(target: Target) -> bool:
    """Does this target identify a record rather than a control?

    Read off the anchor, where a placeholder is the recording saying "the row for whatever the
    caller passed". "The Transfer button" is always there; "the row for member 22841" exists
    for some inputs and not others, and the two want opposite behaviour on a miss.
    """
    return "{{" in (target.anchor_text or "")


def _follow(anchor: Element, target: Target, obs: Observation) -> Element | None:
    """Step from the element that carries the words to the one being acted on.

    Returns None rather than falling back to the anchor when the neighbour is missing: typing
    into a label because the field beside it could not be found is a silent wrong action.
    """
    if target.relation is Relation.SELF:
        return anchor
    index = ElementIndex(obs.elements)
    if target.column:
        cell = _cell(anchor, target.column, obs, index)
        if cell is not None:
            return cell
        # Fall through to the index: the recording found a header here and this frame does not.
    neighbours = (
        index.right_of(anchor) if target.relation is Relation.RIGHT_OF else index.below(anchor)
    )
    return neighbours[target.relation_index] if len(neighbours) > target.relation_index else None


def _cell(anchor: Element, column: str, obs: Observation, index: ElementIndex) -> Element | None:
    """The cell of the anchor's own row that sits under `column`.

    Content on both axes — the row carries the anchor, the cell sits in the header's band — so
    a blank cell or an extra column does not shift the answer the way a count would.
    """
    row = next((r for r in index.rows() if any(e.id == anchor.id for e in r)), None)
    if not row:
        return None
    span = column_span(obs, column, above=min(e.bbox.y for e in row))
    if span is None:
        return None
    return cell_in_column(row, span)


def _label(el: Element) -> str:
    return (el.text or el.name or "").strip()


def _narrow(matches: list[Element], needle: str, target: Target) -> list[Element]:
    """Drop the matches the anchor did not really mean.

    `contains` is the right default — a balance sits inside "Available Balance: $18,204.55" —
    but it makes the raw match count a bad measure of ambiguity, since "Search" matches the
    button and the heading "Member Search" every time. So an element whose whole label is the
    anchor beats one that merely contains it.

    What survives is the ambiguity worth stopping for: three rows whose buttons all read
    "View", where the recorded position is no evidence either. That is what `find_and_act` is
    for.
    """
    if len(matches) < 2:
        return matches
    exact = [el for el in matches if apply(_label(el), target.normalize) == needle]
    return exact if exact else matches


def _matches(haystack: str, needle: str, mode: MatchMode) -> bool:
    if not haystack:
        return False
    if mode is MatchMode.EXACT:
        return haystack == needle
    if mode is MatchMode.REGEX:
        try:
            return re.search(needle, haystack) is not None
        except re.error:
            # A bad pattern in an artifact is a resolution miss, not a mid-run crash: the step
            # then fails with a legible message.
            return False
    return needle in haystack


def point_in(bbox: Bbox, offset: tuple[float, float]) -> Point:
    """Offset within a resolved box, 0..1; (0.5, 0.5) is the centre. Non-centre offsets target
    the "View" button at the right edge of a matched row without a second resolution pass."""
    return Point(
        x=min(1.0, max(0.0, bbox.x + offset[0] * bbox.w)),
        y=min(1.0, max(0.0, bbox.y + offset[1] * bbox.h)),
    )
