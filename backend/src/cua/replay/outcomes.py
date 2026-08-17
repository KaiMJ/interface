"""Classifying what we are looking at.

One function, called after every step, that decides which of the four classes the
current frame belongs to. Keeping it in one place is what stops the taxonomy from
being re-litigated at each call site — the most common way a clean error model
rots.

    BUSINESS_OUTCOME  declared per capability. Checked FIRST: "no such member" is
                      an answer the caller branches on, and if the checkpoint check
                      ran first it would be reported as a checkpoint failure.
    RECOVERABLE       declared per app policy. Apply the handler, re-observe, retry
                      the step. Bounded by max_per_run.
    OK                the step's checkpoint holds.
    FAILURE           everything else.

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
from ..schema import Capability, Observation


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
    for outcome in cap.business_outcomes:
        if evaluate(outcome.detector, obs, params):
            return Classified(
                kind=Classification.BUSINESS_OUTCOME,
                name=outcome.name,
                observed=outcome.detector.value,
                fields=_outcome_fields(outcome, params),
            )

    # 2. A declared recoverable condition. Bounded: dismissing the same modal
    #    eleven times means the dismissal is not working, and eleven successful
    #    recoveries is the wrong way to describe that.
    recovery = policy.match_recovery(_text(obs))
    if recovery is not None:
        if recovery_counts.get(recovery.name, 0) >= recovery.max_per_run:
            return Classified(
                kind=Classification.FAILURE,
                name=recovery.name,
                expected=f"{recovery.name} to clear after {recovery.max_per_run} attempts",
                observed=recovery.detector_value,
            )
        return Classified(kind=Classification.RECOVERABLE, name=recovery.name)

    # 3. Declared conditions with no handler, split by who can clear them. Both
    #    would otherwise arrive as "the checkpoint did not hold", which describes
    #    the symptom and hides the cause.
    text = _text(obs)
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
