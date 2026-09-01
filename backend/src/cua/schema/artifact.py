"""The capability artifact: the contract between the discovery run that wrote it and the
agents that invoke it.

Three readers at once — a *calling agent* needing typed inputs, outputs and outcomes to branch
on; a *human reviewer* who must approve it without watching a video; and the *replay engine*,
which executes it with no model present.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import (
    Bbox,
    Frozen,
    MatchMode,
    Normalizer,
    Risk,
    Template,
    Unit,
    ValueType,
    Viewport,
)

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Targeting
# ---------------------------------------------------------------------------


class Relation(str, Enum):
    """Where the real target sits relative to the thing we can name.

    The control a step acts on is usually not the thing with words on it: a form field is an
    empty box beside a label, a balance is the cell right of "Available Balance". Vision has no
    `for=` attribute to follow, so the relationship is recorded instead.
    """

    SELF = "self"
    RIGHT_OF = "right_of"
    BELOW = "below"


class Target(Frozen):
    """How a step identifies the control it acts on, most portable first.

    The resolver walks these in order and records which tier won, a free drift signal: anchors
    decaying into `bbox` fallbacks are an early warning long before a hard failure.

      1. anchor_text  visible text at/near the target. Survives rebranding, and may contain
                      `{{param}}` — "the row for member {{member_id}}".
      2. role + name  semantic match against detected elements.
      3. bbox         the recorded position. Using it logs a drift event.

    `intent` and `target_desc` are not decoration: policy classifies `intent` for risk, and the
    pre-click assertion checks the resolved region actually says `target_desc`.
    """

    intent: str                                   # "click the Transfer button"
    target_desc: str                              # "primary submit button on the transfer form"

    anchor_text: Template | None = None
    anchor_match: MatchMode = MatchMode.CONTAINS
    role: str | None = None
    name: str | None = None
    bbox: Bbox | None = None

    # Resolve the anchor, then step to its neighbour: how a step types into the box beside
    # "User ID", which has no text of its own to match.
    relation: Relation = Relation.SELF
    relation_index: int = 0               # nth neighbour in that direction
    # For a table value, the header above it. Beats `relation_index` when both are recorded:
    # an index counts the cells filled on one row, so a blank status or an extra column shifts
    # it silently onto the wrong cell.
    column: Template | None = None

    # Click point within the resolved box, centre by default — how a step targets the "View"
    # button at the right edge of a matched row. Bounded: outside the box is not a click on it.
    offset: tuple[Unit, Unit] = (0.5, 0.5)

    normalize: tuple[Normalizer, ...] = (Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS)


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


class CheckKind(str, Enum):
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    URL_MATCHES = "url_matches"
    ELEMENT_VISIBLE = "element_visible"
    FIELD_VALUE_MATCHES = "field_value_matches"
    REGION_STABLE = "region_stable"       # two consecutive frames hash-equal


class Checkpoint(Frozen):
    """An assertion that the expected state was actually reached.

    Per step, not only at the end: a wrong click at step 3 should fail at step 3 with a legible
    diff, not a plausible wrong output at step 9. Also how this system waits — polling a
    checkpoint until timeout, rather than sleeping.
    """

    kind: CheckKind
    value: Template | None = None
    match: MatchMode = MatchMode.CONTAINS
    scope: Target | None = None           # restrict the check to a region
    timeout_ms: int = 5000
    normalize: tuple[Normalizer, ...] = (Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS)


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


class Primitive(str, Enum):
    """The action space. Identical in discovery and replay: the agent can only express what
    replay can execute, so recordings are replayable by construction."""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    KEY = "key"
    SCROLL = "scroll"
    EXTRACT = "extract"
    WAIT = "wait"
    ASSERT = "assert"


class OnError(str, Enum):
    """What replay does when this step's checkpoint does not hold.

    `RETRY` is legal only on a `safe` step and validation refuses the combination: a risky step
    asking to run twice is a contradiction, not a preference. The engine re-checks against
    policy's *effective* risk, catching a step promoted to risky from its intent.
    """

    HARD_FAIL = "hard_fail"
    ESCALATE = "escalate"
    RETRY = "retry"


class StepBase(Frozen):
    id: int
    risk: Risk = Risk.SAFE
    # Which declared screen this step expects, checked before it acts, so a flow that went
    # elsewhere names where it is rather than reporting a missing target.
    screen: str | None = None
    # Verified after the action. This is the step's own success condition.
    checkpoint: Checkpoint | None = None
    on_error: OnError = OnError.HARD_FAIL
    # Re-executions allowed when `on_error` is RETRY, ignored otherwise.
    retries: int = 0
    note: str | None = None               # reviewer-facing


class ActStep(StepBase):
    """A single primitive against a single resolved target."""

    kind: Literal["act"] = "act"
    action: Primitive
    target: Target | None = None          # None for navigate / wait / key
    value: Template | None = None         # url, text to type, key name, scroll amount
    # For EXTRACT: which declared output this populates.
    extract_as: str | None = None


class ScanAdvance(str, Enum):
    SCROLL = "scroll"
    CLICK_ANCHOR = "click_anchor"         # pagination: "Next"
    NONE = "none"


class PredicateMatch(str, Enum):
    ROW_CONTAINS_ALL = "row_contains_all"
    ROW_CONTAINS_ANY = "row_contains_any"
    CELL_EQUALS = "cell_equals"


class Predicate(Frozen):
    """A data-dependent match, evaluated deterministically over detected elements."""

    match: PredicateMatch = PredicateMatch.ROW_CONTAINS_ALL
    terms: tuple[Template, ...]
    normalize: tuple[Normalizer, ...] = (
        Normalizer.CASEFOLD,
        Normalizer.COLLAPSE_WS,
        Normalizer.STRIP_CURRENCY,
    )


class Scan(Frozen):
    """How to reach the next screenful, and how far to keep trying.

    No `stop_when` field, deliberately: exhaustion has two signals and both follow from
    `advance` — a screenful identical to one already seen, and a pagination anchor no longer on
    the page. A field selecting between them could only disable one of two correct checks.
    """

    advance: ScanAdvance = ScanAdvance.SCROLL
    anchor: Template | None = None        # for CLICK_ANCHOR, e.g. "Next"
    # Never a full region height: a row straddling the boundary would be skipped and
    # reported as a false not-found.
    overlap: Unit = 0.15
    max_advances: int = 10


class MultiplePolicy(str, Enum):
    FIRST = "first"                       # tolerable on a read
    ESCALATE = "escalate"                 # required on a write: the wrong record is unrecoverable
    FAIL = "fail"


class ScopeExtent(str, Enum):
    BELOW = "below"
    ABOVE = "above"
    WITHIN = "within"


class FindAndActStep(StepBase):
    """Find the thing matching a predicate, then act on it.

    A first-class step type because a target's position in a list is a function of the data,
    not the layout: a recorded `scroll, scroll, click(y)` cannot express an absent record or an
    ambiguous match. Evaluation stays deterministic — observe scope, evaluate, advance, no
    model. Pagination and bulk extraction are the same primitive with a different
    `scan.advance` / `on_found_action`.
    """

    kind: Literal["find_and_act"] = "find_and_act"

    # Located by anchor text (a column-header row), not a fixed box, so a banner above the
    # table does not invalidate the scope.
    scope: Target
    scope_extent: ScopeExtent = ScopeExtent.BELOW

    predicate: Predicate
    scan: Scan = Scan()

    on_found_action: Primitive = Primitive.CLICK
    on_found_offset: tuple[Unit, Unit] = (0.5, 0.5)
    on_found_extract_as: str | None = None
    # Which cell of the matched row to read, named by its column header: a row is not a value.
    # Found by header text and horizontal overlap.
    on_found_extract_column: Template | None = None
    collect_all: bool = False             # "the last N transactions"
    limit: int | None = None

    # Exhausting the list without a match is a legitimate answer, so it maps to a business
    # outcome. Hitting max_advances with content still moving is a hard failure: absence and
    # quitting early are not the same thing.
    on_not_found_outcome: str | None = None
    on_multiple: MultiplePolicy = MultiplePolicy.ESCALATE


Step = Annotated[ActStep | FindAndActStep, Field(discriminator="kind")]


# ---------------------------------------------------------------------------
# Contract: inputs, outputs, outcomes
# ---------------------------------------------------------------------------


class Constraints(Frozen):
    pattern: str | None = None
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] | None = None
    not_equal_to: str | None = None       # name of another input


class InputSpec(Frozen):
    name: str
    type: ValueType
    required: bool = True
    description: str = ""
    example: str | None = None
    constraints: Constraints | None = None
    # Never logged, never written unredacted to evidence, never in a prompt. Supplied at
    # execution time and substituted below the serialization boundary.
    sensitive: bool = False


class OutputSpec(Frozen):
    name: str
    type: ValueType
    description: str = ""
    # Which step produced it. Declared, not "whatever the model happened to read": the
    # caller's contract has to be stable across runs.
    from_step: int
    normalize: tuple[Normalizer, ...] = (Normalizer.COLLAPSE_WS,)
    required: bool = True
    # `InputSpec.constraints` pointed the other way. A checkpoint proves we are on the right
    # screen, not that what was read off it is a value this capability may return: `18204.55`
    # misread as `1820455` is type-valid and passes every assertion. Authored at review time,
    # since a bound derived from a recording that saw one value is that value or a guess.
    constraints: Constraints | None = None


class BusinessOutcome(Frozen):
    """A legitimate alternative result, not a failure.

    "No such member" is an answer the caller needs, so it is declared per capability and the
    calling agent knows every shape it may receive. Detectors run before the success check at
    each step; first match wins and the run stops cleanly.

    `detector` may be omitted and is then inherited from app policy by name. *What the screen
    says* belongs to the application; *whether this flow can return that answer* belongs to the
    capability, so the policy holds the detector once and each capability opts in by name.

    `cua learn-outcome` demonstrates an outcome by replaying with inputs that reach it;
    `cua diagnose` reads a run that stopped on an undeclared screen and proposes the policy
    stanza. Neither edits `policies/<app>.yaml` itself: a system that writes its own guardrails
    is not one.
    """

    name: str
    description: str = ""
    detector: Checkpoint | None = None
    result_fields: dict[str, ValueType] = Field(default_factory=dict)
    # Has this outcome actually been seen to fire?
    #
    # A recording cannot confirm one: the outcome describes a screen the successful run did not
    # visit, so synthesis can only *refute* a proposed detector (see
    # `discovery.synthesize._falsify`). An unverified outcome stays in the artifact, where a
    # reviewer can see what the model guessed, and out of the agent-facing manifest, where it
    # would be a promise. Inherited, taught or hand-authored outcomes are verified by
    # construction, which is why this defaults to true.
    verified: bool = True


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class Screen(Frozen):
    """A recognisable state of the application: the smallest useful piece of a UI model, and
    the seam a fuller one would grow from.

    Two institutions on the same vendor product have the same screens with different branding,
    so this is where per-tenant overrides attach. Declared and enforced — a step names the
    screen it expects and replay asserts it before acting — but not derived: telling a screen
    from the record on it needs two runs with different inputs.
    """

    name: str
    signature: Checkpoint


class Status(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class AppRef(Frozen):
    """Which application this capability drives. `base_url_pattern` is a pattern, not a
    literal, so one artifact can point at another institution's install of the same vendor
    product (REPORT §4)."""

    name: str
    vendor: str | None = None
    base_url_pattern: str


class Recording(Frozen):
    """Provenance. Not part of the contract; part of the audit trail."""

    run_id: str
    model: str
    surface: Literal["browser", "desktop"] = "browser"
    viewport: Viewport
    recorded_at: str
    step_count: int = 0


class Capability(BaseModel):
    """A saved, versioned, agent-invocable capability."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    id: str                               # cap_get_account_balance
    version: int = 1
    status: Status = Status.DRAFT

    goal: str                             # the natural-language goal it satisfies
    description: str = ""

    app: AppRef
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    # Empty is legitimate: declaring no screens makes no claim about where it is.
    screens: list[Screen] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)

    # The final assertion. Screenshots are evidence, not the decision mechanism.
    success: Checkpoint

    business_outcomes: list[BusinessOutcome] = Field(default_factory=list)

    recording: Recording | None = None

    @property
    def ref(self) -> str:
        return f"{self.id}@v{self.version}"

    @model_validator(mode="after")
    def _referentially_intact(self) -> Capability:
        """Reject an artifact whose internal references do not resolve.

        Only claims the artifact makes about itself. Whether an anchor exists or a checkpoint
        can pass are claims about the application, answerable only by running it.
        """
        problems: list[str] = []

        step_ids = [s.id for s in self.steps]
        duplicates = {i for i in step_ids if step_ids.count(i) > 1}
        if duplicates:
            problems.append(f"duplicate step ids: {sorted(duplicates)}")
        by_id = {s.id: s for s in self.steps}

        declared = {i.name for i in self.inputs}
        for name in declared:
            if not name.isidentifier():
                problems.append(f"input {name!r} is not a usable placeholder name")
        for spec in self.inputs:
            other = spec.constraints.not_equal_to if spec.constraints else None
            if other is not None and other not in declared:
                problems.append(
                    f"input {spec.name!r} is constrained against undeclared input {other!r}"
                )

        # `from_step` pointing at a click yields nothing, and the caller's contract silently
        # loses a field.
        for out in self.outputs:
            step = by_id.get(out.from_step)
            if step is None:
                problems.append(f"output {out.name!r} reads from missing step {out.from_step}")
            elif not _extracts(step):
                problems.append(
                    f"output {out.name!r} reads from step {out.from_step}, "
                    "which does not extract anything"
                )

        screens = {s.name for s in self.screens}
        for step in self.steps:
            if step.screen is not None and step.screen not in screens:
                problems.append(f"step {step.id} expects undeclared screen {step.screen!r}")

            # `risky` says the step is not reversible; `on_error: retry` asks to run it
            # twice. A file saying both is asking for a duplicate transfer.
            if step.on_error is OnError.RETRY and step.risk is not Risk.SAFE:
                problems.append(
                    f"step {step.id} is risky and declares on_error: retry; "
                    "an irreversible action cannot be retried"
                )
            if step.on_error is OnError.RETRY and step.retries < 1:
                problems.append(
                    f"step {step.id} declares on_error: retry with no retry budget "
                    "(set `retries`)"
                )

        # An unknown {{placeholder}} raises at render time, mid-run. Here it is a rejected
        # file.
        for where, text in _templates(self):
            for name in _PLACEHOLDER.findall(text):
                if name not in declared:
                    problems.append(f"{where} uses undeclared input {{{{{name}}}}}")

        try:
            re.compile(self.app.base_url_pattern)
        except re.error as e:
            problems.append(f"app.base_url_pattern is not a valid regex: {e}")

        if problems:
            raise ValueError(f"{self.id}: " + "; ".join(sorted(set(problems))))
        return self


