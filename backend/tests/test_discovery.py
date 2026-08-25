"""The discovery loop and artifact synthesis.

The model is scripted here, so what is under test is everything around it: that a
mark becomes a typed step, that a step whose expectation did not come true is
discarded rather than recorded, that policy is enforced on this path too, and that
what synthesis emits is an artifact the replay engine can actually execute.

The last test is the one that matters most. It records a capability with a
scripted model and then replays it through the real engine — the whole thread,
discover -> synthesize -> replay, with no browser and no API key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDriver, FakePerceiver, ScriptedLLM, el, frame, row_frame

from cua.discovery import actions as tools
from cua.discovery.llm import ToolCall
from cua.discovery.loop import DiscoveryLoop
from cua.discovery.synthesize import parameterize, prune, synthesize
from cua.escalation import RunControl
from cua.evidence import EvidenceWriter
from cua.policy import Policy, Redactor
from cua.replay.engine import ReplayEngine
from cua.resolve import Resolver
from cua.schema import (
    ActStep,
    AppRef,
    Capability,
    InterventionResolution,
    Observation,
    Primitive,
    Relation,
    ResolutionTier,
    RunStatus,
    Status,
    Target,
    ValueType,
    Viewport,
)

POLICY = Policy.load(Path(__file__).resolve().parents[2] / "policies" / "targetapp.yaml")
BASE = "http://targetapp:8080"
GOAL = "look up a member and read their savings balance"
INPUTS = {"member_id": "12345", "account_nickname": "Primary Savings"}



class ApprovingOperator(RunControl):
    """A control token whose human always says yes, immediately.

    Discovery now parks on a risky action exactly as replay does, so without an
    operator every test whose intent trips the policy's mutation-verb promotion
    would deadlock. These tests are about the loop, not about the handoff — the
    parking itself is asserted separately, below, with a real `RunControl` and a
    real await.
    """

    async def escalate(self, req: Any) -> Any:
        self.park(req)
        self.take_control("test-operator")
        resolution = InterventionResolution(
            id=req.id, outcome="resume", operator="test-operator"
        )
        self.release(resolution)
        return resolution


def build(
    tmp_path: Path, frames: list[Any], script: list[ToolCall], declaration: Any = None
) -> tuple[DiscoveryLoop, FakeDriver, ScriptedLLM]:
    # The loop navigates to the start URL before its first observation, which
    # advances the scripted screens by one — so the caller's first frame is the
    # first frame the model actually sees.
    perceiver = FakePerceiver([frame("Meridian Credit Union"), *frames])
    driver = FakeDriver(perceiver)
    llm = ScriptedLLM(script, declaration)
    control = ApprovingOperator(run_id="discover-test")
    loop = DiscoveryLoop(
        perceiver=perceiver,
        driver=driver,
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "discover-test", Redactor()),
        llm=llm,
        control=control,
        max_steps=6,
        settle_timeout_ms=100,
        settle_poll_ms=1,
    )
    return loop, driver, llm, control


# The screens a run walks through: the member page, then the same page after the
# accounts grid has been read.
MEMBER = frame("Member Profile", "Dolores Chen", "Member ID 12345")
ACCOUNTS = row_frame("29455", "Primary Savings", "Active", "$18,204.55")


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


async def test_a_recorded_step_is_one_whose_expectation_came_true(tmp_path: Path) -> None:
    loop, driver, _, _control = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "open the accounts grid",
                    "expect": "Primary Savings",
                    "risk": "safe",
                },
            ),
            ToolCall(
                name="finish",
                input={"summary": "done", "success_text": "Primary Savings"},
            ),
        ],
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.status is RunStatus.SUCCESS
    # The entry navigation plus the click. A capability establishes its own
    # starting state rather than inheriting whatever the browser was showing.
    assert result.steps_taken == 2
    assert loop.state.steps[0].action is Primitive.NAVIGATE
    assert ("click", (0.25, 0.11)) in driver.calls


async def test_a_step_whose_expectation_failed_is_discarded_and_explained(
    tmp_path: Path,
) -> None:
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, MEMBER],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "open the member search screen",
                    "expect": "Transfer complete",
                    "risk": "safe",
                },
            ),
            ToolCall(name="escalate", input={"reason": "that did not work"}),
        ],
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    # Not recorded: nothing changed and the model said it would. Only the entry
    # navigation survives.
    assert result.steps_taken == 1
    assert result.status is RunStatus.ESCALATED
    # And the model was told what is actually there, which is what lets it adapt
    # rather than repeat itself.
    assert "DISCARDED" in llm.prompts[-1]
    assert "Member Profile" in llm.prompts[-1]


async def test_an_action_that_changed_the_screen_is_kept_without_its_checkpoint(
    tmp_path: Path,
) -> None:
    # Wrong about *what* would appear, not about whether the action did something.
    # Dropping it leaves the recording missing a state transition the flow made, and
    # replay then starts from a screen it never reaches.
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "open the accounts grid",
                    "expect": "Transfer complete",
                    "risk": "safe",
                },
            ),
            ToolCall(
                name="finish",
                input={"summary": "done", "success_text": "Primary Savings"},
            ),
        ],
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert result.steps_taken == 2
    recorded = loop.state.steps[1]
    # Kept, so the flow reproduces; checkpointless, because an assertion the run
    # could not verify has no business in an artifact; and flagged for review.
    assert recorded.checkpoint is None
    assert "Review before approving" in (recorded.note or "")
    assert "you expected" in llm.prompts[-1]


async def test_policy_is_enforced_on_the_discovery_path_too(tmp_path: Path) -> None:
    loop, driver, llm, _control = build(
        tmp_path,
        [MEMBER, MEMBER],
        [
            ToolCall(
                name="navigate",
                input={
                    "url": "http://evil.example/",
                    "intent": "go somewhere else",
                    "expect": "anything",
                },
            ),
            ToolCall(name="escalate", input={"reason": "blocked"}),
        ],
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.steps_taken == 1
    # The first navigate is the loop's own start-url; the denied one never ran.
    assert driver.calls == [("navigate", BASE)]
    assert "refused by policy" in llm.prompts[-1]


async def test_finishing_requires_the_screen_to_agree(tmp_path: Path) -> None:
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, MEMBER],
        [
            ToolCall(
                name="click",
                input={"mark": 0, "intent": "look", "expect": "Member Profile", "risk": "safe"},
            ),
            ToolCall(
                name="finish",
                input={"summary": "done", "success_text": "Transfer complete"},
            ),
            ToolCall(name="escalate", input={"reason": "cannot prove success"}),
        ],
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.status is RunStatus.ESCALATED
    assert "tried to finish" in llm.prompts[-1]


async def test_a_stuck_run_escalates_before_the_step_budget_runs_out(tmp_path: Path) -> None:
    repeat = ToolCall(
        name="click",
        input={"mark": 0, "intent": "click the same thing", "expect": "Member Profile",
               "risk": "safe"},
    )
    loop, _, _, _control = build(
        tmp_path, [MEMBER], [repeat, repeat, repeat, repeat, repeat, repeat]
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.status is RunStatus.ESCALATED
    assert "not making progress" in result.stop_reason or "repeated" in result.stop_reason
    # The point of detecting it early: an operator gets told what went wrong
    # instead of receiving twenty near-identical screenshots at max-steps.
    assert result.steps_taken < 6


# ---------------------------------------------------------------------------
# synthesis
# ---------------------------------------------------------------------------


def test_parameterize_substitutes_declared_inputs_longest_first() -> None:
    steps = [
        ActStep(
            id=1,
            action=Primitive.NAVIGATE,
            value="http://targetapp:8080/members/12345",
        )
    ]
    rewritten, specs = parameterize(steps, {"member_id": "12345", "branch": "123"})

    assert rewritten[0].value == "http://targetapp:8080/members/{{member_id}}"
    # `branch` never appeared, so it is not a parameter of this flow. Declaring it
    # would tell a calling agent it can steer something it cannot.
    assert [s.name for s in specs] == ["member_id"]
    assert specs[0].type is ValueType.STRING


def test_prune_drops_a_navigation_immediately_superseded() -> None:
    steps = [
        ActStep(id=1, action=Primitive.NAVIGATE, value="http://targetapp:8080/members"),
        ActStep(id=2, action=Primitive.NAVIGATE, value="http://targetapp:8080/members/12345"),
    ]
    assert [s.value for s in prune(steps, [])] == ["http://targetapp:8080/members/12345"]


async def test_synthesis_rejects_a_success_phrase_that_is_not_on_the_final_screen(
    tmp_path: Path,
) -> None:
    loop, _, llm, _control = build(
        tmp_path,
        [ACCOUNTS],
        [
            ToolCall(
                name="extract",
                input={"mark": 3, "output_name": "balance", "intent": "read the balance"},
            ),
            ToolCall(name="finish", input={"summary": "done", "success_text": "Primary Savings"}),
        ],
        declaration={
            "description": "reads a balance",
            # Not on the final screen. The run's own verified phrase is.
            "success_text": "Wire transfer submitted",
            "business_outcomes": [],
        },
    )
    await loop.run(GOAL, BASE, INPUTS)
    cap = await synthesize(loop.state, INPUTS, llm, capability_id="cap_test")

    assert cap.success.value == "Primary Savings"


async def test_a_recorded_capability_replays(tmp_path: Path) -> None:
    """The whole thread: a scripted run, synthesized, then executed by the engine.

    No browser, no key, no hand-written artifact — if the shape discovery emits
    were not the shape replay consumes, this is where it would show.
    """
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="navigate",
                input={
                    "url": f"{BASE}/members/12345",
                    "intent": "open member 12345's profile",
                    "expect": "Primary Savings",
                },
            ),
            ToolCall(
                name="extract",
                input={
                    "mark": 3,
                    "output_name": "balance",
                    "intent": "read the current balance for Primary Savings",
                },
            ),
            ToolCall(
                name="finish",
                input={"summary": "read it", "success_text": "Primary Savings"},
            ),
        ],
        declaration={
            "description": "Reads the current balance of a named account.",
            "success_text": "Primary Savings",
            "business_outcomes": [
                {
                    "name": "member_not_found",
                    "description": "no member with that id",
                    "detector_text": "No member record found",
                }
            ],
        },
    )
    discovery = await loop.run(GOAL, BASE, INPUTS)
    assert discovery.status is RunStatus.SUCCESS

    cap = await synthesize(
        loop.state,
        INPUTS,
        llm,
        capability_id="cap_get_savings_balance",
        app=AppRef(name="targetapp", base_url_pattern=f"^{BASE}(/.*)?$"),
        viewport=Viewport(width=1440, height=900),
    )

    assert cap.status is Status.DRAFT              # a human approves before unattended use
    assert [i.name for i in cap.inputs] == ["member_id", "account_nickname"]
    assert cap.steps[0].action is Primitive.NAVIGATE
    assert [o.name for o in cap.outputs] == ["balance"]
    assert cap.outputs[0].type is ValueType.NUMBER  # read off what was extracted
    assert "{{member_id}}" in (cap.steps[0].value or "")
    assert [o.name for o in cap.business_outcomes] == ["member_not_found"]

    # Now run it. Different fakes, same artifact.
    perceiver = FakePerceiver([frame("start"), ACCOUNTS])
    driver = FakeDriver(perceiver)
    engine = ReplayEngine(
        perceiver=perceiver,
        driver=driver,
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "replay-test", Redactor()),
        control=RunControl(run_id="replay-test"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
    )
    result = await engine.replay(cap, INPUTS)

    assert result.status is RunStatus.SUCCESS
    assert result.outputs == {"balance": 18204.55}
    assert driver.calls[0] == ("navigate", f"{BASE}/members/12345")


@pytest.mark.parametrize("member_id", ["99999"])
async def test_a_recorded_capability_reports_the_outcome_it_declared(
    tmp_path: Path, member_id: str
) -> None:
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="navigate",
                input={
                    "url": f"{BASE}/members/12345",
                    "intent": "open the member's profile",
                    "expect": "Primary Savings",
                },
            ),
            ToolCall(
                name="finish",
                input={"summary": "read it", "success_text": "Primary Savings"},
            ),
        ],
        declaration={
            "description": "Reads a balance.",
            "success_text": "Primary Savings",
            "business_outcomes": [
                {
                    "name": "member_not_found",
                    "description": "no member with that id",
                    "detector_text": "No member record found",
                }
            ],
        },
    )
    await loop.run(GOAL, BASE, INPUTS)
    cap = await synthesize(loop.state, INPUTS, llm, capability_id="cap_x")

    perceiver = FakePerceiver([frame("start"), frame("No member record found for ID 99999.")])
    engine = ReplayEngine(
        perceiver=perceiver,
        driver=FakeDriver(perceiver),
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "replay-outcome", Redactor()),
        control=RunControl(run_id="replay-outcome"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
    )
    result = await engine.replay(cap, {**INPUTS, "member_id": member_id})

    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome is not None
    assert result.outcome.name == "member_not_found"


# ---------------------------------------------------------------------------
# screens
# ---------------------------------------------------------------------------


async def test_a_recording_declares_no_screens_it_cannot_justify(tmp_path: Path) -> None:
    """One run cannot tell an application's chrome from one record's data.

    A first version derived screens from a single run by taking the longest line
    unique to each frame. It named the member profile `riverside_004` — after the
    member's branch — which identifies the record rather than the screen, so the
    capability would have refused to run for anybody else. Declaring nothing is
    the honest answer; see `synthesize` for where derivation belongs.
    """
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "open the member",
                    "expect": "Primary Savings",
                    "risk": "safe",
                },
            ),
            ToolCall(
                name="finish",
                input={"summary": "done", "success_text": "Primary Savings"},
            ),
        ],
    )
    await loop.run(GOAL, BASE, INPUTS)
    cap = await synthesize(loop.state, INPUTS, llm, capability_id="cap_screens")

    assert cap.screens == []
    assert all(s.screen is None for s in cap.steps)


async def test_a_step_on_the_wrong_screen_says_where_it_is(tmp_path: Path) -> None:
    from cua.schema import CheckKind, Checkpoint, FailureKind, Screen

    cap = Capability(
        id="cap_two_screens",
        goal="read something on the second screen",
        app=AppRef(name="targetapp", base_url_pattern=f"^{BASE}(/.*)?$"),
        screens=[
            Screen(
                name="member_profile",
                signature=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Member Profile"),
            ),
            Screen(
                name="sign_on",
                signature=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Staff Sign-On"),
            ),
        ],
        steps=[
            ActStep(
                id=1,
                action=Primitive.EXTRACT,
                screen="member_profile",
                extract_as="balance",
                target=Target(
                    intent="read the balance",
                    target_desc="the balance cell",
                    anchor_text="Primary Savings",
                ),
            )
        ],
        success=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50),
    )

    # The session timed out and the app bounced to sign-on. Without the screen
    # claim this reads "the target was not found", which sends an operator hunting
    # for a layout change that never happened.
    perceiver = FakePerceiver([frame("Meridian Credit Union", "Staff Sign-On", "User ID")])
    engine = ReplayEngine(
        perceiver=perceiver,
        driver=FakeDriver(perceiver),
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "wrong-screen", Redactor()),
        control=RunControl(run_id="wrong-screen"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
    )
    result = await engine.replay(cap, {})

    assert result.status is RunStatus.FAILURE
    assert result.failure is not None
    assert result.failure.kind is FailureKind.WRONG_SCREEN
    assert result.failure.expected == "member_profile"
    assert result.failure.observed == "sign_on"


# ---------------------------------------------------------------------------
# the guardrail binds the recording session too
# ---------------------------------------------------------------------------


async def _until_parked(task: Any, control: RunControl) -> None:
    for _ in range(500):
        if control.intervention is not None:
            return
        await asyncio.sleep(0.005)
    task.cancel()
    raise AssertionError("discovery never parked")


async def test_discovery_parks_on_a_risky_action_and_proceeds_when_approved(
    tmp_path: Path,
) -> None:
    """A risky action is risky whoever is driving.

    Replay parks before a risky step; so does discovery. Letting a recording
    session submit a transfer because a human is notionally watching a VNC
    window is a guardrail that binds only the path that needs it least.
    """
    loop, driver, llm, _ = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "confirm the transfer",
                    "expect": "Primary Savings",
                    "risk": "risky",
                },
            ),
            ToolCall(name="finish", input={"success_text": "Primary Savings", "summary": "done"}),
        ],
    )
    control = RunControl(run_id="discover-test")
    loop.control = control

    task = asyncio.create_task(loop.run(GOAL, BASE, INPUTS))
    await _until_parked(task, control)

    assert control.intervention is not None
    assert control.intervention.mode == "discovery"
    assert control.intervention.reason.value == "risky_action_confirmation"
    # Nothing was clicked while the request was open. The entry navigation is the
    # only thing the driver has been asked to do.
    assert [kind for kind, _ in driver.calls] == ["navigate"]

    control.take_control("operator-1")
    control.release(
        InterventionResolution(
            id=control.intervention.id, outcome="resume", operator="operator-1", note="checked"
        )
    )
    result = await task

    assert result.status is RunStatus.SUCCESS
    assert any(kind == "click" for kind, _ in driver.calls)


async def test_a_declined_risky_action_ends_the_recording(tmp_path: Path) -> None:
    """Abort is not "skip the step" — the recording stops.

    A capability recorded around a step a human refused would be a capability
    that omits the one action they objected to.
    """
    loop, driver, llm, _ = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "submit the transfer",
                    "expect": "Primary Savings",
                    "risk": "risky",
                },
            ),
        ],
    )
    control = RunControl(run_id="discover-test")
    loop.control = control

    task = asyncio.create_task(loop.run(GOAL, BASE, INPUTS))
    await _until_parked(task, control)
    control.take_control("operator-1")
    control.release(
        InterventionResolution(
            id=control.intervention.id, outcome="abort", operator="operator-1"
        )
    )
    result = await task

    assert result.status is RunStatus.ESCALATED
    assert "declined" in result.stop_reason
    assert [kind for kind, _ in driver.calls] == ["navigate"]


async def test_a_loop_with_no_operator_refuses_rather_than_proceeding(tmp_path: Path) -> None:
    """"Nobody can be asked" reads as no, not as yes.

    A DiscoveryLoop built without a control token cannot raise an intervention.
    The safe reading is that the risky action does not happen.
    """
    loop, driver, llm, _ = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "authorize the payment",
                    "expect": "Primary Savings",
                    "risk": "risky",
                },
            ),
        ],
    )
    loop.control = None

    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.status is RunStatus.ESCALATED
    assert [kind for kind, _ in driver.calls] == ["navigate"]


# ---------------------------------------------------------------------------
# anchors: the model proposes, the code falsifies
#
# An element's text is often part durable, part volatile — "29883 - Checking -
# $4,820.19". Recording the whole string records a balance the flow is about to
# change. Which half is which is semantic, so the model is asked and then checked
# against the frame it said it about.
# ---------------------------------------------------------------------------


OPTION_FRAME = Observation(
    screenshot_path="/nonexistent.png",
    viewport=Viewport(width=1440, height=900),
    frame_hash="h",
    taken_at="2026-08-17T00:00:00+00:00",
    url=BASE + "/transfer",
    elements=(
        el("e0", 0.05, 0.20, 0.12, 0.02, "From Account"),
        el("e1", 0.20, 0.27, 0.30, 0.02, "29883 - Checking - $4,820.19"),
        el("e2", 0.20, 0.30, 0.30, 0.02, "13455 - Savings - $18,204.55"),
    ),
)
DECLARED = ["12345", "29883", "13455"]


def _anchor(anchor: str | None, obs: Observation = OPTION_FRAME, mark: str = "e1") -> str | None:
    call = {"mark": 1, "intent": "select the source account", "expect": "x", "risk": "safe"}
    if anchor is not None:
        call["anchor"] = anchor
    step = tools.to_step("click", call, obs.by_id(mark), 4, obs, identifiers=DECLARED)
    return step.target.anchor_text if step.target else None


def test_a_declared_input_beats_the_rest_of_the_cell() -> None:
    """The floor, with no model involved at all.

    The caller named `29883` as an input for this run, which is the caller
    saying "this is the thing that varies per invocation" — i.e. the thing that
    identifies the record. Recording the balance beside it would tie the
    capability to one moment.
    """
    assert _anchor(None) == "29883"


def test_a_model_proposal_that_is_not_on_the_element_is_dropped() -> None:
    """It cannot be verified, so it is not used. Falls back to the floor."""
    assert _anchor("Source Account") == "29883"


def test_a_model_proposal_that_matches_several_elements_is_dropped() -> None:
    """An anchor that picks out three things is worse than the full string."""
    assert _anchor("-") == "29883"


def test_a_declared_input_beats_a_model_proposal_of_the_volatile_half() -> None:
    """The case that decided the ordering.

    `$4,820.19` is on the element and unique on the frame, so every check here
    passes — "will this still be true next month" is not answerable from one
    observation. A declared input is answerable, and it wins.
    """
    assert _anchor("$4,820.19") == "29883"


def test_the_model_supplies_the_anchor_when_the_code_has_nothing() -> None:
    """No declared input appears in this text, so the proposal is the only source.

    This is the case a pure code heuristic cannot cover: the merchant name is
    what identifies the row, and nobody declared it.
    """
    obs = OPTION_FRAME.model_copy(
        update={
            "elements": (
                el("e0", 0.05, 0.20, 0.12, 0.02, "Merchant"),
                el("e1", 0.20, 0.27, 0.40, 0.02, "PACIFIC WIRELESS  Telecom  ($441.56)"),
            )
        }
    )
    assert _anchor("PACIFIC WIRELESS", obs) == "PACIFIC WIRELESS"


def test_stable_text_is_left_alone() -> None:
    """A button reading "View" needs no rewriting, and must not get one."""
    obs = OPTION_FRAME.model_copy(
        update={"elements": (el("e1", 0.60, 0.27, 0.06, 0.02, "View"),)}
    )
    assert _anchor(None, obs) == "View"
    assert _anchor("View", obs) == "View"


async def test_a_dropdown_option_survives_the_balance_changing(tmp_path: Path) -> None:
    """End to end: the recording must still resolve after the flow it records
    has moved the money it was anchored beside."""
    after = OPTION_FRAME.model_copy(
        update={
            "elements": (
                el("e0", 0.05, 0.20, 0.12, 0.02, "From Account"),
                el("e1", 0.20, 0.27, 0.30, 0.02, "29883 - Checking - $4,795.19"),
                el("e2", 0.20, 0.30, 0.30, 0.02, "13455 - Savings - $18,229.55"),
            )
        }
    )
    step = tools.to_step(
        "click",
        {"mark": 1, "intent": "select the source account", "expect": "x", "risk": "safe"},
        OPTION_FRAME.by_id("e1"),
        4,
        OPTION_FRAME,
        identifiers=DECLARED,
    )
    resolved = Resolver(allow_vlm=False).resolve(step.target, after)
    assert resolved.tier is ResolutionTier.ANCHOR_TEXT
    assert resolved.matched_text is not None and "29883" in resolved.matched_text


# ---------------------------------------------------------------------------
# naming the capability
# ---------------------------------------------------------------------------


def test_the_model_names_the_capability_when_the_name_is_usable() -> None:
    from cua.discovery.synthesize import _name

    chosen, why = _name(
        "cap_get_savings_balance",
        {"member_id": "12345", "account_nickname": "Primary Savings"},
        "open the profile for member 12345 and read the balance",
    )

    assert chosen == "cap_get_savings_balance"
    assert why == ""


def test_a_name_carrying_a_parameter_value_is_refused() -> None:
    """The one rejection worth making mechanically.

    `12345` is what varies per invocation — it is the argument, not the flow — so
    a capability parameterised by member id must not have a member id in its name.
    A run that recorded it would produce an artifact whose name is a lie the second
    time it is called.
    """
    from cua.discovery.synthesize import _name

    chosen, why = _name(
        "cap_get_balance_for_12345",
        {"member_id": "12345"},
        "read the balance for member 12345",
    )

    assert chosen != "cap_get_balance_for_12345"
    assert "member_id" in why


def test_an_unusable_name_falls_back_to_the_goal() -> None:
    from cua.discovery.synthesize import _name

    # "!!" is the interesting one: it normalises to nothing, and the slug helper's
    # own fallback would otherwise ship a capability literally called "capability".
    for proposed in ("", "!!", "x", "Cap With Spaces And A Very Long Name" * 3):
        chosen, why = _name(proposed, {}, "read a balance")
        assert chosen == "read_a_balance", proposed
        assert why, proposed


def test_the_callers_own_id_is_never_second_guessed(tmp_path: Path) -> None:
    """`--capability-id` is someone naming their own artifact. Nothing above
    overrides that — the refutation is for a name nobody chose."""
    from cua.discovery.synthesize import _name

    # _name is not consulted at all in that path; this pins the precedence that
    # `synthesize` implements: caller, then model, then slug.
    assert _name("cap_x", {}, "goal")[0] == "cap_x"


# ---------------------------------------------------------------------------
# the expectation: refuted the way the anchor is refuted
# ---------------------------------------------------------------------------


def test_an_expectation_that_is_this_records_money_is_refused() -> None:
    """The measured failure, in one line.

    A recording of `get_account_balance` asserted `$18,204.55` at its final step.
    Every replay for a different member navigated perfectly and then failed that
    assertion, because the assertion was about the member it was recorded on.
    """
    assert tools.durable_expect("$18,204.55") is None


def test_an_expectation_that_is_a_date_is_refused() -> None:
    """`_ANCHOR` already warns against dates by name. Same class, same answer."""
    assert tools.durable_expect("Member Since 2019-11-02") is None


def test_the_applications_own_words_survive() -> None:
    """What a checkpoint is for: text the app renders whoever is on screen."""
    assert tools.durable_expect("Member Profile") == "Member Profile"
    assert tools.durable_expect("Accounts") == "Accounts"
    # Digits are not the test — this is a wizard position, true for every record.
    assert tools.durable_expect("Step 2 of 3") == "Step 2 of 3"


def test_a_declared_input_is_not_refused() -> None:
    """Deliberately exempt, and the reason is `parameterize`.

    `expect: '12345'` after typing the member id is a real check that the
    keystrokes landed, and synthesis rewrites it to `{{member_id}}`, which is true
    for every invocation. Refuting it would delete a working assertion.
    """
    assert tools.durable_expect("12345", identifiers=["12345"]) == "12345"


def test_an_expectation_the_run_already_read_off_the_screen_is_refused() -> None:
    """A capability asserting its own output is asserting last run's data."""
    row = "13455 Savings Primary Savings Active $18,204.55"
    assert tools.durable_expect("Active", extracted=[row]) is None


