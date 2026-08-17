"""Guardrails.

Checked in the same place on both paths — discovery and replay call the identical
`Policy.check()` before every action. A guardrail that only guards the LLM is not
a guardrail; a compromised or buggy artifact is just as capable of submitting the
wrong transfer as a confused model is.

Three rules:

  1. Allowlist. Permitted URL patterns and permitted primitives. Anything else is
     POLICY_DENIED, which is a hard stop rather than a skip — an agent that
     silently continues past a denied action produces a run whose result no longer
     means what it says.

  2. Risk. Every step declares `safe` or `risky` (schema.Risk). Risky actions are
     handled conservatively: blocked, or escalated to a human for confirmation, per
     policy. In banking, latency is cheap and a silently wrong transfer is not.
     This is only enforceable because steps carry declared intent — `click(0.42,
     0.71)` cannot be classified.

  3. Redaction. Values marked `sensitive` never reach a log line, an artifact, an
     evidence file or a model prompt.

Known limits, to state honestly rather than paper over:
  - risk classification is static per capability, authored at record time and
    reviewed by a human. There is no dynamic risk scoring.
  - there is no defense here against prompt injection via page content that the
    discovery model reads. The allowlist bounds the blast radius; it does not
    prevent the model being misled inside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..schema import Primitive, Relation, Risk, Target


class PolicyDenied(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


class RiskDisposition(str):
    ALLOW = "allow"
    CONFIRM = "confirm"     # escalate to a human before proceeding
    BLOCK = "block"


@dataclass(frozen=True)
class Recovery:
    """A declared recoverable condition.

    Lives in app policy rather than in a capability, because these are properties
    of the application, not of any one flow: a session-expiry interstitial can
    interrupt every capability on the app, and duplicating its handler into each
    artifact would guarantee they drift apart.

    `max_per_run` is what stops a recovery loop. Dismissing the same modal eleven
    times means the dismissal is not working, and that is a hard failure, not
    eleven successful recoveries.
    """

    name: str
    detector_kind: str
    detector_value: str
    actions: tuple[dict[str, str], ...]
    max_per_run: int = 2


@dataclass(frozen=True)
class Condition:
    """A declared application state with no handler.

    Two kinds, and the difference is who can clear it:

      app error    the application itself failed. Nobody can fix it from here, so
                   the run stops with APP_ERROR rather than reporting a checkpoint
                   that did not hold — which would send an operator looking for a
                   layout problem that is not there.
      escalation   a human can fix it. Session expiry is the archetype, and the
                   reason it is not a recovery: re-authenticating mid-flow would
                   mean the automation holds credentials and silently resumes a
                   run whose context may no longer be valid.
    """

    name: str
    detector_kind: str
    detector_value: str


@dataclass(frozen=True)
class SignOnStep:
    """One step of the sign-on recipe.

    Deliberately the same shape a capability step has — an action, a target
    described by what is on screen, a value — because it is the same mechanism.
    What differs is that this one is never serialized into an artifact, because
    its value is a credential.
    """

    action: Primitive
    target: Target | None
    value: str | None = None


@dataclass(frozen=True)
class SignOn:
    """How to get this application into an authenticated state."""

    detector_kind: str
    detector_value: str
    steps: tuple[SignOnStep, ...]


class Policy:
    """Loaded from `policies/<app>.yaml`."""

    def __init__(
        self,
        allowed_url_patterns: tuple[str, ...],
        allowed_actions: frozenset[Primitive],
        risky_disposition: str,
        recoveries: tuple[Recovery, ...],
        redact_patterns: tuple[str, ...],
        risky_intent_patterns: tuple[str, ...] = (),
        app_errors: tuple[Condition, ...] = (),
        escalations: tuple[Condition, ...] = (),
        sign_on: SignOn | None = None,
        surface: str = "",
        app: str = "",
    ) -> None:
        self.app = app
        self.sign_on = sign_on
        # What the discovery model is told it is looking at. A fact about the
        # application, so it lives with the application's other facts.
        self.surface = surface
        self.app_errors = app_errors
        self.escalations = escalations
        self.allowed_url_patterns = allowed_url_patterns
        self.allowed_actions = allowed_actions
        self.risky_disposition = risky_disposition
        self.recoveries = recoveries
        self.redact_patterns = redact_patterns
        self.risky_intent_patterns = risky_intent_patterns
        self._url_res = tuple(re.compile(p) for p in allowed_url_patterns)
        self._intent_res = tuple(re.compile(p) for p in risky_intent_patterns)

    @classmethod
    def load(cls, path: Path) -> Policy:
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        return cls(
            app=str(raw.get("app", "")),
            allowed_url_patterns=tuple(raw.get("allowed_url_patterns", ())),
            # An unlisted primitive is denied, so an empty or missing list denies
            # everything. Failing closed is the only safe direction here.
            allowed_actions=frozenset(
                Primitive(a) for a in raw.get("allowed_actions", ())
            ),
            risky_disposition=str(raw.get("risky_disposition", RiskDisposition.CONFIRM)),
            recoveries=tuple(
                Recovery(
                    name=str(r["name"]),
                    detector_kind=str(r["detector"]["kind"]),
                    detector_value=str(r["detector"].get("value", "")),
                    actions=tuple(dict(a) for a in r.get("actions", ())),
                    max_per_run=int(r.get("max_per_run", 2)),
                )
                for r in raw.get("recoveries", ())
            ),
            redact_patterns=tuple(raw.get("redact_patterns", ())),
            risky_intent_patterns=tuple(raw.get("risky_intent_patterns", ())),
            app_errors=_conditions(raw.get("app_errors", ())),
            escalations=_conditions(raw.get("escalations", ())),
            sign_on=_sign_on(raw.get("sign_on")),
            surface=str(raw.get("surface", "")).strip(),
        )

    def check_url(self, url: str) -> None:
        """Raise PolicyDenied if the URL is outside the allowlist.

        Evaluated on navigate *and* after every action, because a click can
        navigate. Checking only on explicit navigation leaves the obvious hole.
        """
        if not any(r.match(url) for r in self._url_res):
            raise PolicyDenied("allowlist", f"{url} is not in the permitted URL patterns")

    def check_action(self, action: Primitive, risk: Risk, intent: str) -> str:
        """Return a RiskDisposition, or raise PolicyDenied.

        `intent` is carried through purely so a denial is debuggable: 'denied
        click' is useless in an audit log, 'denied: submit $500 transfer from
        29883' is not.
        """
        if action not in self.allowed_actions:
            raise PolicyDenied("allowlist", f"action {action.value!r} not permitted ({intent})")

        if self.classify_risk(risk, intent) is Risk.SAFE:
            return RiskDisposition.ALLOW
        if self.risky_disposition == RiskDisposition.BLOCK:
            raise PolicyDenied("risk", f"risky action blocked by policy: {intent}")
        return str(self.risky_disposition)

    def classify_risk(self, declared: Risk, intent: str) -> Risk:
        """The declared risk, escalated when the intent reads as a mutation.

        One-directional on purpose: policy can promote `safe` to `risky`, never
        the reverse. A recording that mislabels a submit as safe is the expensive
        failure; a read misclassified as risky costs one confirmation.
        """
        if declared is Risk.RISKY:
            return Risk.RISKY
        return Risk.RISKY if any(r.search(intent) for r in self._intent_res) else Risk.SAFE

    def match_recovery(self, observed_text: str) -> Recovery | None:
        return next(
            (r for r in self.recoveries if _matches(r, observed_text)),
            None,
        )

    def match_app_error(self, observed_text: str) -> Condition | None:
        return next((c for c in self.app_errors if _matches(c, observed_text)), None)

    def match_escalation(self, observed_text: str) -> Condition | None:
        return next((c for c in self.escalations if _matches(c, observed_text)), None)


def _matches(condition: Any, observed_text: str) -> bool:
    return (
        condition.detector_kind == "text_present"
        and bool(condition.detector_value)
        and condition.detector_value.casefold() in observed_text.casefold()
    )


def _sign_on(raw: Any) -> SignOn | None:
    if not raw:
        return None
    steps = []
    for entry in raw.get("steps", ()):
        anchor = entry.get("anchor_text")
        intent = str(entry.get("intent", "sign on"))
        steps.append(
            SignOnStep(
                action=Primitive(entry["action"]),
                target=(
                    Target(
                        intent=intent,
                        target_desc=(
                            f"the control {anchor!r} names"
                            if entry.get("relation", "self") == "self"
                            else f"the field beside {anchor!r}"
                        ),
                        anchor_text=str(anchor),
                        relation=Relation(entry.get("relation", "self")),
                    )
                    if anchor
                    else None
                ),
                value=entry.get("value"),
            )
        )
    return SignOn(
        detector_kind=str(raw["detector"]["kind"]),
        detector_value=str(raw["detector"].get("value", "")),
        steps=tuple(steps),
    )


def _conditions(raw: Any) -> tuple[Condition, ...]:
    return tuple(
        Condition(
            name=str(c["name"]),
            detector_kind=str(c["detector"]["kind"]),
            detector_value=str(c["detector"].get("value", "")),
        )
        for c in raw
    )
