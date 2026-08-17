"""What a run returns.

The result contract carries the error taxonomy, and the taxonomy is the point.
Four classes, never conflated:

  SUCCESS           checkpoint passed, declared outputs extracted
  BUSINESS_OUTCOME  a legitimate answer the caller must branch on ("no such member")
  ESCALATED         stopped and handed to a human; may resume
  FAILURE           something we do not understand — stop, keep evidence, surface it

Unknown states are hard failures by design. Under a vision-first perception model
we cannot enumerate every screen; guessing in a banking application is worse than
stopping.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import Frozen


class RunStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    ESCALATED = "escalated"
    FAILURE = "failure"


class FailureKind(str, Enum):
    """Why we stopped. Each maps to a distinct operator action, which is the test
    of whether a taxonomy is real: if two entries would prompt the same response,
    they should be one entry."""

    RESOLUTION_EXHAUSTED = "resolution_exhausted"   # no tier located the target
    TARGET_MISMATCH = "target_mismatch"             # resolved region says the wrong thing
    UNEXPECTED_OVERLAY = "unexpected_overlay"       # undeclared dialog on top of the target
    CHECKPOINT_FAILED = "checkpoint_failed"         # action executed, state is not what we expected
    WRONG_SCREEN = "wrong_screen"                   # not the state this step expects to act on
    AMBIGUOUS_MATCH = "ambiguous_match"             # predicate matched more than one record
    SCAN_INCONCLUSIVE = "scan_inconclusive"         # hit max_advances with content still changing
    POLICY_DENIED = "policy_denied"                 # allowlist or risk rule refused the action
    EXTRACTION_FAILED = "extraction_failed"         # could not read a declared output
    TIMEOUT = "timeout"
    APP_ERROR = "app_error"                         # the target application itself errored
    MAX_STEPS = "max_steps"                         # discovery only
    INTERNAL = "internal"


class ResolutionTier(str, Enum):
    """Which tier of the resolver ladder produced the coordinate.

    Recorded on every step. Aggregated across runs it is a drift canary that costs
    nothing: anchor resolutions decaying into bbox fallbacks means the app moved.
    """

    ANCHOR_TEXT = "anchor_text"
    ROLE_NAME = "role_name"
    RECORDED_BBOX = "recorded_bbox"
    VLM_GATED = "vlm_gated"       # off on the replay path; see REPORT §3
    NONE = "none"


class StepStatus(str, Enum):
    OK = "ok"
    RECOVERED = "recovered"       # a declared recoverable condition fired and was handled
    SKIPPED = "skipped"
    FAILED = "failed"


class Evidence(Frozen):
    """Pointers, not payloads. Keeps results small and keeps large binaries out of
    anything that might get logged or sent to a model."""

    screenshot: str | None = None
    annotated_screenshot: str | None = None   # with the set-of-marks overlay
    observation: str | None = None            # serialized Observation


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int
    intent: str = ""
    status: StepStatus
    resolution: ResolutionTier = ResolutionTier.NONE
    duration_ms: int = 0
    # On a checkpoint failure these two fields are the whole debugging story:
    # what we asserted, and what the screen actually said.
    expected: str | None = None
    observed: str | None = None
    recovery_applied: str | None = None
    evidence: Evidence = Evidence()


class FailureDetail(Frozen):
    kind: FailureKind
    step_id: int | None = None
    message: str
    expected: str | None = None
    observed: str | None = None


class OutcomeDetail(BaseModel):
    """A declared business outcome that fired."""

    model_config = ConfigDict(extra="forbid")

    name: str
    step_id: int | None = None
    fields: dict[str, Any] = Field(default_factory=dict)


class ReplayResult(BaseModel):
    """What an AI agent gets back from invoking a capability."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    capability: str                       # cap_x@v1
    status: RunStatus
    inputs: dict[str, Any] = Field(default_factory=dict)   # redacted before write

    outputs: dict[str, Any] = Field(default_factory=dict)  # SUCCESS only
    outcome: OutcomeDetail | None = None                   # BUSINESS_OUTCOME only
    failure: FailureDetail | None = None                   # FAILURE only
    intervention_id: str | None = None                     # ESCALATED only

    steps: list[StepResult] = Field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration_ms: int = 0
    evidence_dir: str = ""


class DiscoveryResult(BaseModel):
    """What a discovery run produces: a capability, or an explanation."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    goal: str
    status: RunStatus
    capability_ref: str | None = None     # written on success
    stop_reason: str = ""
    model: str = ""
    steps_taken: int = 0
    llm_calls: int = 0
    steps: list[StepResult] = Field(default_factory=list)
    failure: FailureDetail | None = None
    intervention_id: str | None = None
    started_at: str = ""
    finished_at: str = ""
    evidence_dir: str = ""
