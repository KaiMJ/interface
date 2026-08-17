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
from cua.policy import Policy, Redactor
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

POLICY = Policy.load(Path(__file__).resolve().parent.parent / "policies" / "targetapp.yaml")
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
        return self.frames[min(self.index, len(self.frames) - 1)]


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


async def test_the_automation_cannot_act_while_a_human_holds_control(tmp_path: Path) -> None:
    from cua.escalation import ControlError

    control = RunControl(run_id="run-test")
    control.holder = control.holder.__class__.HUMAN
    with pytest.raises(ControlError):
        control.assert_automation()