def test_a_name_survives_and_that_is_the_known_residual() -> None:
    """Stated rather than hidden.

    `Marcus Webb` identifies one record and one frame cannot tell it from a
    heading. What catches it is the reviewer at `approve`, and a second run with
    different inputs — not this function.
    """
    assert tools.durable_expect("Marcus Webb") == "Marcus Webb"


async def test_a_step_keeps_its_action_and_loses_only_the_bad_expectation(
    tmp_path: Path,
) -> None:
    """Refuted, not rejected — and the model is told, mid-run.

    The click worked and the balance really was on the next screen, so discarding
    the step would lose a state transition the flow made. What is dropped is the
    assertion, and the model hears about it in time to choose better on the step
    after this one.
    """
    loop, _, llm, _control = build(
        tmp_path,
        [MEMBER, ACCOUNTS],
        [
            ToolCall(
                name="click",
                input={
                    "mark": 0,
                    "intent": "open the accounts grid",
                    "expect": "$18,204.55",
                    "risk": "safe",
                },
            ),
            ToolCall(name="finish", input={"summary": "done", "success_text": "Primary Savings"}),
        ],
    )
    result = await loop.run(GOAL, BASE, INPUTS)

    assert result.status is RunStatus.SUCCESS
    # Kept: navigate, then the click.
    assert result.steps_taken == 2
    assert loop.state.steps[1].checkpoint is None
    # Both halves on the record, so a reviewer sees the refutation and not just a
    # step that happens to have no checkpoint.
    turn = result.steps[-1].model_turn
    assert turn is not None
    assert turn.expect == "$18,204.55"
    assert turn.expect_recorded is None
    assert "not recorded as a checkpoint" in llm.prompts[-1]


