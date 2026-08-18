"""The capability artifact.

This is the contract between the discovery run that wrote it and the AI agents
that will invoke it in production. Three readers have to be served at once:

  - a *calling agent*, which needs typed inputs, typed outputs, and a declared set
    of legitimate business outcomes so it can branch on them;
  - a *human reviewer*, who has to be able to approve it without watching a video;
  - the *replay engine*, which must be able to execute it with no model present.

Two shapes were rejected on purpose. A raw model transcript fails all three. A
bare click track (`click(0.42, 0.71)`) fails the reviewer, fails risk
classification — a coordinate cannot be judged reversible or not — and fails
cross-tenant reuse, since rebranding moves every pixel. Every step therefore
carries its semantic intent alongside its coordinate.
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

    The control a step acts on is very often not the thing with the words on it.
    A form field is an empty box beside a label; a balance is a cell to the right
    of "Available Balance"; a "View" button is at the end of a row identified by
    its account number. Under vision there is no `for=` attribute to follow, so
    the relationship has to be recorded — and once recorded it is more portable
    than either a coordinate or a role, because rebranding an app rarely moves the
    value out from beside its label.
    """

    SELF = "self"
    RIGHT_OF = "right_of"
    BELOW = "below"


class Target(Frozen):
    """How a step identifies the control it acts on.

    Ordered from most to least portable. The resolver walks these in order (see
    `cua.resolve.resolver`); the tier that succeeded is recorded on every step
    result, which gives a free drift signal — anchor resolutions starting to fall
    through to `bbox` is an early warning long before a hard failure.

      1. anchor_text  visible text at/near the target. Survives rebranding and
                      relayout. May contain `{{param}}`, which is what makes
                      data-dependent targeting possible ("the row for member
                      {{member_id}}").
      2. role + name  semantic match against detected elements.
      3. bbox         the recorded position. Correct until something above it on
                      the page changes height. Using it logs a drift event.

    `intent` and `target_desc` are not decoration: `intent` is what the policy
    layer classifies for risk, and `target_desc` is what the pre-click assertion
    checks the resolved region actually says.
    """

    intent: str                                   # "click the Transfer button"
    target_desc: str                              # "primary submit button on the transfer form"

    anchor_text: Template | None = None
    anchor_match: MatchMode = MatchMode.CONTAINS
    role: str | None = None
    name: str | None = None
    bbox: Bbox | None = None

    # Resolve the anchor, then step to its neighbour. `right_of` is how a step
    # types into the box beside "User ID" or reads the value beside "Available
    # Balance" without either one having any text of its own to match.
    relation: Relation = Relation.SELF
    relation_index: int = 0               # nth neighbour in that direction

    # Click point relative to the resolved box, 0..1. Defaults to its center.
    # Lets a step target the "View" button at the right edge of a matched row.
    # Bounded, because an offset outside the box is not a click on the box.
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

    Present per step, not only at the end. A wrong click at step 3 should fail at
    step 3 with a legible diff, not produce a plausible-looking wrong output at
    step 9. Also the reason there is no `sleep()` anywhere in this system: waiting
    is `poll a checkpoint until timeout`.
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
    """The action space.

    Same list in discovery and in replay, on purpose. If the discovery agent can
    only express things replay can execute, recordings are replayable by
    construction instead of by post-hoc inference over a transcript.
    """

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

    `RETRY` is legal only on a step declared `safe`, and validation refuses the
    combination — `risk` is the artifact's statement about reversibility, so a
    risky step asking to be run twice is a contradiction rather than a preference.
    The engine applies the same gate against policy's *effective* risk, which also
    catches a step promoted to risky from its intent.
    """

    HARD_FAIL = "hard_fail"
    ESCALATE = "escalate"
    RETRY = "retry"


class StepBase(Frozen):
    id: int
    risk: Risk = Risk.SAFE
    # Which of the capability's declared screens this step expects to be looking
    # at. Checked before the step acts, so a flow that has gone somewhere else
    # fails with "we are on the sign-on screen, not the member profile" instead of
    # "the target was not found" — a different sentence and a different fix.
    screen: str | None = None
    # Verified after the action. This is the step's own success condition.
    checkpoint: Checkpoint | None = None
    on_error: OnError = OnError.HARD_FAIL
    # Re-executions allowed when `on_error` is RETRY. Ignored otherwise: a budget
    # without a policy is a field that reads as if it does something.
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

    There is deliberately no `stop_when` field. Exhaustion has exactly two
    signals and both follow from `advance`: a screenful identical to one already
    seen (every mode), and a pagination anchor that is no longer on the page
    (`click_anchor`). A field that selects between them could only ever disable
    one of two correct checks.
    """

    advance: ScanAdvance = ScanAdvance.SCROLL
    anchor: Template | None = None        # for CLICK_ANCHOR, e.g. "Next"
    # Never advance a full region height: a row straddling the boundary would be
    # skipped and reported as a false not-found.
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

    A first-class step type because a target's position in a list is a function of
    the data, not of the layout. Recording `scroll, scroll, click(y)` is wrong four
    separate ways: the position drifts, the page drifts, the record may be absent,
    and the match may be ambiguous. Recording the predicate is right in all four.

    Still fully deterministic — a fixed loop of (observe scope, evaluate predicate,
    advance) with no model in it. Pagination is the same primitive with
    `advance=click_anchor`. Bulk extraction is the same primitive with
    `on_found.action=extract`.
    """

    kind: Literal["find_and_act"] = "find_and_act"

    # Where to look. Located by anchor text (a column-header row), not a fixed box,
    # so a banner appearing above the table does not invalidate the scope.
    scope: Target
    scope_extent: ScopeExtent = ScopeExtent.BELOW

    predicate: Predicate
    scan: Scan = Scan()

    on_found_action: Primitive = Primitive.CLICK
    on_found_offset: tuple[Unit, Unit] = (0.5, 0.5)
    on_found_extract_as: str | None = None
    # Which cell of the matched row to read, named by its column header. A row is
    # not a value: a caller asking for an amount wants a number, and returning
    # "08/10/2026 08/11/2026 PACIFIC WIRELESS Telecom ($441.56)" makes them parse
    # a screen we already parsed. The column is located by its header text and the
    # cell by horizontal overlap with it — the way a person reads a table.
    on_found_extract_column: Template | None = None
    collect_all: bool = False             # "the last N transactions"
    limit: int | None = None

    # Exhausting the list without a match is a legitimate answer, so it maps to a
    # declared business outcome rather than to a failure. Hitting max_advances
    # while content was still changing is NOT the same thing and is a hard failure:
    # we cannot tell absence from quitting early, and conflating those is the
    # mistake the brief names outright.
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
    # Marks a value that must never be logged, screenshotted into evidence
    # unredacted, or included in an LLM prompt. Its value is supplied at execution
    # time and substituted below the serialization boundary.
    sensitive: bool = False


class OutputSpec(Frozen):
    name: str
    type: ValueType
    description: str = ""
    # Which step produced it. Outputs are declared, not "whatever the model
    # happened to read" — the caller's contract must be stable across runs.
    from_step: int
    normalize: tuple[Normalizer, ...] = (Normalizer.COLLAPSE_WS,)
    required: bool = True
    # The same class the inputs use, pointed the other way. A checkpoint proves we
    # are on the right screen; it says nothing about whether the characters read
    # out of it are a number this capability may return. Under vision that is the
    # gap that matters: a misread digit produces a value that is type-valid,
    # passes every assertion, and is wrong — `18204.55` read as `1820455` is a
    # balance a downstream agent will happily quote to a member. A declared range
    # or format turns that from an answer into `OUTPUT_REJECTED`.
    #
    # Authored at review time rather than derived. The recording only ever saw one
    # value, and a bound inferred from one observation would either be the value
    # itself or a guess.
    constraints: Constraints | None = None


class BusinessOutcome(Frozen):
    """A legitimate alternative result, not a failure.

    "No such member" is an answer the caller needs, and conflating it with a crash
    is the most common design mistake in this problem. Declared per capability so
    the calling agent knows the full set of shapes it may receive.

    Detectors are evaluated before the success check at each step; first match wins
    and the run stops cleanly.

    **`detector` may be omitted, and then it is inherited from app policy by
    name.** The two halves of an outcome belong to different owners and it took a
    second capability on the same app to see it: *what the screen says* is a
    property of the application — ten flows that search for a member meet the same
    "no member matches" wording — while *whether this flow can return that answer*
    is a property of the capability, and only the capability can say it. So the
    app owns the detector and the capability owns the declaration.

    Inheriting rather than copying is what makes teaching scale: `learn-outcome`
    and `diagnose` write the detector once into `policies/<app>.yaml`, and every
    capability that opts in gets it — including the ones recorded next year.
    Copying it into each artifact would guarantee they drift, which is the same
    argument that put recoveries in policy in the first place.

    Opting in is still explicit rather than automatic. This list is the contract a
    calling agent branches on, and a capability that advertises an outcome it
    cannot actually reach is lying to its caller about the shapes it may receive.
    """

    name: str
    description: str = ""
    detector: Checkpoint | None = None
    result_fields: dict[str, ValueType] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class Screen(Frozen):
    """A recognisable state of the application.

    The smallest useful piece of a UI model, and the seam a fuller one would grow
    from. Capabilities on the same application visit the same screens, so this is
    where per-tenant differences want to attach: two institutions running the same
    vendor product have the same screens with different branding, and overriding a
    screen's signature is a smaller and safer change than re-recording every
    artifact that passes through it.

    Declared and enforced: a step may name the screen it expects, and replay
    asserts it before acting (`FailureKind.WRONG_SCREEN`, naming where it actually
    is). Not yet *derived*: synthesis emits no screens, so a recorded capability
    declares none and makes no claim about where it is. Deriving one from a single
    run was tried and named the member profile after the member's branch — data,
    not a screen. Separating the two needs two runs with different inputs, which
    is `cua learn-screens`, listed as the next thing to build in REPORT §7.
    """

    name: str
    signature: Checkpoint


class Status(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class AppRef(Frozen):
    """Which application this capability drives.

    `base_url_pattern` is a pattern rather than a literal so the same artifact can
    point at a different institution's install of the same vendor product. That is
    the cheapest possible down-payment on the multi-tenant story (§3.7) and costs
    nothing now.
    """

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
    id: str                               # cap_get_savings_balance
    version: int = 1
    status: Status = Status.DRAFT

    goal: str                             # the natural-language goal it satisfies
    description: str = ""

    app: AppRef
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    # The states this flow passes through. Empty is legitimate: a capability that
    # declares no screens simply makes no claim about where it is.
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

        Every one of these is otherwise a capability that loads fine, passes
        review, and fails halfway through a run against a member's account —
        which is the expensive place to find out. Synthesis on a familiar surface
        happens to produce consistent artifacts; recordings from an unfamiliar one
        will be rougher, and the cheapest place to catch that is construction.

        Deliberately *not* checked here: whether an anchor exists on the screen,
        or whether a checkpoint can pass. Those are claims about the application,
        answerable only by running it. This validator only checks claims the
        artifact makes about itself.
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

        # An output must name a step that exists and that actually extracts —
        # a `from_step` pointing at a click yields nothing and the caller's
        # contract silently loses a field.
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

            # `risk: risky` is the artifact's declaration that the step is not
            # reversible, and `on_error: retry` is a request to run it twice. A
            # file that says both is asking for a duplicate transfer, and the
            # cheapest place to refuse it is before it is ever loaded — the engine
            # also refuses at run time, but by then the artifact has been reviewed
            # and approved with a contradiction in it.
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

        # Every {{placeholder}} anywhere in the artifact must name a declared
        # input. An unknown one raises at render time, mid-run; here it is a
        # rejected file.
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
