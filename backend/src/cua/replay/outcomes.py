"""What is this frame? One function, so the ordering cannot be re-litigated per call site.

    BUSINESS_OUTCOME  per capability; detector may be inherited from app policy
    RECOVERABLE       per app policy; handle, re-observe, re-execute if safe
    OK                the step's checkpoint holds
    FAILURE           everything else
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..resolve import evaluate, verify_effect
from ..schema import (
    BusinessOutcome,
    Capability,
    CheckKind,
    Checkpoint,
    FindAndActStep,
    Observation,
)


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
    """Order is load-bearing; see module docstring."""

    # 1. First, always: evaluated after the checkpoint, "no such member" arrives as a failed
    #    assertion rather than as the answer that was asked for.
    for outcome in effective_outcomes(cap, policy):
        # Inherited detectors are filled in above, or it raised.
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
    """A capability inherits an outcome the app does not declare.

    Raised at run start rather than when the screen appears: an unresolved detector never
    matches, so the outcome would come back as a checkpoint failure while the contract went on
    advertising it.
    """


def effective_outcomes(cap: Capability, policy: Any) -> list[BusinessOutcome]:
    """Outcomes with inherited detectors filled in.

    An entry carrying its own detector is flow-specific ("insufficient funds") and used as
    recorded; one without names an app-level detector, resolved here rather than at load time
    so a policy edit takes effect on the next run of every capability.
    """
    # An outcome a step raises structurally needs no text: `on_not_found_outcome` fires on an
    # exhausted scan, which is an absence rather than an announcement. The screen that produces
    # it may say nothing at all about the condition — a member without the account simply does
    # not have the row — so requiring a detector here would make the step's own field
    # undeclarable.
    structural = {
        step.on_not_found_outcome
        for step in cap.steps
        if isinstance(step, FindAndActStep) and step.on_not_found_outcome
    }

    resolved: list[BusinessOutcome] = []
    for outcome in cap.business_outcomes:
        if outcome.detector is not None:
            resolved.append(outcome)
            continue
        inherited = policy.outcome(outcome.name)
        if inherited is None and outcome.name in structural:
            resolved.append(outcome)
            continue
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
                    # The app names the field; only the capability knows its type.
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
    """Declared app conditions on this frame, or None if it is ordinary. Evaluable without a
    step, which is why it is its own function: the engine calls it before a step acts as well
    as after."""
    text = _text(obs)

    # Bounded: dismissing the same modal eleven times means the dismissal is not working.
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

    # No handler, split by who can clear it. Both would otherwise arrive as "the checkpoint
    # did not hold" — the symptom, not the cause.
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
    """Fill a business outcome's declared fields from the run's inputs, rather than scraping an
    error page to recover a value the caller handed us."""
    return {name: params.get(name) for name in outcome.result_fields}
