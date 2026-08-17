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

from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    Bbox,
    Frozen,
    MatchMode,
    Normalizer,
    Risk,
    Template,
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
    offset: tuple[float, float] = (0.5, 0.5)

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


class Predicate(Frozen):
    """A data-dependent match, evaluated deterministically over detected elements."""

    match: Literal["row_contains_all", "row_contains_any", "cell_equals"] = "row_contains_all"
    terms: tuple[Template, ...]
    normalize: tuple[Normalizer, ...] = (
        Normalizer.CASEFOLD,
        Normalizer.COLLAPSE_WS,
        Normalizer.STRIP_CURRENCY,
    )


class Scan(Frozen):
    advance: ScanAdvance = ScanAdvance.SCROLL
    anchor: Template | None = None        # for CLICK_ANCHOR, e.g. "Next"
    # Never advance a full region height: a row straddling the boundary would be
    # skipped and reported as a false not-found.
    overlap: float = 0.15
    max_advances: int = 10
    stop_when: Literal["region_hash_unchanged", "anchor_absent"] = "region_hash_unchanged"


class MultiplePolicy(str, Enum):
    FIRST = "first"                       # tolerable on a read
    ESCALATE = "escalate"                 # required on a write: the wrong record is unrecoverable
    FAIL = "fail"


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
    scope_extent: Literal["below", "above", "within"] = "below"

    predicate: Predicate
    scan: Scan = Scan()

    on_found_action: Primitive = Primitive.CLICK
    on_found_offset: tuple[float, float] = (0.5, 0.5)
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


class BusinessOutcome(Frozen):
    """A legitimate alternative result, not a failure.

    "No such member" is an answer the caller needs, and conflating it with a crash
    is the most common design mistake in this problem. Declared per capability so
    the calling agent knows the full set of shapes it may receive.

    Detectors are evaluated before the success check at each step; first match wins
    and the run stops cleanly.
    """

    name: str
    description: str = ""
    detector: Checkpoint
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

    Derived from a recording rather than authored beside it — see
    `discovery.synthesize.screens_from` — so it cannot drift from what replay
    actually sees.
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