# ---------------------------------------------------------------------------
# find_and_click / find_and_extract
# ---------------------------------------------------------------------------


def _find(tool_name: str, **extra: Any) -> Any:
    call = {
        "scope_anchor": "Accounts",
        "terms": ["Primary Savings"],
        "intent": "read the balance on the savings row",
        **extra,
    }
    return tools.to_step(tool_name, call, None, 5, None, identifiers=DECLARED)


def test_reading_a_row_cannot_carry_an_expectation_at_all() -> None:
    """The structural half of the fix.

    Extraction does not change the screen, so "what will be on screen after this"
    has no answer but the value just read. The field is absent from the tool rather
    than described better, which is what makes it unavailable to a model that has
    not read the description.
    """
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools.TOOLS}
    assert "expect" not in schemas["find_and_extract"]["properties"]
    assert "expect" in schemas["find_and_click"]["properties"]

    step = _find("find_and_extract", output_name="balance")
    assert step.on_found_action is Primitive.EXTRACT
    assert step.on_found_extract_as == "balance"
    assert step.checkpoint is None


def test_a_read_is_given_no_checkpoint_even_if_one_is_supplied() -> None:
    """Belt to the schema's braces.

    `additionalProperties: false` is the provider's job to enforce, and a provider
    that does not would put the field back. The recorded step is the same either way.
    """
    step = _find("find_and_extract", output_name="balance", expect="$18,204.55")
    assert step.checkpoint is None


