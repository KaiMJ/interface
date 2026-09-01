"""Guardrails.

Checked in the same place on both paths: discovery and replay call the identical
`check_action()` and `check_url()`. A guardrail that only guards the LLM is not a guardrail —
a buggy or tampered artifact submits the wrong transfer just as well as a confused model does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from ..schema import AppRef, PolicyDecision, Primitive, Relation, Risk, Target

# Every detector in a policy file is matched by `_matches`, which reads text off the frame.
# Named so `load` can reject a kind it would otherwise accept and silently never match.
_SUPPORTED_DETECTOR_KINDS = frozenset({"text_present"})


class PolicyDenied(Exception):
    def __init__(self, rule: str, detail: str) -> None:
        super().__init__(f"{rule}: {detail}")
        self.rule = rule
        self.detail = detail


class RiskDisposition(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"     # escalate to a human before proceeding
    BLOCK = "block"


@dataclass(frozen=True)
class Recovery:
    """A declared recoverable condition.

    In app policy rather than in a capability: an interstitial can interrupt every capability
    on the app, and copies of its handler in each artifact would drift. `max_per_run` stops a
    recovery loop — dismissing the same modal eleven times means the dismissal is not working.
    """

    name: str
    detector_kind: str
    detector_value: str
    actions: tuple[dict[str, str], ...]
    max_per_run: int = 2


@dataclass(frozen=True)
class Condition:
    """A declared application state with no handler. Two kinds, and the difference is who can
    clear it:

      app error    the application failed. Nobody can fix it from here, so the run stops with
                   APP_ERROR rather than reporting a checkpoint that did not hold.
      escalation   a human can fix it. Session expiry is the archetype: resuming mid-flow
                   would continue a run whose context may no longer be valid.
    """

    name: str
    detector_kind: str
    detector_value: str


@dataclass(frozen=True)
class AppOutcome:
    """A business outcome's *detector*, owned by the application.

    The wording is a fact about the app, shared by every capability that meets that screen, so
    it is taught once here and inherited by name. Whether a particular flow can return it is
    the capability's declaration, part of the contract its caller branches on.
    """

    name: str
    detector_kind: str
    detector_value: str
    description: str = ""
    result_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SignOnStep:
    """One step of the sign-on recipe.

    The same shape a capability step has — an action, a target described by what is on screen,
    a value — and never serialized into an artifact, because its value is a credential.
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
        vendor: str | None = None,
        base_url_pattern: str = "",
        entry_url: str = "",
        fault_url: str = "",
        max_restarts: int = 1,
        max_escalations_per_step: int = 2,
        business_outcomes: tuple[AppOutcome, ...] = (),
        volatile_text: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.vendor = vendor
        # Which installs an artifact recorded here applies to. Defaults to the allowlist's
        # first pattern, usually the same set.
        self.base_url_pattern = base_url_pattern or (
            allowed_url_patterns[0] if allowed_url_patterns else ""
        )
        # A per-institution fact, so a second tenant overrides this and nothing else.
        self.entry_url = entry_url
        # The fault harness, if the app has one. Deliberately *outside* the allowlist: an
        # agent that could arm its own faults could disarm them.
        self.fault_url = fault_url
        self.sign_on = sign_on
        # What the discovery model is told it is looking at — a fact about the app.
        self.surface = surface
        self.app_errors = app_errors
        self.escalations = escalations
        self.allowed_url_patterns = allowed_url_patterns
        self.allowed_actions = allowed_actions
        self.risky_disposition = risky_disposition
        self.recoveries = recoveries
        self.redact_patterns = redact_patterns
        self.risky_intent_patterns = risky_intent_patterns
        # Two budgets bounding the ways a run can decline to finish. Here rather than in the
        # engine because both are judgements about an application:
        #   max_restarts              a session that dies twice is not transient.
        #   max_escalations_per_step  an operator can resume without clearing the condition;
        #                             unbounded, the run parks forever holding the only session.
        self.max_restarts = max_restarts
        self.max_escalations_per_step = max_escalations_per_step
        # Detectors a capability on this app may inherit by name.
        self.business_outcomes = business_outcomes
        # Lines that change while nothing is happening — a countdown, a clock, a "last
        # refreshed" stamp. Excluded from the settle comparison, where they would stop any
        # two frames from ever agreeing.
        self.volatile_text = volatile_text
        self._url_res = tuple(re.compile(p) for p in allowed_url_patterns)
        self._intent_res = tuple(re.compile(p) for p in risky_intent_patterns)

    @classmethod
    def load(cls, path: Path) -> Policy:
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        _check_detector_kinds(path, raw)
        return cls(
            app=str(raw.get("app", path.stem)),
            vendor=raw.get("vendor"),
            base_url_pattern=str(raw.get("base_url_pattern", "")),
            entry_url=str(raw.get("entry_url", "")),
            fault_url=str(raw.get("fault_url", "")),
            allowed_url_patterns=tuple(raw.get("allowed_url_patterns", ())),
            # An unlisted primitive is denied, so an empty or missing list denies everything.
            allowed_actions=frozenset(
                Primitive(a) for a in raw.get("allowed_actions", ())
            ),
            risky_disposition=str(raw.get("risky_disposition", RiskDisposition.CONFIRM.value)),
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
            volatile_text=tuple(raw.get("volatile_text", ())),
            max_restarts=int(raw.get("max_restarts", 1)),
            max_escalations_per_step=int(raw.get("max_escalations_per_step", 2)),
            business_outcomes=tuple(
                AppOutcome(
                    name=str(o["name"]),
                    detector_kind=str(o["detector"]["kind"]),
                    detector_value=str(o["detector"].get("value", "")),
                    description=str(o.get("description", "")),
                    result_fields=tuple(o.get("result_fields", ())),
                )
                for o in raw.get("business_outcomes", ())
            ),
        )

    def app_ref(self) -> AppRef:
        """The identity a capability recorded against this app carries. Sourced here, so every
        artifact naming an app names one that has a policy file."""
        return AppRef(
            name=self.app,
            vendor=self.vendor,
            base_url_pattern=self.base_url_pattern,
        )

    def check_url(self, url: str) -> None:
        """Raise PolicyDenied if the URL is outside the allowlist. Evaluated on navigate *and*
        after every action, because a click can navigate."""
        if not any(r.match(url) for r in self._url_res):
            raise PolicyDenied("allowlist", f"{url} is not in the permitted URL patterns")

    def decide(self, action: Primitive, risk: Risk, intent: str) -> PolicyDecision:
        """The same verdict `check_action` reaches, as a record instead of a raise.

        Returns the whole decision, allows included, so evidence shows what the agent was
        permitted *here* and a risk promotion leaves a trace. `check_action` is the raising
        wrapper over this.
        """
        declared = risk.value
        if action not in self.allowed_actions:
            return PolicyDecision(
                action=action.value,
                declared_risk=declared,
                effective_risk=declared,
                disposition="denied",
                rule="allowlist",
                detail=f"action {action.value!r} not permitted",
                intent=intent,
            )

        promoted = self.promoting_pattern(risk, intent)
        effective = Risk.RISKY if (risk is Risk.RISKY or promoted) else Risk.SAFE

        if effective is Risk.SAFE:
            return PolicyDecision(
                action=action.value,
                declared_risk=declared,
                effective_risk=effective.value,
                disposition=RiskDisposition.ALLOW.value,
                rule="allowlist",
                intent=intent,
            )
        if self.risky_disposition == RiskDisposition.BLOCK:
            return PolicyDecision(
                action=action.value,
                declared_risk=declared,
                effective_risk=effective.value,
                disposition="denied",
                rule="risk",
                detail="risky action blocked by policy",
                promoted_from=declared if promoted else None,
                intent=intent,
            )
        return PolicyDecision(
            action=action.value,
            declared_risk=declared,
            effective_risk=effective.value,
            disposition=str(self.risky_disposition),
            rule="risk",
            detail=(f"intent matches {promoted!r}" if promoted else "declared risky"),
            promoted_from=declared if promoted else None,
            intent=intent,
        )

    def check_action(self, action: Primitive, risk: Risk, intent: str) -> str:
        """Return a RiskDisposition, or raise PolicyDenied. `intent` is carried through so a
        denial names what was refused rather than only the primitive."""
        decision = self.decide(action, risk, intent)
        if decision.disposition == "denied":
            raise PolicyDenied(
                decision.rule or "policy", f"{decision.detail} ({intent})"
            )
        return decision.disposition

    def promoting_pattern(self, declared: Risk, intent: str) -> str | None:
        """Which risky-intent pattern raised this step, if one did.

        The pattern itself, not a boolean, so a promotion is reported with its cause.

        One-directional on purpose — `safe` can become `risky`, never the reverse. A recording
        that mislabels a submit as safe is the expensive failure; a read misclassified as risky
        costs one confirmation.
        """
        if declared is Risk.RISKY:
            return None
        return next((r.pattern for r in self._intent_res if r.search(intent)), None)

    def match_recovery(self, observed_text: str) -> Recovery | None:
        return next(
            (r for r in self.recoveries if _matches(r, observed_text)),
            None,
        )

    def match_app_error(self, observed_text: str) -> Condition | None:
        return next((c for c in self.app_errors if _matches(c, observed_text)), None)

    def match_escalation(self, observed_text: str) -> Condition | None:
        return next((c for c in self.escalations if _matches(c, observed_text)), None)

    def outcome(self, name: str) -> AppOutcome | None:
        """The app-level detector a capability inherits under this name."""
        return next((o for o in self.business_outcomes if o.name == name), None)


