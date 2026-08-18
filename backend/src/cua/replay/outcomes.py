"""Classifying what we are looking at.

One function, called after every step, that decides which of the four classes the
current frame belongs to. Keeping it in one place is what stops the taxonomy from
being re-litigated at each call site — the most common way a clean error model
rots.

    BUSINESS_OUTCOME  declared per capability, with the detector optionally
                      inherited from app policy by name (`effective_outcomes`).
                      Checked FIRST: "no such member" is an answer the caller
                      branches on, and if the checkpoint check ran first it would
                      be reported as a checkpoint failure.
    RECOVERABLE       declared per app policy. Apply the handler, re-observe, and
                      re-execute the step if it is safe to. Bounded by max_per_run.
    OK                the step's checkpoint holds.
    FAILURE           everything else.

`conditions()` is the middle band on its own — the states that are properties of
the *application* rather than of any step. It is split out because the engine
needs it twice: after a step, as part of the full classification below, and
*before* a step, on the frame the step is about to act on. A maintenance modal
that is already on screen must be cleared before the click, not diagnosed after it
was swallowed.

That last line is the design decision, not a gap. Under vision-first perception we
cannot enumerate every screen an enterprise app can produce. A system that guesses
at an unrecognized state in a banking application is worse than one that stops and
says what it saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..resolve import evaluate, verify_effect
from ..schema import BusinessOutcome, Capability, CheckKind, Checkpoint, Observation


class Classification(str, Enum):
    OK = "ok"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    ESCALATE = "escalate"          # declared, unrecoverable by us, fixable by a human
    APP_ERROR = "app_error"        # the application itself failed
    FAILURE = "failure"


@dataclass(frozen=True)
class Classified:
    kind: Classification
    name: str | None = None              # outcome name or recovery name
    expected: str | None = None
    observed: str | None = None
    fields: dict[str, Any] | None = None


def classify(
    obs: Observation,
    cap: Capability,
    step: Any,
    policy: Any,
    params: dict[str, Any],
    recovery_counts: dict[str, int],
) -> Classified:
    """Decide what the current frame is. Order is load-bearing; see module docstring."""

    # 1. A declared business outcome ends the run cleanly. First, always: if the
    #    checkpoint were evaluated first, "no such member" would be reported as a
    #    failed assertion rather than as the answer the caller asked for.
    for outcome in effective_outcomes(cap, policy):
        # `effective_outcomes` has filled every inherited detector or raised, so
        # the optionality is resolved by the time we are here.
        detector = outcome.detector
        if detector is not None and evaluate(detector, obs, params):
            return Classified(
                kind=Classification.BUSINESS_OUTCOME,
                name=outcome.name,
                observed=detector.value,
                fields=_outcome_fields(outcome, params),
            )

    # 2-3. Application conditions: recoverable, app error, or one for a human.
    condition = conditions(obs, policy, recovery_counts)
    if condition is not None:
        return condition

    # 4. The expected path.
    checkpoint = getattr(step, "checkpoint", None) if step is not None else None
    if checkpoint is None:
        return Classified(kind=Classification.OK)
    verified = verify_effect(checkpoint, obs, params)
    if verified.ok:
        return Classified(kind=Classification.OK)

    # 5. Everything else. Not a gap — see the module docstring.
    return Classified(
        kind=Classification.FAILURE,
        expected=verified.expected,
        observed=verified.observed,
    )


class UndeclaredOutcome(Exception):
    """A capability inherits an outcome the application does not declare.

    Raised when the run starts rather than when the screen appears. The failure
    this prevents is quiet: an outcome whose detector resolved to nothing simply
    never matches, so "no such member" comes back as a checkpoint failure and the
    capability's contract is wrong in the one direction nobody notices.
    """


def effective_outcomes(cap: Capability, policy: Any) -> list[BusinessOutcome]:
    """The capability's outcomes with inherited detectors filled in.

    An entry that carries its own detector is used as recorded — a flow-specific
    answer like "insufficient funds" is not a property of the application and has
    nowhere else to live. An entry without one names an app-level detector, and
    resolving it here rather than at load time means a policy edit takes effect on
    the next run without rewriting every artifact that inherits from it. That is
    the whole point of inheriting: one YAML diff immunises every capability on the
    app, including the ones recorded next year.
    """
    resolved: list[BusinessOutcome] = []
    for outcome in cap.business_outcomes:
        if outcome.detector is not None:
            resolved.append(outcome)
            continue
        inherited = policy.outcome(outcome.name)
        if inherited is None:
            raise UndeclaredOutcome(
                f"{cap.ref} inherits business outcome {outcome.name!r}, which "
                f"{policy.app or 'this application'} does not declare"
            )
        resolved.append(
            outcome.model_copy(
                update={
                    "detector": Checkpoint(
                        kind=CheckKind(inherited.detector_kind),
                        value=inherited.detector_value,
                    ),
                    "description": outcome.description or inherited.description,
                    # Which of the run's inputs this answer reports back. Typed
                    # from the capability's own declarations, because the app knows
                    # the field is called `member_id` and only the capability knows
                    # what type it accepts for one.
                    "result_fields": outcome.result_fields
                    or {
                        name: spec.type
                        for spec in cap.inputs
                        for name in [spec.name]
                        if name in inherited.result_fields
                    },
                }
            )
        )
    return resolved


def conditions(
    obs: Observation, policy: Any, recovery_counts: dict[str, int]
) -> Classified | None:
    """Declared application conditions on this frame, or None if it is ordinary.

    Everything here is a property of the *application*, declared in its policy
    file, and therefore evaluable without a step — which is exactly why it is its
    own function. The engine calls it before a step acts as well as after: an
    interstitial that is already on screen swallows the click that follows it, and
    diagnosing that afterwards costs a step timeout and tells the operator the
    checkpoint failed rather than that a modal was in the way.
    """
    text = _text(obs)

    # Recoverable. Bounded: dismissing the same modal eleven times means the
    # dismissal is not working, and eleven successful recoveries is the wrong way
    # to describe that.
    recovery = policy.match_recovery(text)
    if recovery is not None:
        if recovery_counts.get(recovery.name, 0) >= recovery.max_per_run:
            return Classified(
                kind=Classification.FAILURE,
                name=recovery.name,
                expected=f"{recovery.name} to clear after {recovery.max_per_run} attempts",
                observed=recovery.detector_value,
            )
        return Classified(kind=Classification.RECOVERABLE, name=recovery.name)

    # Declared, with no handler, split by who can clear it. Both would otherwise
    # arrive as "the checkpoint did not hold", which describes the symptom and
    # hides the cause.
    app_error = policy.match_app_error(text)
    if app_error is not None:
        return Classified(
            kind=Classification.APP_ERROR,
            name=app_error.name,
            observed=app_error.detector_value,
        )
    escalation = policy.match_escalation(text)
    if escalation is not None:
        return Classified(
            kind=Classification.ESCALATE,
            name=escalation.name,
            observed=escalation.detector_value,
        )
    return None


def _text(obs: Observation) -> str:
    return " ".join(t for t in ((e.text or e.name or "").strip() for e in obs.elements) if t)


def _outcome_fields(outcome: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fill a business outcome's declared fields.

    From the run's inputs, not from the screen. A "member not found" outcome
    reports *which* member was not found, and the only trustworthy source for that
    is what the caller asked for — reading it back off an error page would be
    scraping a message to recover a value we were handed.
    """
    return {name: params.get(name) for name in outcome.result_fields}