def test_clicking_a_row_still_carries_one() -> None:
    """Opening a record does change the screen, so an expectation is a real check."""
    step = _find("find_and_click", expect="Member Profile", risk="safe")
    assert step.on_found_action is Primitive.CLICK
    assert step.checkpoint is not None
    assert step.checkpoint.value == "Member Profile"


def test_a_row_read_can_name_its_column() -> None:
    """The gap that made a correct recording impossible.

    `on_found_extract_column` has always worked in replay — the cell is located by
    its header and horizontal overlap, the way a person reads a table. No discovery
    tool ever exposed it, so every recorded row-read returned the whole row, the
    declared output type failed to coerce, and the failure surfaced at the caller
    instead of at the read.
    """
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in tools.TOOLS}
    assert "column" in schemas["find_and_extract"]["properties"]

    step = _find("find_and_extract", output_name="balance", column="Current Balance")
    assert step.on_found_extract_column == "Current Balance"


def test_a_row_read_without_a_column_still_records() -> None:
    """Optional, because not every match is in a table."""
    step = _find("find_and_extract", output_name="balance")
    assert step.on_found_extract_column is None


def test_a_read_is_policy_checked_as_a_read() -> None:
    """One tool name for both shapes meant an extract was decided as a click."""
    assert tools.PRIMITIVES["find_and_extract"] is Primitive.EXTRACT
    assert tools.PRIMITIVES["find_and_click"] is Primitive.CLICK


