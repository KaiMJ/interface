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

from dataclasses import dataclass

from ..schema import Bbox, Observation, Point, ResolutionTier, Target


@dataclass(frozen=True)
class Resolution:
    point: Point
    bbox: Bbox
    tier: ResolutionTier
    matched_text: str | None = None
    candidates: int = 1          # >1 means the match was ambiguous
    drift: bool = False          # true when a lower tier than anchor_text was used


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
        raise NotImplementedError

    # --- tiers ---------------------------------------------------------------

    def _by_anchor_text(
        self, target: Target, obs: Observation, params: dict[str, object]
    ) -> Resolution | None:
        """Most portable tier. Survives rebranding, relayout and per-tenant skins.

        Ambiguity is reported, not silently resolved: `Resolution.candidates`
        carries the match count and the caller decides. Two matches on a read may
        be fine; on a write, acting on the wrong record is unrecoverable.
        """
        raise NotImplementedError

    def _by_role_name(self, target: Target, obs: Observation) -> Resolution | None:
        raise NotImplementedError

    def _by_recorded_bbox(self, target: Target, obs: Observation) -> Resolution | None:
        """Always sets `drift=True`. Aggregated across runs this is the cheapest
        early-warning signal we have, and it is the same mechanism a per-tenant
        canary would use."""
        raise NotImplementedError

    def _by_vlm(self, target: Target, obs: Observation) -> Resolution | None:
        """Discovery only. Asserts `self.allow_vlm` and refuses otherwise."""
        raise NotImplementedError


def point_in(bbox: Bbox, offset: tuple[float, float]) -> Point:
    """Offset within a resolved box, 0..1. Default (0.5, 0.5) is the center.

    Non-center offsets are how a step targets the 'View' button at the right edge
    of a matched row without needing a second resolution pass.
    """
    raise NotImplementedError
