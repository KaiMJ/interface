"""What a run returns.

The result contract carries the error taxonomy, and the taxonomy is the point — four terminal
classes, never conflated:

  SUCCESS           checkpoint passed, declared outputs extracted
  BUSINESS_OUTCOME  a legitimate answer the caller branches on ("no such member")
  ESCALATED         stopped and handed to a human; may resume
  FAILURE           something we do not understand
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .common import Bbox, Frozen
from .elements import SettledBy


class RunStatus(str, Enum):
    # Written before the first step and replaced when the run ends, so a run being watched
    # reads as in-flight rather than as whatever the default was.
    RUNNING = "running"
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    ESCALATED = "escalated"
    FAILURE = "failure"


class FailureKind(str, Enum):
    """Why we stopped. Each maps to a distinct operator action; two entries prompting the same
    response would be one entry."""

    RESOLUTION_EXHAUSTED = "resolution_exhausted"   # no tier located the target
    TARGET_MISMATCH = "target_mismatch"             # resolved region says the wrong thing
    UNEXPECTED_OVERLAY = "unexpected_overlay"       # undeclared dialog on top of the target
    RECOVERY_EXHAUSTED = "recovery_exhausted"       # a declared handler ran out of budget
    CHECKPOINT_FAILED = "checkpoint_failed"         # action executed, state is not what we expected
    WRONG_SCREEN = "wrong_screen"                   # not the state this step expects to act on
    AMBIGUOUS_MATCH = "ambiguous_match"             # predicate matched more than one record
    SCAN_INCONCLUSIVE = "scan_inconclusive"         # hit max_advances with content still changing
    POLICY_DENIED = "policy_denied"                 # allowlist or risk rule refused the action
    EXTRACTION_FAILED = "extraction_failed"         # could not read a declared output
    OUTPUT_REJECTED = "output_rejected"             # read it; it is not a value this may return
    TIMEOUT = "timeout"
    APP_ERROR = "app_error"                         # the target application itself errored
    MAX_STEPS = "max_steps"                         # discovery only
    INTERNAL = "internal"


class ResolutionTier(str, Enum):
    """Which tier of the resolver ladder produced the coordinate. Recorded on every step;
    aggregated across runs, anchors decaying into bbox fallbacks mean the app moved."""

    ANCHOR_TEXT = "anchor_text"
    ROLE_NAME = "role_name"
    RECORDED_BBOX = "recorded_bbox"
    VLM_GATED = "vlm_gated"       # off on the replay path; see REPORT §1
    NONE = "none"


class StepStatus(str, Enum):
    OK = "ok"
    RECOVERED = "recovered"       # a declared recoverable condition fired and was handled
    SKIPPED = "skipped"
    FAILED = "failed"


class Phases(Frozen):
    """Where a step's wall clock went.

    `duration_ms` says a step took 5.6s; this says 4.8s of it was two OCR passes. Model,
    browser and resolver are collectively a rounding error against perception. Buckets are
    disjoint and sum to slightly under `duration_ms`; the remainder is bookkeeping.
    """

    observe_ms: int = 0        # settling and interpreting frames
    observations: int = 0      # how many full perceptions this step paid for
    resolve_ms: int = 0        # the ladder, pure computation over an observation
    act_ms: int = 0            # the primitive itself: the driver's round trip
    verify_ms: int = 0         # checkpoint polling, excluding the observing it did


class PolicyDecision(Frozen):
    """What the guardrail decided about this step, recorded whether or not it refused.

    Recording the *allow* matters: a denial leaves a mark by stopping the run, an allow leaves
    none, and the auditable question is what the agent was permitted to do *here*.
    `promoted_from` records policy raising a declared `safe` to `risky` from its intent, the
    backstop against a mislabelled recording.
    """

    action: str                            # the primitive that was checked
    declared_risk: str                     # what the artifact claimed
    effective_risk: str                    # what policy concluded
    disposition: str                       # allow | confirm | block | denied
    rule: str | None = None                # which rule decided, when one did
    detail: str | None = None              # the denial message, or the pattern that promoted
    promoted_from: str | None = None       # set when safe was raised to risky
    intent: str = ""                       # what was classified; a denial without it is unreadable


class TierAttempt(Frozen):
    """One rung of the resolver ladder, and what it did.

    A miss is as informative as a hit: an anchor gone from the screen and an anchor matching
    three elements are different applications and different fixes, and the winning tier alone
    does not distinguish them.
    """

    tier: ResolutionTier
    outcome: str                           # matched | miss | skipped | error
    candidates: int = 0                    # how many elements the tier matched
    matched_text: str | None = None
    detail: str | None = None              # why it was skipped or missed


class ResolutionTrace(Frozen):
    """The full ladder walk for one target."""

    target_desc: str = ""
    anchor_text: str | None = None         # rendered against the caller's inputs
    relation: str = "self"
    attempts: tuple[TierAttempt, ...] = ()
    tier: ResolutionTier = ResolutionTier.NONE
    candidates: int = 1
    drift: bool = False
    bbox: Bbox | None = None               # where the ladder landed
    point: tuple[float, float] | None = None


class ModelTurn(Frozen):
    """The decision the model made on this step. Discovery only.

    What it was shown, which mark it chose, and what the loop did with the answer — facts no
    other record holds, and the only alternative to `intent`, the model's gloss on itself.
    """

    call: str                              # the tool name: click | type | finish | escalate | ...
    # What it said alongside the call, verbatim, as against `intent`, its gloss on itself.
    text: str = ""
    # The chain of thought behind the call. Separate from `text` because a forced tool call
    # leaves `content` empty, so a reasoning model's whole deliberation is here or nowhere.
    reasoning: str = ""
    # What the model was shown: the goal, declared inputs, this frame's candidates, and the run
    # so far — so a mark chosen from a list that never held the right element reads as the
    # perception failure it is. The system prompt is identical every step, written once per run.
    prompt: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    intent: str = ""
    expect: str | None = None
    mark: int | None = None                # the numbered box it chose
    element_id: str | None = None          # which element that mark actually was
    element_label: str | None = None       # …and what it said, measured not claimed
    anchor_proposed: str | None = None     # the model's own answer for a durable anchor
    anchor_recorded: str | None = None     # what survived falsification
    # The same pair for the expectation: `expect` is what the model proposed, this is what
    # became a checkpoint. None here beside a value there is a refutation.
    expect_recorded: str | None = None
    candidates_marked: int = 0             # numbered on the screenshot, all selectable
    candidates_listed: int = 0             # …and described in the prompt's candidate list
    latency_ms: int = 0
    verdict: str = ""                      # kept | kept_without_checkpoint | discarded | rejected
    detail: str | None = None


class Evidence(Frozen):
    """Pointers, not payloads, keeping large binaries out of anything that might be logged or
    sent to a model.

    Two frames per step: `screenshot` is the screen the step *acted on*, the only frame on
    which its resolved region means anything, and `after` is what the action produced and what
    the checkpoint saw.
    """

    screenshot: str | None = None             # the screen the step acted on
    after: str | None = None                  # what the action produced
    annotated_screenshot: str | None = None   # with the set-of-marks overlay
    observation: str | None = None            # serialized Observation


class StepResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: int
    intent: str = ""
    status: StepStatus
    resolution: ResolutionTier = ResolutionTier.NONE
    # A drift signal, like `resolution`: settling by text on every step means the surface has
    # begun to animate.
    settled_by: SettledBy = SettledBy.UNSET
    duration_ms: int = 0
    # 1 on the ordinary path. Above it means `on_error: retry` or a recovery that cleared while
    # the checkpoint still did not hold; either way the step was safe, because a risky one is
    # never re-executed.
    attempts: int = 1
    # On a checkpoint failure, the whole debugging story: what we asserted, what the screen
    # said.
    expected: str | None = None
    observed: str | None = None
    recovery_applied: str | None = None
    # Anything the engine did that the artifact does not say: an interstitial cleared before
    # acting, a URL rebased onto this deployment, a retry and why.
    note: str | None = None
    # Why the step was allowed, how its target was found, and — on discovery — what the model
    # was shown and chose.
    policy: PolicyDecision | None = None
    resolution_trace: ResolutionTrace | None = None
    phases: Phases = Phases()
    model_turn: ModelTurn | None = None
    evidence: Evidence = Evidence()


class FailureDetail(Frozen):
    kind: FailureKind
    step_id: int | None = None
    message: str
    expected: str | None = None
    observed: str | None = None
    # Where on screen, when we know. The console draws it over the step's frame.
    region: Bbox | None = None


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
    # Which application this ran against. Redundant with the capability's own AppRef, and
    # carried so listing runs does not have to load the artifact.
    app: str = ""
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
    app: str = ""
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