# ---------------------------------------------------------------------------
# a recorded extraction names its column
# ---------------------------------------------------------------------------

ACCOUNTS_GRID = Observation(
    screenshot_path="/nonexistent.png",
    viewport=Viewport(width=1440, height=900),
    frame_hash="h",
    taken_at="2026-08-18T00:00:00+00:00",
    url=BASE + "/members/12345",
    elements=(
        el("h0", 0.02, 0.20, 0.08, 0.02, "Account"),
        el("h1", 0.13, 0.20, 0.05, 0.02, "Type"),
        el("h2", 0.24, 0.20, 0.09, 0.02, "Nickname"),
        el("h3", 0.40, 0.20, 0.07, 0.02, "Status"),
        el("h4", 0.49, 0.20, 0.14, 0.02, "Current Balance"),
        el("h5", 0.68, 0.20, 0.16, 0.02, "Available Balance"),
        el("r0", 0.02, 0.28, 0.06, 0.02, "13455"),
        el("r1", 0.13, 0.28, 0.07, 0.02, "Savings"),
        el("r2", 0.24, 0.28, 0.13, 0.02, "Primary Savings"),
        el("r3", 0.40, 0.28, 0.06, 0.02, "Active"),
        el("r4", 0.60, 0.28, 0.06, 0.02, "$18,204.55"),
        el("r5", 0.84, 0.28, 0.08, 0.02, "$18,204.55"),
    ),
)


