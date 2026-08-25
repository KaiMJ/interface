"""Verification — the checks that wrap every action.

A stable coordinate is not a right one. An unexpected modal moves nothing; it lands on
top, so the recorded coordinate still resolves and the click hits the dialog. In a
banking application that is a correctness failure rather than a robustness one, and no
amount of better targeting detects it. So each step is: resolve, verify target, execute,
verify effect.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..calibration import CALIBRATION
from ..perception import ElementIndex
from ..schema import (
    Bbox,
    CheckKind,
    Checkpoint,
    Element,
    ElementSource,
    FailureKind,
    MatchMode,
    Observation,
    Relation,
    ResolutionTier,
    Target,
)
from .normalize import apply
from .resolver import Resolution, Resolver, Unresolvable
from .template import render

# How much of a failed assertion to show. A display limit, not a threshold.
_OBSERVED_SNIPPET = 240


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    kind: FailureKind | None = None
    expected: str | None = None
    observed: str | None = None
    detail: str = ""
    # "An undeclared element covers the target" is not actionable without knowing
    # *which*: an operator has to find it, and a policy author has to write a
    # dismissal handler against it.
    region: Bbox | None = None


def verify_target(
    target: Target,
    resolution: Resolution,
    obs: Observation,
    params: dict[str, object] | None = None,
) -> VerifyResult:
    """Pre-action assertion.

    Two distinct checks with two distinct failure kinds, because they call for
    different operator responses:

      TARGET_MISMATCH     the region resolved, but its text does not match the
                          recorded label. Either we resolved the wrong thing or the
                          app changed. Re-record.
      UNEXPECTED_OVERLAY  something is stacked on top of the target. If the policy
                          declares a dismissal handler this is recoverable; if not,
                          it is a hard stop. Never click through one.
    """
    p = params or {}

    # 1. Does the region say what the recording said it said?
    #
    # Only the semantic handles are assertable. `target_desc` is prose for a
    # reviewer and appears nowhere on screen. With neither an anchor nor a name there
    # is nothing to check, and saying so beats a check that always passes.
    # Declared-but-unrenderable is not the same as never declared. The first is a
    # check that has become impossible and must fail; the second is a target with
    # nothing assertable, which is legitimate. Reading both as "nothing to check"
    # turned off this assertion for exactly the run that needed it — a blank input,
    # an anchor that rendered to nothing, and a balance read off the wrong row.
    if target.anchor_text and not (render(target.anchor_text, p) or "").strip():
        return VerifyResult(
            ok=False,
            kind=FailureKind.TARGET_MISMATCH,
            expected=target.anchor_text,
            observed="<the anchor rendered empty>",
            detail=(
                "the target's anchor is a template that rendered to nothing against "
                "these inputs, so there is no way to confirm what was resolved"
            ),
            region=resolution.bbox,
        )

    expected = render(target.anchor_text, p) or target.name
    if expected:
        # With a relation the resolved box is the *neighbour* — an empty input
        # beside a label — so the assertion belongs on the anchor we stepped from.
        # Absent, the target fell through to a recorded box without finding the
        # label, and "type the username at these coordinates on a screen that does
        # not say User ID" is exactly what this prevents.
        observed = (
            region_text(obs, resolution.bbox)
            if target.relation is Relation.SELF
            else (resolution.matched_text or "")
        )
        norm = target.normalize
        if not _match(apply(observed, norm), apply(expected, norm), target.anchor_match):
            return VerifyResult(
                ok=False,
                kind=FailureKind.TARGET_MISMATCH,
                expected=expected,
                observed=observed[:_OBSERVED_SNIPPET] or "<nothing readable there>",
                detail=(
                    f"resolved via {resolution.tier.value} but the region does not "
                    f"read as the recorded target"
                ),
                region=resolution.bbox,
            )

    # 2. Is something sitting on top of it?
    #
    # Only worth asking when the target was *not* confirmed from screen text: if we
    # just read the recording's words at these coordinates, nothing opaque covers
    # them. Otherwise a panel enclosing its own button is indistinguishable from a
    # modal enclosing someone else's — measured, the sign-on panel tripped this on
    # every login.
    overlay = (
        _overlay_over(obs, resolution.bbox)
        if resolution.tier is ResolutionTier.RECORDED_BBOX
        else None
    )
    if overlay is not None:
        return VerifyResult(
            ok=False,
            kind=FailureKind.UNEXPECTED_OVERLAY,
            expected=expected or target.target_desc,
            observed=(overlay.text or overlay.name or f"<{overlay.role} {overlay.id}>")[
                :_OBSERVED_SNIPPET
            ],
            detail="an undeclared element covers the target; refusing to click through it",
            # The *overlay's* box: what a dismissal handler is written against.
            region=overlay.bbox,
        )

    return VerifyResult(ok=True)


def verify_effect(
    checkpoint: Checkpoint,
    obs: Observation,
    params: dict[str, object] | None = None,
) -> VerifyResult:
    """Post-action assertion against the step's declared checkpoint."""
    if evaluate(checkpoint, obs, params):
        return VerifyResult(ok=True)
    expected = render(checkpoint.value, params)
    return VerifyResult(
        ok=False,
        kind=FailureKind.CHECKPOINT_FAILED,
        expected=f"{checkpoint.kind.value} {expected!r}" if expected else checkpoint.kind.value,
        observed=_observed_for(checkpoint, obs, params)[:_OBSERVED_SNIPPET],
        detail="the action executed but the state it should have produced is not there",
    )


