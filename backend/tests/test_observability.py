"""What a run leaves behind for a human.

The system already made every decision these tests assert on — which tier found
the target, what policy said, what the model was shown. It just used to throw
them away once they had been acted on, which meant the console could show that a
step happened and never why. These are the records that close that gap, so they
are tested like a contract rather than like logging: something reads them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cua.policy import Policy
from cua.resolve import Resolver
from cua.resolve.resolver import Unresolvable
from cua.schema import (
    Bbox,
    Element,
    ElementSource,
    Observation,
    Primitive,
    Relation,
    ResolutionTier,
    Risk,
    Target,
    Viewport,
)

POLICY = Policy.load(Path(__file__).resolve().parents[2] / "policies" / "targetapp.yaml")
VIEWPORT = Viewport(width=1440, height=900)


def el(id_: str, x: float, y: float, text: str, role: str = "text") -> Element:
    return Element(
        id=id_,
        role=role,
        name=text,
        text=text,
        bbox=Bbox(x=x, y=y, w=0.1, h=0.02),
        source=ElementSource.OCR,
        conf=0.95,
    )


def frame(*elements: Element) -> Observation:
    return Observation(
        screenshot_path="/dev/null",
        viewport=VIEWPORT,
        elements=elements,
        url="http://targetapp:8080/members/12345",
        frame_hash="h1",
        taken_at="2026-08-16T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# the guardrail's verdict, allow as well as deny
# ---------------------------------------------------------------------------


def test_an_allowed_action_is_recorded_not_only_a_denied_one() -> None:
    # The failure this prevents: evidence in which "this transfer was permitted"
    # is represented by the absence of an entry.
    decision = POLICY.decide(Primitive.CLICK, Risk.SAFE, "click the View button")

    assert decision.disposition == "allow"
    assert decision.effective_risk == "safe"
    assert decision.promoted_from is None
    assert decision.intent == "click the View button"


def test_a_promotion_names_the_pattern_that_caused_it() -> None:
    # Policy may raise a step the recording declared safe. Reporting the promotion
    # without its cause reads as the system being arbitrary.
    decision = POLICY.decide(Primitive.CLICK, Risk.SAFE, "click Confirm Transfer")

    assert decision.disposition == "confirm"
    assert decision.declared_risk == "safe"
    assert decision.effective_risk == "risky"
    assert decision.promoted_from == "safe"
    assert "submit|confirm|transfer" in (decision.detail or "")


def test_an_unlisted_primitive_is_denied_with_the_rule_that_denied_it() -> None:
    narrowed = Policy(
        allowed_url_patterns=("^http://targetapp:8080(/.*)?$",),
        allowed_actions=frozenset({Primitive.CLICK}),
        risky_disposition="confirm",
        recoveries=(),
        redact_patterns=(),
    )
    decision = narrowed.decide(Primitive.TYPE, Risk.SAFE, "type into the amount field")

    assert decision.disposition == "denied"
    assert decision.rule == "allowlist"
    assert "'type'" in (decision.detail or "")


def test_check_action_still_raises_so_the_enforcement_path_is_unchanged() -> None:
    from cua.policy import PolicyDenied

    with pytest.raises(PolicyDenied):
        Policy(
            allowed_url_patterns=(),
            allowed_actions=frozenset(),
            risky_disposition="block",
            recoveries=(),
            redact_patterns=(),
        ).check_action(Primitive.CLICK, Risk.SAFE, "click anything")


# ---------------------------------------------------------------------------
# the resolver ladder, rung by rung
# ---------------------------------------------------------------------------


def test_the_winning_tier_records_what_it_matched() -> None:
    obs = frame(el("e0", 0.1, 0.1, "Search"))
    target = Target(intent="i", target_desc="Search", anchor_text="Search")
    _, trace = Resolver().resolve_traced(target, obs)

    assert trace.tier is ResolutionTier.ANCHOR_TEXT
    assert [(a.tier.value, a.outcome) for a in trace.attempts] == [("anchor_text", "matched")]
    assert trace.attempts[0].matched_text == "Search"
    assert trace.drift is False


def test_a_fallthrough_says_why_each_rung_missed() -> None:
    # The question an operator actually has when a step resolves by the recorded
    # box is *why did the anchor miss* — and "recorded_bbox" alone cannot answer
    # it. A miss because the text is gone and a miss because it matched three
    # elements are different applications and different fixes.
    obs = frame(el("e0", 0.1, 0.1, "Something else"))
    target = Target(
        intent="i",
        target_desc="the View button",
        anchor_text="View",
        role="button",
        name="View",
        bbox=Bbox(x=0.5, y=0.5, w=0.05, h=0.02),
    )
    _, trace = Resolver().resolve_traced(target, obs)

    assert trace.tier is ResolutionTier.RECORDED_BBOX
    assert trace.drift is True
    outcomes = {a.tier.value: a for a in trace.attempts}
    assert outcomes["anchor_text"].outcome == "miss"
    assert "'view'" in (outcomes["anchor_text"].detail or "").lower()
    assert outcomes["role_name"].outcome == "miss"


def test_ambiguity_is_reported_as_a_count_not_hidden() -> None:
    obs = frame(el("e0", 0.1, 0.1, "View"), el("e1", 0.1, 0.3, "View"))
    _, trace = Resolver().resolve_traced(
        Target(intent="i", target_desc="a View button", anchor_text="View"), obs
    )

    assert trace.candidates == 2
    assert trace.attempts[0].candidates == 2


def test_a_relation_that_lands_nowhere_says_so() -> None:
    # Typing into a label because the field beside it could not be found is the
    # class of silent wrong action this system exists to prevent, so the miss has
    # to be legible rather than merely correct.
    obs = frame(el("e0", 0.8, 0.1, "User ID"))
    target = Target(
        intent="i",
        target_desc="the field beside 'User ID'",
        anchor_text="User ID",
        relation=Relation.RIGHT_OF,
    )
    with pytest.raises(Unresolvable):
        Resolver().resolve_traced(target, obs)


# ---------------------------------------------------------------------------
# both of the above, on a real step record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replayed_step_carries_its_policy_and_its_ladder(tmp_path: Path) -> None:
    from test_replay import ACCOUNTS_ROW, INPUTS, build, savings_capability
    from test_replay import frame as replay_frame

    engine, _driver, _control = build(
        tmp_path, [replay_frame("Sign-On placeholder"), ACCOUNTS_ROW]
    )
    result = await engine.replay(savings_capability(), INPUTS)

    extraction = next(s for s in result.steps if s.resolution_trace is not None)
    assert extraction.policy is not None
    assert extraction.policy.disposition == "allow"
    assert extraction.resolution_trace is not None
    assert extraction.resolution_trace.attempts
    # And the record survives serialization, because the console reads the file
    # rather than the object.
    written = (tmp_path / "run-test" / "steps.jsonl").read_text()
    assert '"policy"' in written and '"resolution_trace"' in written


# ---------------------------------------------------------------------------
# the model's turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_tool_call_is_still_a_step_in_the_log(tmp_path: Path) -> None:
    """A model that tried something impossible left no trace at all.

    The step log a human reads was a filtered view of what happened, filtered by
    exactly the thing being debugged.
    """
    from fakes import FakeDriver, FakePerceiver
    from fakes import frame as fake_frame

    from cua.discovery.llm import ToolCall
    from cua.discovery.loop import DiscoveryLoop
    from cua.evidence import EvidenceWriter
    from cua.policy import Redactor

    screen = fake_frame("Member Services", "Search")
    perceiver = FakePerceiver([screen, screen, screen, screen])
    driver = FakeDriver(perceiver)

    loop = DiscoveryLoop(
        perceiver=perceiver,
        driver=driver,
        policy=POLICY,
        evidence=EvidenceWriter(tmp_path, "discover-test", Redactor()),
        llm=_ScriptedLLM(
            [
                # A mark that is not on this screen: rejected before anything runs.
                ToolCall(name="click", input={"mark": 999, "intent": "click nothing"}),
                ToolCall(name="escalate", input={"reason": "I cannot find it"}),
            ]
        ),
        max_steps=6,
        settle_timeout_ms=10,
        settle_poll_ms=1,
    )
    result = await loop.run("do a thing", "http://targetapp:8080", {})

    turns = [s for s in result.steps if s.model_turn is not None]
    assert [t.model_turn.verdict for t in turns if t.model_turn] == ["rejected", "escalated"]
    rejected = turns[0]
    assert rejected.model_turn is not None
    assert rejected.model_turn.mark == 999
    assert "not on this screen" in (rejected.model_turn.detail or "")
    # The entry navigation is step 1 of the artifact, so it is step 1 of the log —
    # and it carries the frame the run started on, not an empty record that
    # renders as a black panel.
    assert result.steps[0].step_id == 1
    assert result.steps[0].policy is not None
    assert result.steps[0].evidence.screenshot

    # A run in progress is watchable: the file the console tails is rewritten
    # after every step rather than only when the run ends.
    import json

    written = json.loads((tmp_path / "discover-test" / "run.json").read_text())
    assert written["steps"], "run.json ended up with no steps"

    # The model's own words and its raw answer, not just the loop's gloss on them.
    first = next(s for s in result.steps if s.model_turn)
    assert first.model_turn is not None
    assert first.model_turn.arguments == {"mark": 999, "intent": "click nothing"}


class _ScriptedLLM:
    def __init__(self, calls: list[Any]) -> None:
        self.calls_out = list(calls)
        self.calls = 0
        self.model = "test/scripted"

    def preflight(self) -> None:
        return None

    async def decide(self, **_: Any) -> Any:
        self.calls += 1
        return self.calls_out.pop(0) if self.calls_out else _finish()


def _finish() -> Any:
    from cua.discovery.llm import ToolCall

    return ToolCall(name="escalate", input={"reason": "out of script"})