def test_a_recorded_extraction_names_the_column_it_read() -> None:
    """Both addresses, from one frame, with no model involved.

    The anchor is still the declared input — that is what makes the step
    data-dependent — but the cell is now the one under 'Current Balance' rather than
    the second thing to the right, which is a count of this row's filled-in cells.
    """
    step = tools.to_step(
        "extract",
        {"mark": 10, "intent": "read the balance", "output_name": "balance"},
        ACCOUNTS_GRID.by_id("r4"),
        5,
        ACCOUNTS_GRID,
        identifiers=["12345", "Primary Savings"],
    )
    assert step.target is not None
    assert step.target.anchor_text == "Primary Savings"
    assert step.target.column == "Current Balance"


def test_a_section_heading_is_not_mistaken_for_a_column() -> None:
    """A row of one spans the full width, so it contains every cell on the screen.

    Without a floor on what counts as a header row, the heading above the grid would
    be named as the column for every value under it — and would then resolve to
    whatever cell happened to be leftmost.
    """
    titled = Observation(
        screenshot_path="/nonexistent.png",
        viewport=Viewport(width=1440, height=900),
        frame_hash="h",
        taken_at="2026-08-18T00:00:00+00:00",
        url=BASE + "/members/12345",
        elements=(
            el("t0", 0.02, 0.14, 0.08, 0.02, "Accounts"),
            *ACCOUNTS_GRID.elements,
        ),
    )
    step = tools.to_step(
        "extract",
        {"mark": 10, "intent": "read the balance", "output_name": "balance"},
        titled.by_id("r4"),
        5,
        titled,
        identifiers=["Primary Savings"],
    )
    assert step.target is not None
    assert step.target.column == "Current Balance"


