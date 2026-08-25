"""What a run returns.

The result contract carries the error taxonomy, and the taxonomy is the point. Four
terminal classes, never conflated:

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
    # Written before the first step and replaced when the run ends, so a run being
    # watched reads as in-flight rather than as whatever the default was.
    RUNNING = "running"
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
    RECOVERY_EXHAUSTED = "recovery_exhausted"       # a declared handler ran out of budget
    CHECKPOINT_FAILED = "checkpoint_failed"         # action executed, state is not what we expected
    WRONG_SCREEN = "wrong_screen"                   # not the state this step expects to act on
    AMBIGUOUS_MATCH = "ambiguous_match"             # predicate matched more than one record
    SCAN_INCONCLUSIVE = "scan_inconclusive"         # hit max_advances with content still changing
    POLICY_DENIED = "policy_denied"                 # allowlist or risk rule refused the action
    EXTRACTION_FAILED = "extraction_failed"         # could not read a declared output
    OUTPUT_REJECTED = "output_rejected"              # read it; it is not a value this may return
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
    VLM_GATED = "vlm_gated"       # off on the replay path; see REPORT §1
    NONE = "none"


class StepStatus(str, Enum):
    OK = "ok"
    RECOVERED = "recovered"       # a declared recoverable condition fired and was handled
    SKIPPED = "skipped"
    FAILED = "failed"


class Phases(Frozen):
    """Where a step's wall clock went.

    `duration_ms` says a step took 5.6s; this says 4.8s of it was two OCR passes.
    Without the split, optimising is guessing — and the guess is wrong in an
    instructive way: the obvious suspects (the model, the browser, the resolver)
    are collectively a rounding error against perception, and perception is
    almost entirely one text-recognition call.

    The buckets are disjoint and sum to slightly less than `duration_ms`; the
    remainder is policy, bookkeeping and evidence writes, which are microseconds
    and not worth a bucket of their own.
    """

    observe_ms: int = 0        # settling and interpreting frames
    observations: int = 0      # how many full perceptions this step paid for
    resolve_ms: int = 0        # the ladder, pure computation over an observation
    act_ms: int = 0            # the primitive itself: the driver's round trip
    verify_ms: int = 0         # checkpoint polling, excluding the observing it did


class PolicyDecision(Frozen):
    """What the guardrail decided about this step, recorded whether or not it refused.

    The policy object is consulted before every action on both paths, and a verdict visible
    only when it raises cannot answer the question an auditor actually asks — not "what was
    this agent permitted to do" but "what was it permitted to do *here*". Recording the allow
    is the whole point: a denial leaves a mark by stopping the run, an allow leaves none.

    `promoted_from` is the interesting field, since policy may raise a declared `safe` to
    `risky` from its intent — the backstop against a mislabelled recording, invisible unless
    recorded.
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

    Only the winning tier used to survive, which makes the most common debugging
    question — "why did this fall through to the recorded box?" — unanswerable
    from evidence. A miss is as informative as a hit here: `anchor_text` missing
    because the anchor is no longer on screen and missing because it matched three
    elements are different applications and different fixes.
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

    Without it, "why did it click there" is answerable only from `intent`, which
    is the model's own gloss on itself. What it was shown (how many candidates,
    how many were truncated away), which mark it chose, and what the loop then did
    with the answer are all facts about the turn that no other record holds.

    `verdict` is the expect-check outcome from `loop._act`, which until now leaked
    into the artifact only as a prose `note` on the step.
    """

    call: str                              # the tool name: click | type | finish | escalate | ...
    # What it said alongside the call, and the arguments verbatim. `intent` below is
    # its gloss on itself; these are what it emitted, which is what you want when
    # the two disagree.
    text: str = ""
    # The chain of thought behind the call. Separate from `text` because they come
    # from different places and only one of them is usually populated: a forced tool
    # call leaves `content` empty, so a reasoning model's entire deliberation is here
    # or nowhere. Recorded rather than streamed, so the console and the audit trail
    # read the same bytes.
    reasoning: str = ""
    # What the model was shown to produce this call: the goal, the declared inputs,
    # the candidate list off this frame, and the run's history so far. Recorded
    # because "why did it click there" is not answerable from the answer alone —
    # a mark chosen from a list that never contained the right element is a
    # perception failure wearing a decision's clothes. The system prompt is
    # identical every step and is written once per run instead.
    prompt: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    intent: str = ""
    expect: str | None = None
    mark: int | None = None                # the numbered box it chose
    element_id: str | None = None          # which element that mark actually was
    element_label: str | None = None       # …and what it said, measured not claimed
    anchor_proposed: str | None = None     # the model's own answer for a durable anchor
    anchor_recorded: str | None = None     # what survived falsification
    # The same pair for the expectation. `expect` above is what the model proposed;
    # this is what became a checkpoint, and None here beside a value there is a
    # refutation — an assertion that held on this screen and would not have held on
    # the next member's.
    expect_recorded: str | None = None
    candidates_shown: int = 0
    candidates_truncated: int = 0
    latency_ms: int = 0
    verdict: str = ""                      # kept | kept_without_checkpoint | discarded | rejected
    detail: str | None = None


class Evidence(Frozen):
    """Pointers, not payloads — keeping results small and large binaries out of anything
    that might get logged or sent to a model.

    Two frames per step, and conflating them is a real bug rather than an aesthetic one.
    `screenshot` is the screen the step *acted on*, the one its target was resolved against
    and therefore the only frame on which the resolved region means anything; `after` is what
    the action produced, which is what the checkpoint was evaluated against. Storing only the
    second draws a step's target box onto a screen that never contained the target, and makes
    consecutive steps byte-identical, since one step's after-state is the next step's
    acted-on state.
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
    # A free drift signal, like `resolution`: a capability that starts settling by
    # text on every step is running against a surface that has begun to animate.
    settled_by: SettledBy = SettledBy.UNSET
    duration_ms: int = 0
    # 1 on the ordinary path. Above it means `on_error: retry` or a recovery that
    # cleared while the checkpoint still did not hold — and in both cases the step
    # was safe, because a risky one is never re-executed. Its own field rather than
    # buried in `note`: two attempts is degrading before it is failing.
    attempts: int = 1
    # On a checkpoint failure, the whole debugging story: what we asserted, and
    # what the screen said.
    expected: str | None = None
    observed: str | None = None
    recovery_applied: str | None = None
    # Anything the engine did that the artifact does not say: an interstitial
    # cleared before acting, a URL rebased onto this deployment, a retry and why.
    note: str | None = None
    # Why the step was allowed, how its target was found, and — on discovery — what
    # the model was shown and chose. All three are decisions the
    # system already made and used to throw away; keeping them is what turns the
    # console from a viewer of outcomes into something an operator can debug a
    # step with.
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
    # Where on screen, when we know. The console draws it over the step's frame,
    # which turns "an undeclared element covers the target" from a sentence an
    # operator has to reconstruct into a box they can see.
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
    # Which application this ran against. Redundant with the capability's own
    # AppRef and worth carrying anyway: a caller correlating results across apps,
    # and the console listing runs, both need it without loading the artifact.
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