def _matches(condition: Any, observed_text: str) -> bool:
    """Does this declared condition hold on what the frame says?

    Text only, and `load` refuses any other kind rather than letting this return False forever:
    a recovery that never fires is indistinguishable from one that is not needed.
    """
    return (
        condition.detector_kind == "text_present"
        and bool(condition.detector_value)
        and condition.detector_value.casefold() in observed_text.casefold()
    )


def _check_detector_kinds(path: Path, raw: dict[str, Any]) -> None:
    """Reject a detector this file matches with but cannot evaluate."""
    found: list[tuple[str, str]] = []
    for section in ("recoveries", "app_errors", "escalations", "business_outcomes"):
        for entry in raw.get(section, ()) or ():
            kind = str((entry.get("detector") or {}).get("kind", ""))
            found.append((f"{section}.{entry.get('name', '?')}", kind))
    if raw.get("sign_on"):
        kind = str((raw["sign_on"].get("detector") or {}).get("kind", ""))
        found.append(("sign_on", kind))

    bad = [f"{where} declares {kind!r}" for where, kind in found
           if kind not in _SUPPORTED_DETECTOR_KINDS]
    if bad:
        supported = ", ".join(sorted(_SUPPORTED_DETECTOR_KINDS))
        raise ValueError(
            f"{path}: unsupported detector kind ({'; '.join(bad)}). Supported: {supported}"
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