def test_a_form_field_under_a_navigation_bar_records_no_column() -> None:
    """The regression this cost a live run to find.

    A nav bar is a row of three or more texts sitting above everything on the page,
    which is the same shape as a header row. The search box took "Member Search" — a
    nav item — as its column, replay resolved the band that name spans, and the query
    went out empty. Two things rule it out: a column addresses a cell being *read*,
    and the element has to be in a table row itself.
    """
    page = Observation(
        screenshot_path="/nonexistent.png",
        viewport=Viewport(width=1440, height=900),
        frame_hash="h",
        taken_at="2026-08-18T00:00:00+00:00",
        url=BASE + "/members",
        elements=(
            el("n0", 0.02, 0.08, 0.11, 0.02, "Member Search"),
            el("n1", 0.20, 0.08, 0.09, 0.02, "Transfers"),
            el("n2", 0.34, 0.08, 0.06, 0.02, "Holds"),
            el("n3", 0.45, 0.08, 0.06, 0.02, "Cards"),
            el("f0", 0.02, 0.18, 0.10, 0.02, "Member ID or Name"),
            el("f1", 0.16, 0.18, 0.14, 0.03, ""),
        ),
    )
    step = tools.to_step(
        "type_text",
        {"mark": 5, "text": "12345", "intent": "enter the member id"},
        page.by_id("f1"),
        2,
        page,
        identifiers=["12345"],
    )
    assert step.target is not None
    assert step.target.anchor_text == "Member ID or Name"
    assert step.target.column is None


def test_a_value_outside_any_table_records_no_column() -> None:
    """Nothing to find, nothing recorded — the positional address still stands."""
    plain = Observation(
        screenshot_path="/nonexistent.png",
        viewport=Viewport(width=1440, height=900),
        frame_hash="h",
        taken_at="2026-08-18T00:00:00+00:00",
        url=BASE + "/members/12345",
        elements=(
            el("a0", 0.05, 0.40, 0.10, 0.02, "Member Since"),
            el("a1", 0.20, 0.40, 0.10, 0.02, "2019-11-02"),
        ),
    )
    step = tools.to_step(
        "extract",
        {"mark": 1, "intent": "read the join date", "output_name": "joined"},
        plain.by_id("a1"),
        5,
        plain,
        identifiers=["12345"],
    )
    assert step.target is not None
    assert step.target.column is None
    assert step.target.relation is Relation.RIGHT_OF