def evaluate(
    checkpoint: Checkpoint,
    obs: Observation,
    params: dict[str, object] | None = None,
) -> bool:
    """Evaluate one checkpoint against one observation. No waiting, no retries.

    Kept separate from `verify_effect` because business-outcome detectors and
    recoverable-condition detectors are the same shape and are evaluated against
    the same frame — the difference between them is what the caller does with a
    True, not how it is computed.
    """
    p = params or {}
    kind = checkpoint.kind
    expected = render(checkpoint.value, p)

    if kind is CheckKind.REGION_STABLE:
        # Stability is a property of two frames and this function sees one. The
        # engine only evaluates checkpoints on a frame `Perceiver.settle()` has
        # already declared stable, so the condition is satisfied by construction
        # — and `frame_hash` being absent means nobody established that.
        return obs.frame_hash is not None

    if kind is CheckKind.URL_MATCHES:
        return obs.url is not None and _match(obs.url, expected or "", checkpoint.match)

    if kind is CheckKind.ELEMENT_VISIBLE:
        return _locate(checkpoint, obs, p) is not None

    if kind is CheckKind.FIELD_VALUE_MATCHES:
        value = _field_value(checkpoint, obs, p)
        if value is None or expected is None:
            return False
        return _match(
            apply(value, checkpoint.normalize),
            apply(expected, checkpoint.normalize),
            checkpoint.match,
        )

    present = _match(
        apply(_scope_text(checkpoint, obs, p), checkpoint.normalize),
        apply(expected or "", checkpoint.normalize),
        checkpoint.match,
    )
    return present if kind is CheckKind.TEXT_PRESENT else not present


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------
#
# `region_text` is public: the replay engine reads declared outputs with it, and
# an extraction that read the screen differently from the way a checkpoint reads
# it would be a second, silently divergent definition of "what it says there".


def _match(haystack: str, needle: str, mode: MatchMode) -> bool:
    if mode is MatchMode.EXACT:
        return haystack == needle
    if mode is MatchMode.REGEX:
        try:
            return re.search(needle, haystack) is not None
        except re.error:
            return False
    return needle in haystack


def region_text(obs: Observation, region: Bbox) -> str:
    """Everything readable inside a region, in reading order.

    Containment rather than intersection, at `Calibration.region_containment`:
    OCR line boxes and detector boxes disagree about padding, and a label that
    half-overlaps the button is still that button's label.
    """
    index = ElementIndex(obs.elements)
    inside = index.within(region)
    if not inside:
        # Nothing sits inside the region, so report what the region sits inside.
        # When a dialog has covered the target this is the difference between
        # "<nothing readable there>" and "Confirm transfer? Cancel Confirm" in the
        # failure record, and the second one tells an operator what happened.
        # min_iou=0: a dialog covering the whole page has a negligible IoU with a
        # button-sized region and is exactly the element being looked for.
        inside = [
            e
            for e in index.overlapping(region, min_iou=0.0)
            if region.contained_by(e.bbox) >= CALIBRATION.enclosure
        ]
    return " ".join(t for t in ((e.text or e.name or "").strip() for e in inside) if t)


