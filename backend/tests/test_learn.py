"""Learning a business outcome by demonstrating one.

The mechanism this file covers is the answer to a measured failure: asked to name
the alternative results a caller must branch on, the model proposed "Accounts" —
a column header on every screen of the flow — as the detector for
`account_not_found`. Every successful run would have been reported as a business
outcome.

So: proposals are refuted against the frames the successful run actually saw, and
real wording is taken from a run that reaches the other screen.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDriver, FakePerceiver, ScriptedLLM, frame

from cua.catalog import Catalog
from cua.catalog.learn import (
    NothingToLearn,
    all_lines,
    distinguishing_text,
    final_lines,
    with_outcome,
)
from cua.discovery.synthesize import declare
from cua.escalation import RunControl
from cua.evidence import EvidenceWriter
from cua.policy import Policy, Redactor
from cua.replay.engine import ReplayEngine
from cua.resolve import Resolver
from cua.schema import (
    ActStep,
    AppRef,
    Capability,
    CheckKind,
    Checkpoint,
    InputSpec,
    Primitive,
    RunStatus,
    Status,
    ValueType,
)

POLICY = Policy.load(Path(__file__).resolve().parents[2] / "policies" / "targetapp.yaml")
BASE = "http://targetapp:8080"


def capability() -> Capability:
    return Capability(
        id="cap_read_member",
        status=Status.APPROVED,
        goal="open a member profile",
        app=AppRef(name="targetapp", base_url_pattern=f"^{BASE}(/.*)?$"),
        inputs=[InputSpec(name="member_id", type=ValueType.STRING, example="12345")],
        steps=[
            ActStep(
                id=1,
                action=Primitive.NAVIGATE,
                value=f"{BASE}/members/{{{{member_id}}}}",
                checkpoint=Checkpoint(
                    kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50
                ),
            )
        ],
        success=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Member Profile", timeout_ms=50),
    )


# ---------------------------------------------------------------------------
# the difference between two runs
# ---------------------------------------------------------------------------


def test_the_detector_is_the_longest_line_only_the_other_run_shows() -> None:
    reference = ["Meridian Credit Union", "Member Profile", "Dolores Chen", "Accounts"]
    outcome = [
        "Meridian Credit Union",
        "Member Inquiry",
        "No member record found for ID 99999.",
        "Return to search",
    ]
    # Not "Member Inquiry", which is also new but shorter: a screen announcing a
    # different result says so in a sentence, and the chrome it shares with every
    # other screen is short.
    assert distinguishing_text(reference, outcome) == "No member record found for ID 99999."


def test_shared_chrome_never_becomes_a_detector() -> None:
    # The failure this whole mechanism exists to prevent: a phrase that is on the
    # happy path cannot distinguish anything.
    reference = ["Accounts", "Current Balance", "Member Profile"]
    outcome = ["Accounts", "Current Balance", "You do not have permission to view this record."]
    assert distinguishing_text(reference, outcome).startswith("You do not have permission")


def test_two_runs_that_ended_the_same_way_teach_nothing() -> None:
    with pytest.raises(NothingToLearn):
        distinguishing_text(["Member Profile"], ["member  profile"])


def test_final_lines_reads_the_last_observation_of_a_run(tmp_path: Path) -> None:
    observations = tmp_path / "observations"
    observations.mkdir(parents=True)
    for step, text in ((1, "first screen"), (2, "second screen"), (10, "last screen")):
        (observations / f"step-{step:02d}.json").write_text(
            json.dumps({"elements": [{"text": text}]})
        )
    # step-10, not step-02: sorted numerically, not lexically.
    assert final_lines(tmp_path) == ["last screen"]
    # And the reference side of a comparison is everywhere the run went, because
    # a phrase both runs passed through says nothing about how either ended.
    assert all_lines(tmp_path) == ["first screen", "second screen", "last screen"]


# ---------------------------------------------------------------------------
# what it produces
# ---------------------------------------------------------------------------


def test_the_learned_detector_describes_the_capability_not_the_run() -> None:
    learned = with_outcome(
        capability(),
        name="member_not_found",
        description="no member exists with that id",
        detector_text="No member record found for ID 99999.",
        inputs={"member_id": "99999"},
    )
    outcome = learned.business_outcomes[-1]

    # The run's own value is substituted back out. Recording 99999 would give a
    # capability that only detects one member's absence.
    assert outcome.detector.value == "No member record found for ID {{member_id}}."
    # And the caller is told which parameter the answer is about.
    assert outcome.result_fields == {"member_id": ValueType.STRING}


def test_learning_emits_a_new_draft_version(tmp_path: Path) -> None:
    store = Catalog(tmp_path)
    store.save(capability())

    learned = with_outcome(
        capability(),
        name="member_not_found",
        description="",
        detector_text="No member record found",
        inputs={"member_id": "99999"},
        version=2,
    )
    store.save(learned)

    # v1 stays exactly as production has it; the new claim arrives as a draft for
    # review.
    assert store.load("cap_read_member", 1).status is Status.APPROVED
    assert store.load("cap_read_member", 2).status is Status.DRAFT
    assert list(store.versions("cap_read_member")) == [1, 2]


async def test_a_learned_outcome_replays_as_an_outcome(tmp_path: Path) -> None:
    """The point of the exercise: the next invocation gets an answer, not a crash."""
    learned = with_outcome(
        capability(),
        name="member_not_found",
        description="no member exists with that id",
        detector_text="No member record found for ID 99999.",
        inputs={"member_id": "99999"},
    )

    perceiver = FakePerceiver(
        [frame("start"), frame("Member Inquiry", "No member record found for ID 77777.")]
    )
    engine = ReplayEngine(
        perceiver=perceiver,
        driver=FakeDriver(perceiver),
        resolver=Resolver(allow_vlm=False),
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "replay-learned", Redactor()),
        control=RunControl(run_id="replay-learned"),
        settle_timeout_ms=100,
        settle_poll_ms=1,
    )
    result = await engine.replay(learned, {"member_id": "77777"})

    # A different member than the one it was taught with: the detector was
    # parameterized, so it recognises the shape rather than the instance.
    assert result.status is RunStatus.BUSINESS_OUTCOME
    assert result.outcome is not None
    assert result.outcome.name == "member_not_found"
    assert result.outcome.fields == {"member_id": "77777"}


# ---------------------------------------------------------------------------
# refuting what the model proposed
# ---------------------------------------------------------------------------


class _State:
    """Just enough of a discovery run for `declare` to work over."""

    def __init__(self, screens: list[Any]) -> None:
        self.goal = "read a balance"
        self.history = ["1. open the profile"]
        self.observations = screens
        self.success_text = "Member Profile"
        self.declaration: dict[str, Any] = {}


async def test_a_detector_visible_on_the_successful_run_is_rejected() -> None:
    llm = ScriptedLLM(
        [],
        declaration={
            "description": "reads a balance",
            "success_text": "Member Profile",
            "business_outcomes": [
                # Exactly what the real model proposed. "Accounts" is a column
                # header on the screen the run succeeded on.
                {
                    "name": "account_not_found",
                    "description": "the nickname matches no account",
                    "detector_text": "Accounts",
                },
                {
                    "name": "member_not_found",
                    "description": "no member with that id",
                    "detector_text": "No member record found",
                },
            ],
        },
    )
    state = _State([frame("Member Profile", "Accounts", "Primary Savings")])

    declared = await declare(state, llm, {"member_id": "12345"})

    assert [o["name"] for o in declared["business_outcomes"]] == ["member_not_found"]
    rejected = declared["business_outcomes_rejected"]
    assert [o["name"] for o in rejected] == ["account_not_found"]
    # The reason travels with it: the synthesis note is what a reviewer reads
    # before approving, and a silent drop teaches them nothing.
    assert "successful run" in rejected[0]["rejected_because"]
