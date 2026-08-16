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

from dataclasses import dataclass
from pathlib import Path

from ..schema import Primitive, Risk


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


class Policy:
    """Loaded from `policies/<app>.yaml`."""

    def __init__(
        self,
        allowed_url_patterns: tuple[str, ...],
        allowed_actions: frozenset[Primitive],
        risky_disposition: str,
        recoveries: tuple[Recovery, ...],
        redact_patterns: tuple[str, ...],
    ) -> None:
        self.allowed_url_patterns = allowed_url_patterns
        self.allowed_actions = allowed_actions
        self.risky_disposition = risky_disposition
        self.recoveries = recoveries
        self.redact_patterns = redact_patterns

    @classmethod
    def load(cls, path: Path) -> Policy:
        raise NotImplementedError

    def check_url(self, url: str) -> None:
        """Raise PolicyDenied if the URL is outside the allowlist.

        Evaluated on navigate *and* after every action, because a click can
        navigate. Checking only on explicit navigation leaves the obvious hole.
        """
        raise NotImplementedError

    def check_action(self, action: Primitive, risk: Risk, intent: str) -> str:
        """Return a RiskDisposition, or raise PolicyDenied.

        `intent` is carried through purely so a denial is debuggable: 'denied
        click' is useless in an audit log, 'denied: submit $500 transfer from
        29883' is not.
        """
        raise NotImplementedError

    def match_recovery(self, observed_text: str) -> Recovery | None:
        raise NotImplementedError
