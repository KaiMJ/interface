"""The replay engine and its error taxonomy.

Every case here is one of the result classes the caller must be able to tell
apart, driven through the real engine, the real resolver, the real checkpoint
evaluation and the real app policy file. Only the surface is faked: perception
returns scripted frames and the driver records what it was asked to do.

Faking at that seam is the point of having it. If these tests needed a browser
they would not run in CI, and the thing they are actually asserting — that a
"no such member" screen produces a business outcome and an unreadable balance
produces a failure — has nothing to do with pixels.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from cua.escalation import RunControl
from cua.evidence import EvidenceWriter
from cua.policy import Policy, Recovery, Redactor
from cua.replay.engine import ReplayEngine
from cua.resolve import Resolver
from cua.schema import (
    ActStep,
    AppRef,
    Bbox,
    BusinessOutcome,
    Capability,
    CheckKind,
    Checkpoint,
    Constraints,
    Element,
    ElementSource,
    FailureKind,
    InputSpec,
    InterventionResolution,
    Normalizer,
    Observation,
    OnError,
    OutputSpec,
    Point,
    Primitive,
    Relation,
    Risk,
    RunStatus,
    Target,
    ValueType,
    Viewport,
)

POLICY = Policy.load(Path(__file__).resolve().parents[2] / "policies" / "targetapp.yaml")
VIEWPORT = Viewport(width=1440, height=900)
MEMBER_URL = "http://targetapp:8080/members/{{member_id}}"
MONEY = (Normalizer.COLLAPSE_WS, Normalizer.STRIP_CURRENCY)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def el(id_: str, x: float, y: float, w: float, h: float, text: str) -> Element:
    return Element(
        id=id_,
        role="text",
        name=text,
        text=text,
        bbox=Bbox(x=x, y=y, w=w, h=h),
        source=ElementSource.OCR,
        conf=0.95,
    )


def frame(*texts: str, url: str = "http://targetapp:8080/members/12345") -> Observation:
    """A screen, described by the lines on it, laid out top to bottom."""
    return Observation(
        screenshot_path="/nonexistent/frame.png",
        viewport=VIEWPORT,
        elements=tuple(
            el(f"e{i}", 0.1, 0.1 + i * 0.05, 0.3, 0.02, t) for i, t in enumerate(texts)
        ),
        url=url,
        frame_hash="hash",
        taken_at="2026-08-16T00:00:00+00:00",
    )


# The screen the application serves once the session has gone. Declared as a
# recovery in the shipped policy: sign in, start the capability over.
EXPIRED = frame("Staff Sign-On", "Your session has expired", "User ID", "Password")

ACCOUNTS_ROW = Observation(
    screenshot_path="/nonexistent/frame.png",
    viewport=VIEWPORT,
    elements=(
        el("e0", 0.05, 0.10, 0.20, 0.02, "Member Profile"),
        el("e1", 0.05, 0.20, 0.10, 0.02, "29455"),
        el("e2", 0.20, 0.20, 0.12, 0.02, "Primary Savings"),
        el("e3", 0.35, 0.20, 0.06, 0.02, "Active"),
        el("e4", 0.45, 0.20, 0.09, 0.02, "$18,204.55"),
    ),
    url="http://targetapp:8080/members/12345",
    frame_hash="hash",
    taken_at="2026-08-16T00:00:00+00:00",
)


class FakePerceiver:
    """Returns the current scripted frame. Only the driver advances the script."""

    def __init__(self, frames: list[Observation]) -> None:
        self.frames = frames
        self.index = 0
        self.observations = 0

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        self.observations += 1
        return self._current()

    def _current(self) -> Observation:
        return self.frames[min(self.index, len(self.frames) - 1)]

    def peek(self, out_path: Path) -> str:
        """The cheap "has anything changed" probe. Costs no observation, here or
        in production — which is the whole reason the poll loops use it."""
        return self._current().frame_hash or ""


class FakeDriver:
    """Records what it was told to do and advances the scripted screen."""

    def __init__(self, perceiver: FakePerceiver) -> None:
        self.perceiver = perceiver
        self.calls: list[tuple[str, Any]] = []

    def _advance(self) -> None:
        self.perceiver.index += 1

    async def navigate(self, url: str) -> None:
        self.calls.append(("navigate", url))
        self._advance()

    async def reload(self) -> None:
        self.calls.append(("reload", None))
        self._advance()

    async def click(self, p: Point, button: str = "left") -> None:
        self.calls.append(("click", (round(p.x, 3), round(p.y, 3))))
        self._advance()

    async def type_text(self, text: str, secret: bool = False) -> None:
        self.calls.append(("type", "***" if secret else text))

    async def key(self, keys: str) -> None:
        self.calls.append(("key", keys))
        self._advance()

    async def scroll(self, p: Point, dy: float) -> None:
        self.calls.append(("scroll", dy))
        self._advance()

    def current_url(self) -> str | None:
        return self.perceiver.frames[min(self.perceiver.index, len(self.perceiver.frames) - 1)].url


def build(tmp_path: Path, frames: list[Observation]) -> tuple[ReplayEngine, FakeDriver, RunControl]:
    perceiver = FakePerceiver(frames)
    driver = FakeDriver(perceiver)
    control = RunControl(run_id="run-test")
    engine = ReplayEngine(
        perceiver=perceiver,
        driver=driver,
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "run-test", Redactor()),
        control=control,
        settle_timeout_ms=100,
        settle_poll_ms=1,
        step_timeout_ms=100,
    )
    return engine, driver, control


# ---------------------------------------------------------------------------
# the capability under test
# ---------------------------------------------------------------------------


def savings_capability(**overrides: Any) -> Capability:
    """Read a member's savings balance. The read capability, hand-written.

    Deliberately the same shape synthesis emits: a navigate with a templated URL,
    an extraction targeted relative to a label, a declared output with a type, and
    the two legitimate non-answers this screen can produce.
    """
    cap = Capability(
        id="cap_get_savings_balance",
        goal="look up a member and read their savings balance",
        app=AppRef(name="targetapp", base_url_pattern="^http://targetapp:8080(/.*)?$"),
        inputs=[
            InputSpec(name="member_id", type=ValueType.STRING, example="12345"),
            InputSpec(name="account_nickname", type=ValueType.STRING, example="Primary Savings"),
        ],
        outputs=[
            OutputSpec(
                name="balance",
                type=ValueType.NUMBER,
                from_step=2,
                normalize=MONEY,
                description="current balance of the named account",
            )
        ],
        steps=[
            ActStep(
                id=1,
                action=Primitive.NAVIGATE,
                value=MEMBER_URL,
                checkpoint=Checkpoint(
                    kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50
                ),
            ),
            ActStep(
                id=2,
                action=Primitive.EXTRACT,
                extract_as="balance",
                target=Target(
                    intent="read the balance beside the account",
                    target_desc="the current balance cell in the account's row",
                    anchor_text="{{account_nickname}}",
                    relation=Relation.RIGHT_OF,
                    relation_index=1,
                ),
            ),
        ],
        success=Checkpoint(
            kind=CheckKind.TEXT_PRESENT, value="{{account_nickname}}", timeout_ms=50
        ),
        business_outcomes=[
            BusinessOutcome(
                name="member_not_found",
                description="no member exists with that id",
                detector=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="No member record found"),
                result_fields={"member_id": ValueType.STRING},
            ),
            BusinessOutcome(
                name="permission_denied",
                description="the member exists but the operator may not view it",
                detector=Checkpoint(
                    kind=CheckKind.TEXT_PRESENT, value="do not have permission to view"
                ),
                result_fields={"member_id": ValueType.STRING},
            ),
        ],
    )
    return cap.model_copy(update=overrides)


INPUTS = {"member_id": "12345", "account_nickname": "Primary Savings"}


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------


async def test_success_returns_the_declared_typed_output(tmp_path: Path) -> None:
    engine, driver, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    # A number, not the string that was on the screen: the caller's contract says
    # what type it gets, and "$18,204.55" is not one.
    assert result.outputs == {"balance": 18204.55}
    assert driver.calls[0] == ("navigate", "http://targetapp:8080/members/12345")
    assert [s.step_id for s in result.steps] == [1, 2]


async def test_replay_is_deterministic_by_construction(tmp_path: Path) -> None:
    # Not "the engine does not call a model" — the engine *cannot*. Its resolver
    # was built without the tier that could.
    engine, _, _ = build(tmp_path, [ACCOUNTS_ROW])
    assert engine.resolver.allow_vlm is False


async def test_evidence_is_written_as_the_run_proceeds(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])
    await engine.replay(savings_capability(), INPUTS)

    run = tmp_path / "run-test"
    assert (run / "run.json").exists()
    assert (run / "steps.jsonl").read_text().count("\n") == 2
    assert list((run / "observations").glob("*.json"))


# ---------------------------------------------------------------------------
# business outcomes — answers, not failures
# ---------------------------------------------------------------------------


async def test_member_not_found_is_an_outcome_not_a_failure(tmp_path: Path) -> None:
    engine, _, _ = build(
        tmp_path,
        [frame("start"), frame("Member Inquiry", "No member record found for ID 99999.")],
    )
    result = await engine.replay(savings_capability(), {**INPUTS, "member_id": "99999"})

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome is not None
    assert result.outcome.name == "member_not_found"
    # The caller gets back which member was not found, from what they asked for.
    assert result.outcome.fields == {"member_id": "99999"}
    assert result.failure is None


async def test_permission_denied_is_a_distinct_outcome_from_not_found(tmp_path: Path) -> None:
    engine, _, _ = build(
        tmp_path,
        [
            frame("start"),
            frame("Member Inquiry", "You do not have permission to view this member record."),
        ],
    )
    result = await engine.replay(savings_capability(), {**INPUTS, "member_id": "77777"})

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome is not None
    assert result.outcome.name == "permission_denied"


# ---------------------------------------------------------------------------
# hard failures
# ---------------------------------------------------------------------------


async def test_an_application_error_is_named_as_one(tmp_path: Path) -> None:
    # Declared in app policy, so it stops with APP_ERROR rather than as a
    # checkpoint that did not hold — which would send an operator looking for a
    # layout problem that is not there.
    engine, _, _ = build(
        tmp_path,
        [
            frame("start"),
            frame("Application Error", "An unexpected error occurred while processing"),
        ],
    )
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.APP_ERROR


async def test_a_repeatable_step_gets_its_declared_budget_against_an_app_error(
    tmp_path: Path,
) -> None:
    """An application error is not automatically final.

    The taxonomy keeps two statements apart and this is where they meet. A
    *recovery* is the app's operator saying "this condition is transient on this
    application"; `on_error: retry` is the recording saying "this step is
    repeatable". Either can buy another attempt, neither implies the other, and
    both are gated on the same `risk`.

    What does not change: when the budget is spent the run stops with APP_ERROR,
    the kind that names the cause. Retrying must not cost us the diagnosis.
    """
    errored = frame("Search", "An unexpected error occurred while processing")
    engine, _, _ = build(tmp_path, [frame("Search"), errored, errored, errored, errored])
    cap = open_profile(on_error=OnError.RETRY, retries=2)

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.APP_ERROR
    step = result.steps[0]
    assert step.attempts == 3
    assert step.note and "on_error: retry" in step.note


async def test_a_risky_step_is_not_re_run_against_an_app_error(tmp_path: Path) -> None:
    """The same gate, on the same path. A submit whose result we could not read is
    not re-submitted to find out, whatever budget the artifact declared."""
    errored = frame("Search", "An unexpected error occurred while processing")
    engine, _, _ = build(tmp_path, [frame("Search"), errored, errored, errored])
    engine.policy = POLICY.__class__(
        **{**_policy_kwargs(POLICY), "risky_disposition": "allow"}
    )
    cap = open_profile(risk=Risk.RISKY, on_error=OnError.RETRY, retries=2)

    result = await engine.replay(cap, INPUTS)

    assert result.failure is not None
    assert result.failure.kind is FailureKind.APP_ERROR
    assert result.steps[0].attempts == 1


async def test_a_checkpoint_failure_reports_expected_beside_observed(tmp_path: Path) -> None:
    engine, _, _ = build(tmp_path, [frame("start"), frame("Some other screen entirely")])
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.CHECKPOINT_FAILED
    assert result.failure.step_id == 1
    assert "Member Profile" in (result.failure.expected or "")
    assert "Some other screen" in (result.failure.observed or "")


async def test_a_target_that_is_not_there_stops_rather_than_guessing(tmp_path: Path) -> None:
    engine, _, _ = build(
        tmp_path, [frame("start"), frame("Member Profile", "Everyday Checking", "$4,820.19")]
    )
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.RESOLUTION_EXHAUSTED
    assert result.failure.step_id == 2


async def test_a_target_mismatch_reports_where_on_screen_it_looked(tmp_path: Path) -> None:
    """The failure says WHERE, not only what.

    "the region does not read as the recorded target" sends an operator to the
    screenshot to work out which region. Carrying the box means the console can
    draw it — and means the same failure is machine-readable, which is what a
    policy author needs to write a dismissal handler against.
    """
    cap = savings_capability()
    # An anchor that is not on the screen, plus a recorded box. The resolver
    # falls all the way through to the box, and the pre-click assertion then
    # finds the region does not say what the recording said it said.
    cap.steps[1] = cap.steps[1].model_copy(
        update={
            "target": Target(
                intent="read the balance",
                target_desc="the current balance cell",
                anchor_text="Available Balance",
                bbox=Bbox(x=0.44, y=0.31, w=0.09, h=0.02),
            )
        }
    )
    engine, driver, _ = build(
        tmp_path, [frame("start"), frame("Member Profile", "Account Closed")]
    )

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.TARGET_MISMATCH
    assert result.failure.region == Bbox(x=0.44, y=0.31, w=0.09, h=0.02)
    # Nothing was clicked at coordinates whose contents we could not confirm.
    assert not any(kind == "click" for kind, _ in driver.calls)
    # And it survives the wire format the console reads.
    assert result.model_dump(mode="json")["failure"]["region"]["x"] == 0.44


async def test_an_unreadable_output_fails_rather_than_returning_nothing(tmp_path: Path) -> None:
    # The label is there and the cell beside it is not a number. A partial
    # success here is how a downstream agent ends up acting on a null balance.
    unreadable = ACCOUNTS_ROW.model_copy(
        update={
            "elements": tuple(
                e if e.text != "$18,204.55" else el("e4", 0.45, 0.20, 0.09, 0.02, "pending")
                for e in ACCOUNTS_ROW.elements
            )
        }
    )
    engine, _, _ = build(tmp_path, [frame("start"), unreadable])
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.EXTRACTION_FAILED
    assert "pending" in (result.failure.observed or "")


async def test_navigating_outside_the_allowlist_is_denied(tmp_path: Path) -> None:
    cap = savings_capability()
    cap.steps[0] = cap.steps[0].model_copy(update={"value": "http://evil.example/members/1"})
    engine, driver, _ = build(tmp_path, [frame("start"), ACCOUNTS_ROW])

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.POLICY_DENIED
    # Denied means not attempted, not attempted-and-reported.
    assert driver.calls == []


async def test_a_url_containing_a_risky_verb_is_not_a_risky_action(tmp_path: Path) -> None:
    # The policy promotes a step to risky when its *declared intent* reads as a
    # mutation. Matching that against the step's value instead would stop the run
    # before every page whose path contains a verb — measured on
    # /transfer/review, which is a page you look at, not a transfer you make.
    cap = savings_capability()
    cap.steps[0] = cap.steps[0].model_copy(
        update={"value": "http://targetapp:8080/transfer/review?member={{member_id}}"}
    )
    engine, driver, control = build(tmp_path, [frame("start"), ACCOUNTS_ROW])
    result = await engine.replay(cap, INPUTS)

    assert control.intervention is None
    assert result.status is RunStatus.SUCCESS
    assert driver.calls[0][0] == "navigate"


async def test_a_bad_input_is_rejected_before_anything_is_touched(tmp_path: Path) -> None:
    engine, driver, _ = build(tmp_path, [ACCOUNTS_ROW])
    result = await engine.replay(savings_capability(), {"account_nickname": "Primary Savings"})

    assert result.status is RunStatus.FAILURE
    assert driver.calls == []


async def test_a_violated_constraint_is_a_structured_rejection(tmp_path: Path) -> None:
    """Not an exception out of `replay()`.

    A declared constraint is the caller's contract, so breaking it has to come
    back as a result they can read — naming the input and the rule — the same way
    a missing input or a bad type does. Every other class of bad call already did;
    constraints escaped as a bare ValueError past both handlers.
    """
    cap = savings_capability()
    cap = cap.model_copy(
        update={
            "inputs": [
                InputSpec(
                    name="member_id",
                    type=ValueType.STRING,
                    example="12345",
                    constraints=Constraints(pattern=r"^[0-9]{5}$"),
                ),
                cap.inputs[1],
            ]
        }
    )
    engine, driver, _ = build(tmp_path, [ACCOUNTS_ROW])

    result = await engine.replay(cap, {**INPUTS, "member_id": "not-an-id"})

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert "member_id" in result.failure.message
    assert "^[0-9]{5}$" in result.failure.message
    # Rejected before the surface was touched, which is the point of checking at all.
    assert driver.calls == []


async def test_a_sensitive_input_is_never_written_anywhere(tmp_path: Path) -> None:
    """The one redaction guarantee that is implemented, asserted end to end.

    `InputSpec.sensitive` is a declaration rather than a pattern guess, so it
    cannot miss — but only if the redaction actually runs before the first write.
    It writes on every step, so "before the last write" would already be too late.
    """
    cap = savings_capability()
    cap = cap.model_copy(
        update={
            "inputs": [
                *cap.inputs,
                InputSpec(
                    name="operator_pin",
                    type=ValueType.STRING,
                    required=False,
                    sensitive=True,
                ),
            ]
        }
    )
    engine, _, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])

    secret = "8213-super-secret"
    result = await engine.replay(cap, {**INPUTS, "operator_pin": secret})

    assert result.status is RunStatus.SUCCESS
    assert result.inputs["operator_pin"] == Redactor.MASK
    # Non-sensitive inputs are untouched: redacting everything would make a result
    # undebuggable and is not what was declared.
    assert result.inputs["member_id"] == "12345"

    # And nothing under the evidence directory carries it either — the result is
    # written there repeatedly as the run proceeds.
    written = [p.read_text() for p in tmp_path.rglob("*") if p.is_file()]
    assert not any(secret in text for text in written)


# ---------------------------------------------------------------------------
# recoverable conditions
# ---------------------------------------------------------------------------


async def test_a_declared_interstitial_is_dismissed_and_the_run_continues(
    tmp_path: Path,
) -> None:
    engine, driver, _ = build(
        tmp_path,
        [
            frame("start"),
            # The maintenance notice the app policy declares, with its dismiss
            # control. Clicking it advances the script.
            frame("scheduled maintenance", "Dismiss"),
            ACCOUNTS_ROW,
        ],
    )
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    # The capability itself never clicks anything — a navigate and an extract.
    # So any click at all is the policy's dismiss handler, applied without the
    # artifact knowing the interstitial exists.
    assert [kind for kind, _ in driver.calls] == ["navigate", "click"]


# --- an interstitial that eats the click ------------------------------------
#
# The case the dismiss-and-re-poll strategy above cannot handle on its own, and
# the one the demo app was built to produce: the maintenance dialog does not move
# the page, so a recorded coordinate still resolves to the right control and the
# click lands on the dialog instead. Dismissing it afterwards does not make the
# eaten click happen. Either it is cleared before the step acts, or the step is
# executed again.

SEARCH = frame("Member Search", "Search", url="http://targetapp:8080/members")
MODAL = Observation(
    screenshot_path="/nonexistent/frame.png",
    viewport=VIEWPORT,
    elements=(
        # The page beneath is still readable — the overlay is translucent, which
        # is why perception sees both and why the recorded target still resolves.
        el("e0", 0.05, 0.10, 0.20, 0.02, "Member Search"),
        el("e1", 0.05, 0.15, 0.10, 0.02, "Search"),
        el("e2", 0.30, 0.40, 0.30, 0.02, "A scheduled maintenance window"),
        el("e3", 0.55, 0.50, 0.08, 0.02, "Dismiss"),
    ),
    url="http://targetapp:8080/members",
    frame_hash="hash",
    taken_at="2026-08-16T00:00:00+00:00",
)
PROFILE = frame("Member Profile", "Primary Savings", url="http://targetapp:8080/members/12345")


class ModalApp:
    """A perceiver and driver in one, modelling the demo app's dialog.

    Two states. While the modal is up every click is absorbed except the one on
    its dismiss control; once it is down, clicking Search opens the profile. The
    fixture exists because the interesting property cannot be scripted as a frame
    list: whether a click has any effect depends on what is on screen when it is
    issued, which is the whole of this failure mode.
    """

    def __init__(self, modal_at: str = "start") -> None:
        # "start": up before the step observes. "on_click": appears in the gap
        # between observing and clicking, which no amount of pre-checking closes.
        self.modal_at = modal_at
        self.modal = modal_at == "start"
        self.opened = False
        self.calls: list[tuple[str, Any]] = []

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        return self._current()

    def _current(self) -> Observation:
        if self.modal:
            return MODAL
        return PROFILE if self.opened else SEARCH

    def peek(self, out_path: Path) -> str:
        return self._current().frame_hash or ""

    async def click(self, p: Point, button: str = "left") -> None:
        self.calls.append(("click", (round(p.x, 3), round(p.y, 3))))
        if self.modal_at == "on_click" and not self.opened:
            self.modal = True
            self.modal_at = "spent"        # it appears once, as a real one would
            return
        if self.modal:
            if 0.5 < p.x < 0.7 and 0.45 < p.y < 0.55:
                self.modal = False         # the dismiss control
            return                         # everything else is eaten
        self.opened = True

    async def navigate(self, url: str) -> None:
        self.calls.append(("navigate", url))

    async def type_text(self, text: str, secret: bool = False) -> None:
        self.calls.append(("type", text))

    async def key(self, keys: str) -> None:
        self.calls.append(("key", keys))

    async def scroll(self, p: Point, dy: float) -> None:
        self.calls.append(("scroll", dy))

    def current_url(self) -> str | None:
        return self.settle(Path("/x"), 0, 0).url


def open_profile(**overrides: Any) -> Capability:
    """One click on Search, expecting the profile. The smallest flow in which a
    swallowed click is distinguishable from a click that landed."""
    cap = savings_capability()
    return cap.model_copy(
        update={
            "id": "cap_open_profile",
            "steps": [
                ActStep(
                    id=1,
                    action=Primitive.CLICK,
                    target=Target(
                        intent="open the member's profile",
                        target_desc="Search",
                        anchor_text="Search",
                    ),
                    checkpoint=Checkpoint(
                        kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50
                    ),
                    **overrides,
                )
            ],
            "outputs": [],
            "success": Checkpoint(
                kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50
            ),
            "business_outcomes": [],
        }
    )


def build_modal(tmp_path: Path, app: ModalApp) -> ReplayEngine:
    return ReplayEngine(
        perceiver=app,
        driver=app,
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "run-test", Redactor()),
        control=RunControl(run_id="run-test"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
        step_timeout_ms=100,
    )


async def test_an_interstitial_already_on_screen_is_cleared_before_the_step_acts(
    tmp_path: Path,
) -> None:
    """The cheap case: the dialog is up when the step observes, so it never gets
    the chance to eat anything."""
    app = ModalApp(modal_at="start")
    result = await build_modal(tmp_path, app).replay(open_profile(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    # Dismiss first, then the step's own click. One attempt: nothing was lost.
    assert len(app.calls) == 2
    assert result.steps[0].attempts == 1
    assert result.steps[0].recovery_applied == "maintenance_notice"


async def test_a_swallowed_click_is_re_executed_after_the_interstitial_clears(
    tmp_path: Path,
) -> None:
    """The expensive case, and the one that used to hard-fail.

    The dialog appears between observing and clicking, so the click is eaten. The
    old engine dismissed it and then polled a checkpoint for an action that never
    happened, reporting `checkpoint_failed` at the end of the step's timeout —
    with the evidence showing a recovery that had "worked".
    """
    app = ModalApp(modal_at="on_click")
    result = await build_modal(tmp_path, app).replay(open_profile(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    step = result.steps[0]
    assert step.attempts == 2
    assert step.status.value == "recovered"
    assert step.note and "re-running" in step.note


async def test_a_risky_step_is_never_re_executed_even_when_a_recovery_fires(
    tmp_path: Path,
) -> None:
    """The gate. `risk` is the artifact's statement about reversibility, so a step
    that may have submitted something is not run again to find out — the run stops
    and says the checkpoint did not hold, which is a question for a human."""
    app = ModalApp(modal_at="on_click")
    cap = open_profile(risk=Risk.RISKY, on_error=OnError.HARD_FAIL)
    engine = build_modal(tmp_path, app)
    # `confirm` would park the run; this test is about retry, not about escalation.
    engine.policy = POLICY.__class__(
        **{**_policy_kwargs(POLICY), "risky_disposition": "allow"}
    )

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.CHECKPOINT_FAILED
    # Exactly one click on the target. The recovery's dismiss is the only other.
    assert len(app.calls) == 2
    assert result.steps[0].attempts == 1


def _policy_kwargs(policy: Any) -> dict[str, Any]:
    return {
        "app": policy.app,
        "vendor": policy.vendor,
        "base_url_pattern": policy.base_url_pattern,
        "entry_url": policy.entry_url,
        "allowed_url_patterns": policy.allowed_url_patterns,
        "allowed_actions": policy.allowed_actions,
        "risky_intent_patterns": policy.risky_intent_patterns,
        "recoveries": policy.recoveries,
        "app_errors": policy.app_errors,
        "escalations": policy.escalations,
        "sign_on": policy.sign_on,
        "surface": policy.surface,
        "redact_patterns": policy.redact_patterns,
    }


# --- waiting for the screen to arrive ---------------------------------------


class SlowApp(ModalApp):
    """The previous page is still on screen for the first N observations.

    What a server-side delay actually looks like to a coordinate-based system:
    not a spinner, not an empty page — the *old* page, fully rendered and
    perfectly stable, for as long as the request takes. Nothing about settling
    can tell that apart from having arrived, which is why the answer has to be to
    keep looking for the thing the step needs.
    """

    def __init__(self, stale_for: int) -> None:
        super().__init__(modal_at="never")
        self.modal = False
        self.stale_for = stale_for
        self.observations = 0

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        self.observations += 1
        if self.observations <= self.stale_for:
            return SEARCH                     # the page we came from
        return PROFILE if self.opened else SEARCH


async def test_a_target_that_has_not_rendered_yet_is_waited_for_not_failed(
    tmp_path: Path,
) -> None:
    """The gap the `slow` fault found on the live app.

    A step recorded without a checkpoint imposes no wait, so the previous step's
    latency lands on this one as `target_mismatch` — which reads as UI drift and
    is not. Resolution is a read, so polling it needs no risk gate; this is the
    safe direction to retry in.
    """
    app = SlowApp(stale_for=3)
    cap = open_profile()
    # The step's target is on the profile, not on the page we are still looking at.
    cap = cap.model_copy(
        update={
            "steps": [
                cap.steps[0].model_copy(
                    update={
                        "action": Primitive.EXTRACT,
                        "extract_as": None,
                        "target": Target(
                            intent="read the account name",
                            target_desc="Primary Savings",
                            anchor_text="Primary Savings",
                        ),
                        "checkpoint": Checkpoint(
                            kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=5000
                        ),
                    }
                )
            ]
        }
    )
    app.opened = True                          # the app has responded; the frames lag

    result = await build_modal(tmp_path, app).replay(cap, INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert app.observations > 3                # it kept looking rather than stopping
    assert result.steps[0].note and "waited" in result.steps[0].note


async def test_the_wait_still_ends_and_reports_what_it_could_not_find(
    tmp_path: Path,
) -> None:
    """Polling forever is the other way to get this wrong."""
    app = SlowApp(stale_for=10_000)
    cap = open_profile()
    cap = cap.model_copy(
        update={
            "steps": [
                cap.steps[0].model_copy(
                    update={
                        "target": Target(
                            intent="click something that is not there",
                            target_desc="Approve",
                            anchor_text="Approve",
                        ),
                        "checkpoint": Checkpoint(
                            kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50
                        ),
                    }
                )
            ]
        }
    )
    result = await build_modal(tmp_path, app).replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.RESOLUTION_EXHAUSTED
    assert result.failure.expected == "Approve"


# --- declared retry ---------------------------------------------------------


class FlakyApp(ModalApp):
    """Opens the profile only on the Nth click. A transient the app policy has no
    detector for, which is what `on_error: retry` is for."""

    def __init__(self, opens_on: int) -> None:
        super().__init__(modal_at="never")
        self.modal = False
        self.opens_on = opens_on
        self.clicks = 0

    async def click(self, p: Point, button: str = "left") -> None:
        self.calls.append(("click", (round(p.x, 3), round(p.y, 3))))
        self.clicks += 1
        if self.clicks >= self.opens_on:
            self.opened = True


async def test_on_error_retry_re_runs_a_safe_step_within_its_declared_budget(
    tmp_path: Path,
) -> None:
    app = FlakyApp(opens_on=3)
    cap = open_profile(on_error=OnError.RETRY, retries=2)
    result = await build_modal(tmp_path, app).replay(cap, INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert result.steps[0].attempts == 3


async def test_on_error_retry_stops_at_the_budget_rather_than_looping(
    tmp_path: Path,
) -> None:
    app = FlakyApp(opens_on=9)
    cap = open_profile(on_error=OnError.RETRY, retries=2)
    result = await build_modal(tmp_path, app).replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert app.clicks == 3          # 1 + the 2 declared retries, and no more
    assert result.steps[0].attempts == 3


# --- the deployment, not the recording, decides which install ---------------


async def test_a_recorded_url_is_rebased_onto_this_deployments_install(
    tmp_path: Path,
) -> None:
    """The artifact contributes the path; the deployment contributes the origin.

    Without this the allowlist is no defence at all, because it is a *pattern*
    spanning tenants by design — so a capability recorded at one institution would
    replay happily against that institution from another institution's deployment,
    and report success.
    """
    engine, driver, _ = build(tmp_path, [frame("start"), ACCOUNTS_ROW])
    engine.entry_url = "http://targetapp:8080"
    cap = savings_capability()
    recorded = cap.steps[0].model_copy(
        update={"value": "http://targetapp-riverside:8080/members/{{member_id}}"}
    )
    cap = cap.model_copy(
        update={
            "app": cap.app.model_copy(
                update={"base_url_pattern": "^http://targetapp[a-z-]*:8080(/.*)?$"}
            ),
            "steps": [recorded, cap.steps[1]],
        }
    )

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert driver.calls[0] == ("navigate", "http://targetapp:8080/members/12345")
    assert result.steps[0].note and "rebased" in result.steps[0].note


async def test_a_recovery_that_never_works_is_a_failure_not_an_infinite_loop(
    tmp_path: Path,
) -> None:
    # The interstitial never clears. Dismissing it three times means the
    # dismissal is not working, and the policy's cap says so.
    stuck = frame("scheduled maintenance", "Dismiss")
    engine, _, _ = build(tmp_path, [frame("start"), stuck, stuck, stuck, stuck, stuck])
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.FAILURE


# ---------------------------------------------------------------------------
# escalation
# ---------------------------------------------------------------------------


def risky_capability() -> Capability:
    cap = savings_capability()
    return cap.model_copy(
        update={
            "id": "cap_submit_transfer",
            "steps": [
                ActStep(
                    id=1,
                    action=Primitive.CLICK,
                    risk=Risk.RISKY,
                    target=Target(
                        intent="submit the transfer",
                        target_desc="the confirm button",
                        anchor_text="Confirm Transfer",
                    ),
                    checkpoint=Checkpoint(
                        kind=CheckKind.TEXT_PRESENT, value="Transfer complete", timeout_ms=50
                    ),
                    on_error=OnError.ESCALATE,
                )
            ],
            "outputs": [],
            "success": Checkpoint(
                kind=CheckKind.TEXT_PRESENT, value="Transfer complete", timeout_ms=50
            ),
        }
    )


async def _run_until_intervention(
    engine: ReplayEngine, control: RunControl, cap: Capability
) -> asyncio.Task[Any]:
    task = asyncio.create_task(engine.replay(cap, INPUTS))
    for _ in range(500):
        if control.intervention is not None:
            return task
        await asyncio.sleep(0.005)
    task.cancel()
    raise AssertionError("no intervention was raised")


async def test_a_risky_action_waits_for_a_human_and_then_proceeds(tmp_path: Path) -> None:
    engine, driver, control = build(
        tmp_path,
        [frame("Confirm Transfer"), frame("Transfer complete")],
    )
    task = await _run_until_intervention(engine, control, risky_capability())

    assert control.intervention is not None
    assert control.intervention.reason.value == "risky_action_confirmation"
    # Nothing was clicked while the request was open.
    assert driver.calls == []

    control.take_control("operator-1")
    control.release(
        InterventionResolution(id=control.intervention.id, outcome="resume", operator="operator-1")
    )
    result = await task

    assert result.status is RunStatus.SUCCESS
    assert any(kind == "click" for kind, _ in driver.calls)


async def test_an_aborted_intervention_ends_the_run_as_escalated(tmp_path: Path) -> None:
    engine, driver, control = build(
        tmp_path, [frame("Confirm Transfer"), frame("Transfer complete")]
    )
    task = await _run_until_intervention(engine, control, risky_capability())

    control.take_control("operator-1")
    control.release(
        InterventionResolution(
            id=control.intervention.id if control.intervention else "",
            outcome="abort",
            operator="operator-1",
            note="wrong account",
        )
    )
    result = await task

    assert result.status is RunStatus.ESCALATED
    assert result.intervention_id
    assert driver.calls == []


# ---------------------------------------------------------------------------
# what a step costs
# ---------------------------------------------------------------------------


async def test_a_step_does_not_photograph_the_same_screen_twice(tmp_path: Path) -> None:
    """The frame a step ends on is the frame the next step starts from.

    Nothing acts between one step's effect verification and the next step's first
    look, so taking a second picture there costs a full perception — ~2.4s of text
    recognition on a dense page, measured — to establish that the screen nobody
    touched has not changed. Every step after the first should therefore pay for
    one observation, not two.
    """
    engine, _, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    later = [s.phases.observations for s in result.steps[1:]]
    assert later and all(n == 1 for n in later), later
    # And the accounting is real: the time is attributed, not invented.
    assert all(s.phases.observe_ms >= 0 for s in result.steps)


async def test_a_step_records_the_screen_it_acted_on_and_the_one_it_produced(
    tmp_path: Path,
) -> None:
    """Two frames per step, because they mean different things.

    The target was resolved against the screen the step acted on; the checkpoint
    was judged on what the action produced. Storing only the second — which is
    what happened when the post-action observation overwrote the pre-action one —
    draws the step's resolved region onto a screen that never contained it, and
    makes consecutive steps byte-identical, since one step's after-state is the
    next step's acted-on state.

    (These fakes carry no real image, so the pair is asserted on the observation
    records, which are written either way.)
    """
    engine, _, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])
    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    written = {p.name for p in (tmp_path / "run-test" / "observations").glob("*.json")}
    acted = {n for n in written if not n.endswith(".after.json")}
    after = {n for n in written if n.endswith(".after.json")}
    assert acted, "no step recorded the screen it acted on"
    assert after, "no step recorded what its action produced"
    # Every step that produced an effect has both, under its own step number.
    for name in after:
        assert name.replace(".after", "") in acted, name


async def test_an_unchanged_screen_is_not_re_interpreted_while_polling(
    tmp_path: Path,
) -> None:
    """A checkpoint that has not come true is polled against a hash, not an OCR
    pass. The verdict on a byte-identical frame cannot differ from the one we
    already computed, so re-reading it is work whose answer is known."""
    # The checkpoint never holds, so the step polls to its deadline.
    stuck = frame("Search", "nothing this capability is waiting for")
    perceiver = FakePerceiver([stuck, stuck])
    driver = FakeDriver(perceiver)
    engine = ReplayEngine(
        perceiver=perceiver,
        driver=driver,
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "run-test", Redactor()),
        control=RunControl(run_id="run-test"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
        step_timeout_ms=300,
    )
    result = await engine.replay(open_profile(), INPUTS)

    assert result.status is RunStatus.FAILURE
    # Without the gate this polls for 300ms at one full perception per turn. The
    # exact count depends on timing; the property is that it is small and bounded
    # rather than proportional to the timeout.
    assert result.steps[0].phases.observations <= 3, result.steps[0].phases


# ---------------------------------------------------------------------------
# a session that dies mid-flow
# ---------------------------------------------------------------------------


class ExpiringApp:
    """A perceiver and driver in one, whose session dies at a chosen moment.

    Same shape as `ModalApp` and for the same reason: whether the run can get
    itself out of this depends on *when* the session dies relative to what the run
    has already done, which is not a property a frame list can express.
    """

    def __init__(self, expire_at: int | None = None, screen: Observation = ACCOUNTS_ROW) -> None:
        # None means "not on a timer" — the test trips it by hand. A large number
        # would not do: a step polls its screen every millisecond, so any count
        # meant as "never" is reachable inside one timeout.
        self.expire_at = expire_at
        self.screen = screen
        self.observations = 0
        self.expired = False
        self.sign_ons = 0
        self.calls: list[tuple[str, Any]] = []

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        self.observations += 1
        if self.expire_at is not None and self.observations == self.expire_at:
            self.expired = True
        return EXPIRED if self.expired else self.screen

    def peek(self, out_path: Path) -> str:
        return (EXPIRED if self.expired else self.screen).frame_hash or ""

    async def sign_on(self) -> None:
        """What `Session.authenticate` does, from the engine's point of view."""
        self.sign_ons += 1
        self.expired = False

    async def click(self, p: Point, button: str = "left") -> None:
        self.calls.append(("click", (round(p.x, 3), round(p.y, 3))))

    async def navigate(self, url: str) -> None:
        self.calls.append(("navigate", url))

    async def reload(self) -> None:
        self.calls.append(("reload", None))

    async def type_text(self, text: str, secret: bool = False) -> None:
        self.calls.append(("type", text))

    async def key(self, keys: str) -> None:
        self.calls.append(("key", keys))

    async def scroll(self, p: Point, dy: float) -> None:
        self.calls.append(("scroll", dy))

    def current_url(self) -> str | None:
        return "http://targetapp:8080/members/12345"


