"""Target -> coordinate. The semantic-to-pixel ladder.

    anchor_text hit ────────────────► matched bbox + recorded offset
        │ miss
        ▼
    role + name match ──────────────► element bbox
        │ miss
        ▼
    recorded bbox ──────────────────► recorded bbox   (+ drift event)
        │ miss / disabled
        ▼
    VLM (gated, off on replay) ─────► proposed bbox
        │ else
        ▼
    RESOLUTION_EXHAUSTED

Why a ladder and not just the recorded coordinate
-------------------------------------------------
The brief is explicit that these UIs are stable and that layout drift is the
*secondary* concern. This ladder is not a drift-tolerance feature. It exists
because a target's position varies within a single unchanged version of the app,
on essentially every run:

  - a conditional banner renders (session warning, maintenance notice) and
    everything below it shifts
  - an inline validation error appears and the fields under it move
  - a member name or address wraps to two lines
  - the list above the button has 12 rows today and 3 tomorrow
  - a widget loads at t+200ms and re-lays out the page after the screenshot
  - the operator resizes the window during a handoff — which our own design invites

None of those are "the app changed". Anchor-relative resolution handles all of
them; a recorded coordinate handles none.

The cut line: variance within a version is *handled*; true drift across versions
is *detected, not repaired*. Falling through to the recorded bbox logs a drift
event, and if pre-click verification then fails we stop and escalate to a human
who can re-record. No LLM repairs the replay path.

The VLM tier is gated off for replay entirely. It exists for discovery, where a
model is already in the loop, and its presence in the enum is what keeps
"deterministic replay" a checkable property rather than a claim.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..perception import ElementIndex
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
    # Whether that choice was a real one. `candidates` alone is the wrong signal
    # to act on: the default match mode is `contains`, so an anchor of "Search"
    # counts both the button and the heading "Member Search" and a rule reading
    # `candidates > 1` would call almost every step ambiguous. This is the count
    # *after* the anchor's own disambiguators have been applied — see `_narrow`.
    ambiguous: bool = False


class Unresolvable(Exception):
    """No tier produced a coordinate. Always terminal for the step."""


class Resolver:
    """Stateless. Takes an observation and a target, returns a coordinate.

    `allow_vlm` is a constructor argument rather than a per-call flag so that the
    replay engine can construct a resolver that is *structurally incapable* of
    calling a model, rather than one that merely promises not to.
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

        Raises `Unresolvable` rather than returning the recorded bbox as a
        last-ditch guess: a step that cannot find its target has not failed to be
        precise, it has failed to know where it is, and clicking anyway is how a
        banking automation submits the wrong form.
        """
        return self.resolve_traced(target, obs, params)[0]

    def resolve_traced(
        self,
        target: Target,
        obs: Observation,
        params: dict[str, object] | None = None,
    ) -> tuple[Resolution, ResolutionTrace]:
        """The same walk, with a record of every rung.

        The winning tier alone cannot answer the question an operator actually has
        when a step falls through to the recorded box — *why did the anchor miss?*
        A miss because the text is no longer on screen and a miss because it
        matched three elements are different applications and different fixes, and
        both used to arrive as the same one-word `recorded_bbox`.

        `resolve()` stays the interface everything else uses; the trace is built
        here so that no caller can accidentally get a resolution without one being
        available.
        """
        p = params or {}
        attempts: list[TierAttempt] = []
        found = (
            self._by_anchor_text(target, obs, p, attempts)
            or self._by_role_name(target, obs, attempts)
            or self._by_recorded_bbox(target, obs, attempts)
        )
        if found is None and self.allow_vlm:
            found = self._by_vlm(target, obs)
        if found is None:
            raise Unresolvable(
                f"no tier located {target.target_desc!r} "
                f"(anchor={target.anchor_text!r} role={target.role!r} "
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
        """Most portable tier. Survives rebranding, relayout and per-tenant skins.

        Ambiguity is reported, not silently resolved: `Resolution.candidates`
        carries the match count and the caller decides. Two matches on a read may
        be fine; on a write, acting on the wrong record is unrecoverable.
        """
        note = _noting(attempts, ResolutionTier.ANCHOR_TEXT)
        if not target.anchor_text:
            return note("skipped", detail="the step's target declares no anchor text")
        needle = apply(render(target.anchor_text, params) or "", target.normalize)
        if not needle:
            return note("skipped", detail="the anchor rendered empty against these inputs")

        matches = [
            el
            for el in obs.elements
            if _matches(apply(_label(el), target.normalize), needle, target.anchor_match)
        ]
        # A declared role narrows the field but never decides alone: `infer_role`
        # is a geometric guess, and letting a bad guess veto a correct text match
        # would trade a little precision for outright failure.
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
        matches = [
            el
            for el in obs.elements
            if (not target.role or el.role == target.role)
            and (
                not target.name
                or apply(el.name or "", target.normalize)
                == apply(target.name, target.normalize)
            )
        ]
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
            # Not the most portable tier, but still a semantic one: it matched
            # something the frame actually says, so it is not a drift event.
            drift=False,
            # `name` is compared exactly here, so there is no substring inflation
            # to strip: more than one match is more than one real candidate.
            ambiguous=len(matches) > 1,
        )

    def _by_recorded_bbox(
        self, target: Target, obs: Observation, attempts: list[TierAttempt] | None = None
    ) -> Resolution | None:
        """Always sets `drift=True`. Aggregated across runs this is the cheapest
        early-warning signal we have, and it is the same mechanism a per-tenant
        canary would use."""
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
        """Discovery only. Asserts `self.allow_vlm` and refuses otherwise.

        A seam, not an implementation — see REPORT §7. Discovery does not need it:
        the model picks from an enumerated set of marks, so it never has to be
        asked "where is this thing" in the first place. The tier exists in the
        enum and here so that "replay never calls a model" stays a property you
        can check by construction rather than a claim in a document.
        """
        if not self.allow_vlm:
            # A programming error, not a resolution outcome: the replay path is
            # handed a resolver that cannot reach a model, and reaching for one
            # anyway must be loud rather than degrade into a failed step.
            raise RuntimeError("VLM resolution requested on a resolver built without it")
        return None

    # --- shared --------------------------------------------------------------

    def _pick(self, matches: list[Element], target: Target) -> Element:
        """Choose among equally valid matches.

        Nearest to the recorded position first — the recording is a hint about
        which of several identical 'View' buttons was meant, and using it here
        costs nothing because ambiguity is still reported to the caller. Failing
        that, the smallest box, which is the most specific thing that said the
        right words.
        """
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
    """Record one rung's outcome and return None, so a tier can `return note(...)`.

    Returning None from the recorder rather than recording at each call site keeps
    the ladder readable: every early return in a tier is still one line, and a rung
    cannot be added later that forgets to say what it did.
    """

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
        target_desc=target.target_desc,
        # Rendered, not the template: an operator comparing the trace against the
        # frame needs the string that was actually looked for.
        anchor_text=render(target.anchor_text, params) if target.anchor_text else None,
        relation=target.relation.value,
        attempts=tuple(attempts),
        tier=found.tier,
        candidates=found.candidates,
        drift=found.drift,
        bbox=found.bbox,
        point=(found.point.x, found.point.y),
    )


def _follow(anchor: Element, target: Target, obs: Observation) -> Element | None:
    """Step from the element that carries the words to the one being acted on.

    Returns None rather than falling back to the anchor when the neighbour is not
    there: typing into a label because the field beside it could not be found is
    exactly the class of silent wrong action this system exists to prevent.
    """
    if target.relation is Relation.SELF:
        return anchor
    index = ElementIndex(obs.elements)
    neighbours = (
        index.right_of(anchor) if target.relation is Relation.RIGHT_OF else index.below(anchor)
    )
    return neighbours[target.relation_index] if len(neighbours) > target.relation_index else None


def _label(el: Element) -> str:
    return (el.text or el.name or "").strip()


def _narrow(matches: list[Element], needle: str, target: Target) -> list[Element]:
    """Drop the matches the anchor did not really mean.

    `contains` is the right default — a balance sits inside "Available Balance:
    $18,204.55" and an exact anchor would never find it — but it makes the raw
    match count a bad measure of ambiguity. Anchoring on "Search" matches the
    button *and* the heading "Member Search", every time, on a screen where
    nothing is actually ambiguous.

    So: an element whose whole label is the anchor beats one that merely contains
    it. That is not a tie-break heuristic, it is what the recording said — a step
    anchored on "Search" and a control that reads exactly "Search" are the same
    claim, and a heading that happens to end in the same word is not a rival for
    it.

    What survives this is the ambiguity worth stopping for: three rows whose
    buttons all read exactly "View". There the recorded position is not evidence
    either, because which row is where is a function of the data — which is what
    `find_and_act` exists for, and why a plain click that lands here on a write is
    a recording that should have been one.
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
            # A bad pattern in an artifact is a resolution miss, not a crash in
            # the middle of a run: the step then fails with a legible message.
            return False
    return needle in haystack


def point_in(bbox: Bbox, offset: tuple[float, float]) -> Point:
    """Offset within a resolved box, 0..1. Default (0.5, 0.5) is the center.

    Non-center offsets are how a step targets the 'View' button at the right edge
    of a matched row without needing a second resolution pass.
    """
    return Point(
        x=min(1.0, max(0.0, bbox.x + offset[0] * bbox.w)),
        y=min(1.0, max(0.0, bbox.y + offset[1] * bbox.h)),
    )
