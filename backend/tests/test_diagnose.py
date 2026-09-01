"""`cua diagnose`: turning one unforeseen screen into a declaration.

Three things make pointing a model at this safe, and all three are asserted here: it chooses a
line rather than writing one, a line that also appears on a successful run is refused, and a
condition met on a step that mutates is never proposed as auto-recoverable.

No browser, no display, no application: `diagnose` reads a finished run's evidence and calls
one model, so a scripted model and a directory of JSON is the whole fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cua.diagnose import RunEvidence, diagnose, load_run, prompt_for, reference_lines
from cua.policy import Policy

POLICY = Policy.load(Path(__file__).resolve().parents[2] / "policies" / "targetapp.yaml")

DORMANT = "This account is dormant and cannot be viewed."


class ScriptedModel:
    """Returns one prepared answer and remembers what it was asked."""

    model = "test/scripted"

    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.prompt = ""

    async def structured(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.prompt = prompt
        return self.answer


def failed_run(screen: list[str], risk: str = "safe") -> RunEvidence:
    return RunEvidence(
        run_id="replay-test",
        status="failure",
        capability="cap_get_savings_balance@v1",
        failure={
            "kind": "checkpoint_failed",
            "step_id": 2,
            "message": "checkpoint did not hold",
            "expected": "text_present 'Available Balance'",
            "observed": DORMANT,
        },
        screen=screen,
        step_risk=risk,
    )


async def test_a_screen_nobody_declared_becomes_a_proposal() -> None:
    run = failed_run(["Member Profile", "29455", DORMANT])
    model = ScriptedModel(
        {
            "classification": "business_outcome",
            "line": 2,
            "name": "account_dormant",
            "description": "the account exists but is dormant",
            "rationale": "the screen names a condition rather than failing",
        }
    )

    result = await diagnose(run, POLICY, model, reference=["Member Profile", "29455"])

    assert result.actionable
    assert result.classification == "business_outcome"
    # Copied off the screen, character for character.
    assert result.detector == DORMANT
    assert result.target == "policy"
    assert "business_outcomes:" in result.patch
    assert json.dumps(DORMANT) in result.patch


async def test_a_detector_that_is_also_on_a_successful_run_is_refused() -> None:
    """The falsification rule: a line both runs read identifies nothing.

    A detector on "Member Profile" makes every successful replay report itself as a dormant
    account, and the patch looks reasonable in a diff.
    """
    run = failed_run(["Member Profile", "29455", DORMANT])
    model = ScriptedModel(
        {
            "classification": "business_outcome",
            "line": 0,                      # the heading every screen carries
            "name": "account_dormant",
            "description": "",
            "rationale": "it was at the top",
        }
    )

    result = await diagnose(run, POLICY, model, reference=["Member Profile", "29455"])

    assert not result.actionable
    assert result.rejected is not None
    assert "identifies nothing" in result.rejected
    assert result.patch == ""


async def test_a_line_that_was_not_offered_is_refused() -> None:
    """The model returns an index into the lines it was shown, so a detector it
    invented is not expressible. An out-of-range index is the only way that rule
    can be broken, and it is refused rather than clamped."""
    run = failed_run(["Member Profile", DORMANT])
    model = ScriptedModel(
        {
            "classification": "app_error",
            "line": 7,
            "name": "made_up",
            "description": "",
            "rationale": "",
        }
    )

    result = await diagnose(run, POLICY, model, reference=[])

    assert not result.actionable
    assert "not one of the lines offered" in (result.rejected or "")


async def test_a_condition_on_a_step_that_mutates_is_never_auto_recoverable() -> None:
    """Not the model's call.

    `recoverable` means carrying on unattended past a step that may already have moved money,
    so the classification is downgraded whatever the model concluded, and the downgrade is
    recorded in the rationale rather than applied silently.
    """
    run = failed_run(["Confirm Transfer", "Please re-enter your authorization code"], risk="risky")
    model = ScriptedModel(
        {
            "classification": "recoverable",
            "line": 1,
            "name": "authorization_prompt",
            "description": "asks for a code",
            "rationale": "looks like an interstitial",
        }
    )

    result = await diagnose(run, POLICY, model, reference=[])

    assert result.classification == "escalation"
    assert "downgraded" in result.rationale
    assert "escalations:" in result.patch
    assert "actions:" not in result.patch


async def test_drift_proposes_nothing_at_all() -> None:
    """Some failures are not conditions to declare, so the taxonomy has two members that
    deliberately produce no patch."""
    run = failed_run(["Member Profile", "29455"])
    model = ScriptedModel(
        {
            "classification": "drift",
            "line": -1,
            "name": "",
            "description": "",
            "rationale": "the target moved; this needs re-recording",
        }
    )

    result = await diagnose(run, POLICY, model, reference=[])

    assert not result.actionable
    assert result.target is None
    assert result.patch == ""


async def test_the_model_is_shown_what_the_application_already_handles() -> None:
    """A duplicate detector is a second thing to keep in sync with the first."""
    run = failed_run(["Member Profile", DORMANT])
    model = ScriptedModel(
        {"classification": "drift", "line": -1, "name": "", "description": "", "rationale": ""}
    )

    await diagnose(run, POLICY, model, reference=[])

    assert "maintenance_notice" in model.prompt
    assert "member_not_found" in model.prompt
    # And the lines it may choose from, numbered.
    assert f"1: {DORMANT}" in model.prompt


def test_reference_lines_reads_only_successful_runs_of_the_same_capability(
    tmp_path: Path,
) -> None:
    """Broad on purpose: a line has to be absent from every successful run to
    count as identifying. `catalog.learn` records what happens when this set is
    too narrow — the search page's hint text becomes "member not found"."""
    _write_run(tmp_path / "replay-good", "success", "cap_a@v1", ["Shared chrome", "A balance"])
    _write_run(tmp_path / "replay-other", "success", "cap_b@v1", ["Something else"])
    _write_run(tmp_path / "replay-bad", "failure", "cap_a@v1", ["A dormant notice"])

    lines = reference_lines(tmp_path, "cap_a@v1", exclude="replay-bad")

    assert "Shared chrome" in lines
    assert "Something else" not in lines      # a different capability
    assert "A dormant notice" not in lines    # not a successful run


def test_a_finished_run_is_read_off_disk_including_the_step_that_failed(
    tmp_path: Path,
) -> None:
    """Everything `diagnose` needs is already written by the run itself. Nothing
    here re-derives anything, which is what makes it work on last week's run."""
    root = _write_run(
        tmp_path / "replay-x",
        "failure",
        "cap_a@v1",
        [DORMANT],
        failure={"kind": "checkpoint_failed", "step_id": 3},
        steps=[{"step_id": 3, "policy": {"effective_risk": "risky"}}],
    )

    run = load_run(root)

    assert run.status == "failure"
    assert run.capability == "cap_a@v1"
    assert run.step_risk == "risky"
    assert run.screen == [DORMANT]
    assert "Step: 3" in prompt_for(run, POLICY)


def _write_run(
    root: Path,
    status: str,
    capability: str,
    lines: list[str],
    failure: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> Path:
    (root / "observations").mkdir(parents=True)
    (root / "run.json").write_text(
        json.dumps(
            {
                "run_id": root.name,
                "status": status,
                "capability": capability,
                "failure": failure,
                "steps": steps or [],
            }
        )
    )
    (root / "observations" / "step-01.json").write_text(
        json.dumps({"elements": [{"text": line} for line in lines]})
    )
    return root
