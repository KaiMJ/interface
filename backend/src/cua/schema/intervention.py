"""Human-in-the-loop control transfer.

The load-bearing idea is that control is a token with exactly one holder, and the
*session outlives the transfer*. The human does not get a fresh browser; they get
the same X display, the same Chromium process, the same cookies and the same
half-filled form the automation was looking at when it stopped.

    AUTOMATION --(escalate)--> PENDING --(operator takes control)--> HUMAN
         ^                                                             |
         '------------------- (resume) <---- RELEASING <---------------'

Who holds control is explicit state, not an implicit consequence of "nobody is
currently calling page.click()". Without that, an operator clicking during a run
races the automation on the same display.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import Frozen
from .results import Evidence, FailureKind


class Controller(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"
    NOBODY = "nobody"          # between transfers; nothing may act


class InterventionState(str, Enum):
    PENDING = "pending"        # raised, nobody has picked it up
    HUMAN_CONTROL = "human_control"
    RESOLVED = "resolved"      # human handed back, run continues
    ABORTED = "aborted"        # human ended the run
    EXPIRED = "expired"        # nobody came; session torn down


class InterventionReason(str, Enum):
    RESOLUTION_EXHAUSTED = "resolution_exhausted"
    TARGET_MISMATCH = "target_mismatch"
    UNEXPECTED_OVERLAY = "unexpected_overlay"
    AMBIGUOUS_MATCH = "ambiguous_match"
    RISKY_ACTION_CONFIRMATION = "risky_action_confirmation"
    POLICY_DENIED = "policy_denied"
    MAX_STEPS = "max_steps"
    AGENT_REQUESTED = "agent_requested"        # the discovery LLM emitted `escalate`


class InterventionRequest(BaseModel):
    """Raised when the system cannot safely proceed.

    Carries enough context for an operator to act without reading logs: which
    capability and goal, which step, what it was trying to do, what it saw, and a
    live screenshot.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    mode: str                              # "discovery" | "replay"
    capability: str | None = None
    goal: str = ""

    reason: InterventionReason
    failure_kind: FailureKind | None = None
    step_id: int | None = None
    step_intent: str = ""
    message: str = ""
    expected: str | None = None
    observed: str | None = None

    state: InterventionState = InterventionState.PENDING
    evidence: Evidence = Evidence()
    # Where the operator connects. Same display the automation is using.
    vnc_url: str | None = None
    raised_at: str = ""


class HumanAction(Frozen):
    """One observed operator input during a handoff.

    Captured at the X layer rather than by asking the operator what they did.
    Playwright cannot see a manual click — it did not issue it — so instrumenting
    the browser would leave the audit trail with a hole exactly where a human
    touched regulated data.
    """

    at: str
    kind: str                              # click | key | scroll | move | drag
    x: int | None = None
    y: int | None = None
    detail: str | None = None              # keysym or button; redacted for typed text


class InterventionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    outcome: str                           # "resume" | "abort"
    operator: str = "unknown"
    note: str = ""
    human_actions: list[HumanAction] = Field(default_factory=list)
    duration_ms: int = 0
    resolved_at: str = ""
    # On resume the runner re-observes rather than trusting a step counter: the
    # human may have advanced the app several screens.
    resumed_at_step: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