def build_expiring(tmp_path: Path, app: ExpiringApp, sign_on: Any = None) -> ReplayEngine:
    return ReplayEngine(
        perceiver=app,
        driver=app,
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "run-test", Redactor()),
        control=RunControl(run_id="run-test"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
        step_timeout_ms=100,
        sign_on=sign_on,
    )


async def test_a_session_that_dies_on_a_read_signs_back_in_and_starts_over(
    tmp_path: Path,
) -> None:
    """The condition this used to escalate for, on a flow where nobody needs to be
    woken up.

    Re-authenticating does not put the run back where it was — it lands on the
    application's landing page — so the only honest recovery is to run the
    capability again. That is available precisely because nothing this flow does
    is irreversible, which is what `risk: safe` on every step *means*.
    """
    app = ExpiringApp(expire_at=3)
    engine = build_expiring(tmp_path, app, sign_on=None)
    engine.sign_on = app.sign_on

    result = await engine.replay(savings_capability(), INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert result.outputs == {"balance": 18204.55}
    assert app.sign_ons == 1
    # The steps taken before the expiry stay in the log. A run that executed step
    # 2 twice should say so rather than present a tidy five-step history.
    assert len(result.steps) > len(savings_capability().steps)


async def test_a_session_that_dies_after_a_risky_step_waits_for_a_person(
    tmp_path: Path,
) -> None:
    """The gate, and the reason it is not simply "log in again".

    Once a run has executed something irreversible, whether its first half
    committed is not a question this engine may answer by doing the whole thing
    over. It parks instead, and the operator gets the live session.
    """
    app = ExpiringApp(screen=SEARCH)         # not on a timer; tripped by hand below
    engine = build_expiring(tmp_path, app)
    engine.sign_on = app.sign_on
    engine.policy = POLICY.__class__(**{**_policy_kwargs(POLICY), "risky_disposition": "allow"})

    cap = open_profile(risk=Risk.RISKY)
    original = app.click

    async def click_then_expire(p: Point, button: str = "left") -> None:
        await original(p, button)
        app.expired = True                   # the next request bounces to sign-on

    app.click = click_then_expire            # type: ignore[method-assign]

    control = engine.control
    task = asyncio.create_task(engine.replay(cap, INPUTS))
    for _ in range(4000):
        if control.intervention is not None and control.holder.value == "nobody":
            control.take_control("operator-1")
            control.release(
                InterventionResolution(
                    id=control.intervention.id, outcome="abort", operator="operator-1"
                )
            )
        if task.done():
            break
        await asyncio.sleep(0.002)
    result = await task

    assert result.status is RunStatus.ESCALATED
    # The whole point: it did not sign itself back in and re-submit.
    assert app.sign_ons == 0


async def test_a_deployment_with_no_sign_on_recipe_escalates_rather_than_guessing(
    tmp_path: Path,
) -> None:
    app = ExpiringApp(expire_at=1)
    engine = build_expiring(tmp_path, app, sign_on=None)
    control = engine.control

    task = asyncio.create_task(engine.replay(savings_capability(), INPUTS))
    for _ in range(4000):
        if control.intervention is not None and control.holder.value == "nobody":
            control.take_control("operator-1")
            control.release(
                InterventionResolution(
                    id=control.intervention.id, outcome="abort", operator="operator-1"
                )
            )
        if task.done():
            break
        await asyncio.sleep(0.002)
    result = await task

    assert result.status is RunStatus.ESCALATED


async def test_a_declared_reload_is_carried_out(tmp_path: Path) -> None:
    """`reload` in a recovery's action list. What makes "wait and try again"
    expressible for a condition a wait alone cannot clear — a 5xx, a half-rendered
    page — rather than only for one that clears itself."""
    app = ExpiringApp()
    engine = build_expiring(tmp_path, app)
    engine.policy = POLICY.__class__(
        **{
            **_policy_kwargs(POLICY),
            "risky_disposition": POLICY.risky_disposition,
            "recoveries": (
                Recovery(
                    name="transient_app_error",
                    detector_kind="text_present",
                    detector_value="session has expired",
                    actions=({"action": "wait", "value": "1"}, {"action": "reload"}),
                    max_per_run=2,
                ),
            ),
        }
    )
    app.expired = True

    async def clearing_reload() -> None:
        app.calls.append(("reload", None))
        app.expired = False                  # the reload is what fixes it

    app.reload = clearing_reload             # type: ignore[method-assign]

    result = await engine.replay(savings_capability(), INPUTS)

    assert ("reload", None) in app.calls
    assert result.status is RunStatus.SUCCESS


async def test_a_condition_a_human_does_not_clear_stops_rather_than_parking_forever(
    tmp_path: Path,
) -> None:
    """An operator can resume without having fixed anything.

    The declared condition is still on screen, so the run classifies it again and
    would park again — and again. Parking is not free: it holds the only session,
    and a queue that keeps re-issuing the same intervention teaches operators to
    ignore it. After two the run stops and names the condition it could not get
    past, which is a thing someone can act on.
    """
    # A bare sign-on screen, with no "session has expired" on it. The distinction
    # is the point: an expiry that says so is a declared *recovery* now (sign in,
    # start over), and arriving on the sign-on screen with no explanation is not.
    # This test is about the second one.
    locked = frame("Staff Sign-On", "Please sign on to continue")
    engine, _, control = build(tmp_path, [locked, locked, locked, locked, locked])

    task = asyncio.create_task(engine.replay(savings_capability(), INPUTS))
    for _ in range(4000):
        if control.intervention is not None and control.holder.value == "nobody":
            control.take_control("operator-1")
            control.release(
                InterventionResolution(
                    id=control.intervention.id, outcome="resume", operator="operator-1"
                )
            )
        if task.done():
            break
        await asyncio.sleep(0.002)

    result = await task
    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.APP_ERROR
    assert "sign_on_required" in result.failure.message


async def test_the_automation_cannot_act_while_a_human_holds_control(tmp_path: Path) -> None:
    from cua.escalation import ControlError

    control = RunControl(run_id="run-test")
    control.holder = control.holder.__class__.HUMAN
    with pytest.raises(ControlError):
        control.assert_automation()


# ---------------------------------------------------------------------------
# ambiguity, geometry, and the value that came back
# ---------------------------------------------------------------------------


def _allowing(policy: Policy) -> Policy:
    """The same policy with risky actions allowed rather than confirmed.

    A test about ambiguity should not be answered by the risky-disposition
    escalation that would fire first — otherwise it passes for the wrong reason.
    """
    return policy.__class__(**{**_policy_kwargs(policy), "risky_disposition": "allow"})


async def test_an_anchor_that_matches_two_rows_stops_a_risky_step(tmp_path: Path) -> None:
    """Three rows whose button reads exactly "View" are three different members.

    The recorded position picks one, and on a write that is a guess with a
    member's money behind it. `find_and_act` has defaulted to escalating on
    ambiguity from the start because it is the obviously data-dependent case;
    this is the same rule on an ordinary click, which is where it was missing.
    """
    two_rows = Observation(
        screenshot_path="/nonexistent/frame.png",
        viewport=VIEWPORT,
        elements=(
            el("e0", 0.10, 0.20, 0.20, 0.02, "Confirm Transfer"),
            el("e1", 0.10, 0.40, 0.20, 0.02, "Confirm Transfer"),
        ),
        url="http://targetapp:8080/members/12345",
        frame_hash="hash",
        taken_at="2026-08-16T00:00:00+00:00",
    )
    engine, driver, control = build(tmp_path, [two_rows, frame("Transfer complete")])
    engine.policy = _allowing(POLICY)

    task = await _run_until_intervention(engine, control, risky_capability())

    assert control.intervention is not None
    assert control.intervention.reason.value == "ambiguous_match"
    # Nothing was clicked. The point is that it did not pick one and act.
    assert driver.calls == []

    control.take_control("operator-1")
    control.release(
        InterventionResolution(id=control.intervention.id, outcome="abort", operator="operator-1")
    )
    result = await task
    assert result.status is RunStatus.ESCALATED


async def test_a_substring_match_is_not_ambiguity(tmp_path: Path) -> None:
    """The heading "Member Search" is not a rival for the "Search" button.

    `contains` is the right default — a balance lives inside "Available Balance:
    $18,204.55" — but it means the raw match count is inflated on nearly every
    screen. A rule that read it directly would park a risky step on a screen where
    a human sees no ambiguity at all, which is the failure mode that teaches
    operators to ignore the queue.
    """
    screen = Observation(
        screenshot_path="/nonexistent/frame.png",
        viewport=VIEWPORT,
        elements=(
            el("e0", 0.10, 0.10, 0.30, 0.02, "Member Search"),
            el("e1", 0.10, 0.20, 0.10, 0.02, "Search"),
        ),
        url="http://targetapp:8080/members/12345",
        frame_hash="hash",
        taken_at="2026-08-16T00:00:00+00:00",
    )
    cap = risky_capability()
    step = cap.steps[0].model_copy(
        update={
            "target": cap.steps[0].target.model_copy(update={"anchor_text": "Search"}),
        }
    )
    engine, driver, _ = build(tmp_path, [screen, frame("Transfer complete")])
    engine.policy = _allowing(POLICY)

    result = await engine.replay(cap.model_copy(update={"steps": [step]}), INPUTS)

    assert result.status is RunStatus.SUCCESS
    # And it clicked the button, not the heading.
    assert any(kind == "click" and round(point[1], 2) == 0.21 for kind, point in driver.calls)


async def test_a_display_of_a_different_shape_refuses_to_replay(tmp_path: Path) -> None:
    """Normalized coordinates survive scaling. They do not survive reflow.

    `recording.viewport` has always been in the artifact and nothing compared it
    to anything, which made "the recording viewport is part of the contract" a
    sentence rather than a fact.
    """
    from cua.schema import Recording

    cap = savings_capability(
        recording=Recording(
            run_id="discover-test",
            model="test",
            viewport=Viewport(width=1024, height=1024),   # square: a different shape
            recorded_at="2026-08-16T00:00:00+00:00",
        )
    )
    engine, _, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert "1024x1024" in (result.failure.expected or "")
    assert "1440x900" in (result.failure.observed or "")


async def test_a_scaled_display_replays_and_says_so(tmp_path: Path) -> None:
    """Twice the pixels, same shape: every recorded box still covers what it covered."""
    from cua.schema import Recording

    cap = savings_capability(
        recording=Recording(
            run_id="discover-test",
            model="test",
            viewport=Viewport(width=720, height=450),
            recorded_at="2026-08-16T00:00:00+00:00",
        )
    )
    engine, _, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert "same shape, scaled" in (result.steps[0].note or "")


async def test_a_balance_outside_its_declared_range_is_not_returned(tmp_path: Path) -> None:
    """Type-valid is not the same as plausible.

    A misread digit turns 18204.55 into 1820455, which coerces to a float, passes
    every checkpoint — the screen is the right screen — and is a number a
    downstream agent will quote to a member. The checkpoint says where we are; only
    a declared bound says whether what we read can be right.
    """
    cap = savings_capability()
    bounded = cap.outputs[0].model_copy(update={"constraints": Constraints(max=1_000_000)})
    engine, _, _ = build(
        tmp_path,
        [
            frame("Sign-On placeholder"),
            # The same row the successful test uses, with one digit group too
            # many — which is what an OCR misread of a currency value looks like.
            ACCOUNTS_ROW.model_copy(
                update={
                    "elements": (
                        *ACCOUNTS_ROW.elements[:-1],
                        el("e4", 0.45, 0.20, 0.09, 0.02, "$1,820,455.00"),
                    )
                }
            ),
        ],
    )

    result = await engine.replay(cap.model_copy(update={"outputs": [bounded]}), INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    # Not EXTRACTION_FAILED: we read it fine. It is the value that is wrong, and
    # that sends an operator somewhere else entirely.
    assert result.failure.kind is FailureKind.OUTPUT_REJECTED
    assert result.outputs == {}


async def test_an_outcome_declared_by_the_app_is_inherited_by_name(tmp_path: Path) -> None:
    """The app owns the wording; the capability owns whether it may be returned.

    Teaching a detector once per application rather than once per capability is
    what makes the long tail affordable: the first institution to meet a screen
    pays for every capability at every institution afterwards.
    """
    cap = savings_capability(
        business_outcomes=[BusinessOutcome(name="member_not_found")]   # no detector
    )
    engine, _, _ = build(
        tmp_path,
        [
            frame("Sign-On placeholder"),
            frame("Member Search", "No member matches the search criteria entered."),
        ],
    )

    result = await engine.replay(cap, {"member_id": "99999", "account_nickname": "Primary Savings"})

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome is not None
    assert result.outcome.name == "member_not_found"
    assert result.outcome.fields == {"member_id": "99999"}


async def test_an_outcome_the_app_does_not_declare_stops_before_anything_is_touched(
    tmp_path: Path,
) -> None:
    """The quiet failure this prevents: an unresolved detector never matches.

    The run would not error — it would simply report "no such member" as a
    checkpoint failure while the capability's contract went on advertising the
    outcome to its callers.
    """
    cap = savings_capability(business_outcomes=[BusinessOutcome(name="account_dormant")])
    engine, driver, _ = build(tmp_path, [frame("Sign-On placeholder"), ACCOUNTS_ROW])

    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert "account_dormant" in result.failure.message
    assert driver.calls == []