def _scope_text(checkpoint: Checkpoint, obs: Observation, params: dict[str, Any]) -> str:
    """The text a checkpoint is evaluated against — the whole frame, or one region.

    A scope that cannot be resolved yields the empty string rather than falling
    back to the whole frame. Silently widening the scope would turn "the error
    banner does not say 'insufficient funds'" into a claim about the entire page,
    which is how a checkpoint passes for the wrong reason.
    """
    if checkpoint.scope is None:
        return " ".join(
            t for t in ((e.text or e.name or "").strip() for e in obs.elements) if t
        )
    scope = _locate_target(checkpoint.scope, obs, params)
    return "" if scope is None else region_text(obs, scope.bbox)


def _locate_target(target: Target, obs: Observation, params: dict[str, Any]) -> Resolution | None:
    try:
        return Resolver(allow_vlm=False).resolve(target, obs, params)
    except Unresolvable:
        return None


def _locate(checkpoint: Checkpoint, obs: Observation, params: dict[str, Any]) -> Element | None:
    """Find the element an ELEMENT_VISIBLE checkpoint is about."""
    if checkpoint.scope is not None:
        found = _locate_target(checkpoint.scope, obs, params)
        return None if found is None else _closest(obs, found.bbox)
    expected = render(checkpoint.value, params)
    if not expected:
        return None
    wanted = apply(expected, checkpoint.normalize)
    return next(
        (
            e
            for e in obs.elements
            if _match(
                apply(e.text or e.name or "", checkpoint.normalize), wanted, checkpoint.match
            )
        ),
        None,
    )


def _closest(obs: Observation, region: Bbox) -> Element | None:
    # min_iou=0: whatever the R-tree says intersects, ranked by how much. A caller
    # asking "what is at this region" wants the answer, not a filtered one.
    overlapping = ElementIndex(obs.elements).overlapping(region, min_iou=0.0)
    return overlapping[0] if overlapping else None


def _field_value(checkpoint: Checkpoint, obs: Observation, params: dict[str, Any]) -> str | None:
    """Read what a field currently contains.

    Two shapes, because forms use both: the value rendered inside the control, and
    the value rendered in the cell beside its label. Inside wins when it is there.
    """
    if checkpoint.scope is None:
        return None
    scope = _locate_target(checkpoint.scope, obs, params)
    if scope is None:
        return None
    inside = region_text(obs, scope.bbox)
    if inside:
        return inside
    anchor = _closest(obs, scope.bbox)
    if anchor is None:
        return None
    right = ElementIndex(obs.elements).right_of(anchor)
    return (right[0].text or right[0].name) if right else None


def _observed_for(checkpoint: Checkpoint, obs: Observation, params: dict[str, Any] | None) -> str:
    """What to show a human when a checkpoint fails.

    The whole debugging story for a failed replay is `expected` next to this, so
    it has to be the text the check actually looked at — not a generic screen
    dump.
    """
    p = params or {}
    if checkpoint.kind is CheckKind.URL_MATCHES:
        return obs.url or "<no url on this surface>"
    if checkpoint.kind is CheckKind.FIELD_VALUE_MATCHES:
        return _field_value(checkpoint, obs, p) or "<field not readable>"
    return _scope_text(checkpoint, obs, p) or "<nothing readable in scope>"


def _overlay_over(obs: Observation, region: Bbox) -> Element | None:
    """Is an undeclared element covering the target?

    Called only when the target could not be confirmed semantically — see
    `verify_target`. That restriction is what makes a purely geometric test
    meaningful without z-order: we already know the recording's words are *not*
    readable at these coordinates, so a control that covers them, is a large share
    of the frame, and dwarfs the target is the dialog that hid them.

    What this does not catch is a modal that leaves the target uncovered. There the
    click lands on the dialog's backdrop and nothing happens, and the step's own
    checkpoint fails one line later. Detected either way; only the error kind
    differs. Declared interstitials never reach here — `replay.outcomes.classify`
    matches them as recoverable first.
    """
    target_area = region.w * region.h
    for el in obs.elements:
        if el.source is ElementSource.OCR:
            continue
        area = el.bbox.w * el.bbox.h
        if area < CALIBRATION.overlay_min_frame_area or target_area <= 0:
            continue
        if area / target_area < CALIBRATION.overlay_min_size_ratio:
            continue
        if region.contained_by(el.bbox) >= CALIBRATION.enclosure:
            return el
    return None