_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def _extracts(step: Step) -> bool:
    if isinstance(step, ActStep):
        return step.action is Primitive.EXTRACT
    return step.on_found_action is Primitive.EXTRACT or step.on_found_extract_as is not None


def _templates(cap: Capability) -> list[tuple[str, str]]:
    """Every string in the artifact that is rendered against the caller's inputs."""
    found: list[tuple[str, str]] = []

    def check(where: str, checkpoint: Checkpoint | None) -> None:
        if checkpoint is None:
            return
        if checkpoint.value:
            found.append((where, checkpoint.value))
        target(f"{where} scope", checkpoint.scope)

    def target(where: str, t: Target | None) -> None:
        if t is not None and t.anchor_text:
            found.append((where, t.anchor_text))

    check("success", cap.success)
    for outcome in cap.business_outcomes:
        check(f"outcome {outcome.name!r}", outcome.detector)
    for screen in cap.screens:
        check(f"screen {screen.name!r}", screen.signature)
    for step in cap.steps:
        check(f"step {step.id} checkpoint", step.checkpoint)
        if isinstance(step, ActStep):
            target(f"step {step.id} target", step.target)
            if step.value:
                found.append((f"step {step.id} value", step.value))
        else:
            target(f"step {step.id} scope", step.scope)
            found += [(f"step {step.id} predicate", t) for t in step.predicate.terms]
            if step.scan.anchor:
                found.append((f"step {step.id} scan anchor", step.scan.anchor))
            if step.on_found_extract_column:
                found.append((f"step {step.id} column", step.on_found_extract_column))
    return found
