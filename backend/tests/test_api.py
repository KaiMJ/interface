"""The control plane.

Three callers, and the tests follow them: an agent reading the catalog and
invoking a capability, an operator working the intervention queue, and the
console watching a run.

The browser is not involved. `Runtime` holds the session pool as a collaborator
precisely so a test can hand it something else — here, a pool that returns a
session made of the same fakes the engine tests use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cua.api.main import Runtime, create_app
from cua.catalog import Catalog
from cua.escalation import ControlRegistry
from cua.policy import Redactor
from cua.schema import (
    ActStep,
    AppRef,
    Capability,
    CheckKind,
    Checkpoint,
    Controller,
    InputSpec,
    InterventionReason,
    InterventionRequest,
    OutputSpec,
    Primitive,
    Status,
    ValueType,
)


def capability(status: Status = Status.DRAFT) -> Capability:
    return Capability(
        id="cap_get_savings_balance",
        status=status,
        goal="read a member's savings balance",
        description="Returns the current balance of a named account.",
        app=AppRef(name="targetapp", base_url_pattern="^http://targetapp:8080(/.*)?$"),
        inputs=[
            InputSpec(
                name="member_id",
                type=ValueType.STRING,
                description="the member's id",
                example="12345",
            )
        ],
        outputs=[OutputSpec(name="balance", type=ValueType.NUMBER, from_step=2)],
        steps=[
            ActStep(
                id=1,
                action=Primitive.NAVIGATE,
                value="http://targetapp:8080/members/{{member_id}}",
            ),
            ActStep(id=2, action=Primitive.EXTRACT, extract_as="balance"),
        ],
        success=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Member Profile"),
    )


@pytest.fixture
def client(tmp_path: Path) -> Any:
    app = create_app()
    app.state.runtime = Runtime(
        settings=app.state.settings.model_copy(
            update={"artifacts_dir": tmp_path / "artifacts", "evidence_dir": tmp_path / "evidence"}
        ),
        catalog=Catalog(tmp_path / "artifacts"),
        registry=ControlRegistry(),
        redactor=Redactor(),
        pool=None,
    )
    return TestClient(app)


def test_health_answers_before_a_browser_exists(client: Any) -> None:
    # The container's health must not depend on Xvfb having come up, or a slow
    # display start turns into a restart loop.
    body = client.get("/health").json()
    assert body["status"] == "ok"
    # And it says whether the one display is free. Every client that polls health
    # wants that: with a single session, "are you alive" and "can I start a run"
    # are the same question.
    assert body["active_run"] is None


def test_the_catalog_lists_what_an_agent_can_call(client: Any) -> None:
    client.app.state.runtime.catalog.save(capability())
    body = client.get("/capabilities").json()

    assert [c["ref"] for c in body] == ["cap_get_savings_balance@v1"]
    assert body[0]["inputs"] == {"member_id": "string"}
    assert body[0]["outputs"] == {"balance": "number"}


def test_the_tool_manifest_only_offers_approved_capabilities(client: Any) -> None:
    catalog = client.app.state.runtime.catalog
    catalog.save(capability())

    # A draft is a proposal nobody has vouched for. An agent that could call one
    # would be running unreviewed automation against member accounts.
    assert client.get("/capabilities/manifest").json() == []

    client.post(
        "/capabilities/cap_get_savings_balance/approve",
        params={"version": 1, "operator": "reviewer"},
    )
    manifest = client.get("/capabilities/manifest").json()

    assert manifest[0]["name"] == "cap_get_savings_balance"
    assert manifest[0]["parameters"]["required"] == ["member_id"]
    assert manifest[0]["parameters"]["properties"]["member_id"]["type"] == "string"


def test_an_agent_cannot_invoke_a_draft(client: Any) -> None:
    """The approval gate, on the one path where it is load-bearing.

    Absent from the manifest is not the same as unreachable: a caller that knows
    the id can name it. `/invoke` is the agent-facing path — nobody is watching —
    so it refuses anything a human has not signed off, and the engine refuses it
    again for any call site added later. The console and the CLI deliberately do
    not: replaying a draft is how it gets reviewed.
    """
    catalog = client.app.state.runtime.catalog
    catalog.save(capability())

    denied = client.post(
        "/capabilities/cap_get_savings_balance/invoke", json={"inputs": {"member_id": "12345"}}
    )

    assert denied.status_code == 403
    assert "needs approval" in denied.json()["detail"]
    # And nothing was started: no run directory, no session taken.
    assert client.get("/runs").json() == []


def test_an_unknown_capability_is_a_404_not_a_crash(client: Any) -> None:
    assert client.get("/capabilities/nope").status_code == 404
    assert client.post("/capabilities/nope/invoke", json={"inputs": {}}).status_code == 404


# ---------------------------------------------------------------------------
# the operator's queue
# ---------------------------------------------------------------------------


def _park_a_run(client: Any) -> Any:
    """Raise an intervention the way the engine does, and leave it open.

    `park` rather than `escalate`: the state an operator sees is identical, and
    nothing here needs a coroutine suspended on the resume event.
    """
    registry = client.app.state.runtime.registry
    control = registry.create("run-1")
    request = InterventionRequest(
        id="int_abc123",
        run_id="run-1",
        mode="replay",
        capability="cap_submit_transfer@v1",
        goal="transfer funds",
        reason=InterventionReason.RISKY_ACTION_CONFIRMATION,
        step_id=4,
        step_intent="submit the transfer",
        message="risky action needs confirmation",
        vnc_url="http://localhost:6080/vnc.html",
    )

    control.park(request)
    return control


def test_a_pending_intervention_carries_enough_context_to_act_on(client: Any) -> None:
    control = _park_a_run(client)

    queue = client.get("/interventions").json()
    assert [i["id"] for i in queue] == ["int_abc123"]
    # Enough to act without reading logs: which capability, which step, why, and
    # where to connect.
    assert queue[0]["step_intent"] == "submit the transfer"
    assert queue[0]["reason"] == "risky_action_confirmation"
    assert queue[0]["vnc_url"]
    # Nobody holds control between the automation stopping and an operator
    # arriving. That interval is what makes a race impossible rather than
    # unlikely.
    assert control.holder is Controller.NOBODY


def test_taking_control_twice_is_refused(client: Any) -> None:
    _park_a_run(client)
    first = client.post("/interventions/int_abc123/take", params={"operator": "op-1"})
    second = client.post("/interventions/int_abc123/take", params={"operator": "op-2"})

    assert first.status_code == 200
    assert first.json()["holder"] == "human"
    assert second.status_code == 409


def test_resolving_hands_control_back_and_records_the_handoff(client: Any) -> None:
    control = _park_a_run(client)
    client.post("/interventions/int_abc123/take", params={"operator": "op-1"})

    body = client.post(
        "/interventions/int_abc123/resolve",
        json={"outcome": "resume", "operator": "op-1", "note": "confirmed with the member"},
    ).json()

    assert body["outcome"] == "resume"
    assert control.holder is Controller.AUTOMATION
    # The run's evidence gains the request and how it was resolved, whether or not
    # anything was captured at the X layer on this machine.
    run = Path(client.app.state.runtime.settings.evidence_dir) / "run-1" / "intervention"
    assert (run / "request.json").exists()
    assert (run / "resolution.json").exists()


def test_an_unknown_intervention_is_a_404(client: Any) -> None:
    assert client.get("/interventions/int_nope").status_code == 404
    assert (
        client.post("/interventions/int_nope/take", params={"operator": "op"}).status_code == 404
    )


# ---------------------------------------------------------------------------
# what the console needs: starting runs, filtering them, reading them together
# ---------------------------------------------------------------------------


def _finished_run(client: Any, run_id: str, **overrides: Any) -> None:
    """Write a run to the evidence directory the way a finished run leaves it."""
    import json

    root = Path(client.app.state.runtime.settings.evidence_dir) / run_id
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "capability": "cap_get_savings_balance@v1",
        "app": "targetapp",
        "status": "success",
        "duration_ms": 20000,
        "started_at": f"2026-08-16T00:00:0{len(run_id) % 10}+00:00",
        "steps": [
            {
                "step_id": 1, "status": "ok", "resolution": "anchor_text",
                "settled_by": "pixels", "duration_ms": 1000,
                "phases": {"observe_ms": 900, "observations": 1},
            },
            {
                "step_id": 2, "status": "ok", "resolution": "recorded_bbox",
                "settled_by": "pixels", "duration_ms": 1000,
                "phases": {"observe_ms": 900, "observations": 1},
            },
        ],
    }
    payload.update(overrides)
    (root / "run.json").write_text(json.dumps(payload))


def test_runs_can_be_filtered_by_the_capability_they_ran(client: Any) -> None:
    _finished_run(client, "replay-aaa")
    _finished_run(client, "replay-bbb", capability="cap_transfer_funds@v1")

    everything = client.get("/runs").json()
    filtered = client.get("/runs", params={"capability": "cap_get_savings_balance"}).json()

    assert len(everything) == 2
    # "show me every run of the flow I am about to approve" is the question, and
    # answering it used to mean matching strings by eye.
    assert [r["run_id"] for r in filtered] == ["replay-aaa"]


def test_capability_history_aggregates_the_drift_signal(client: Any) -> None:
    client.app.state.runtime.catalog.save(capability())
    _finished_run(client, "replay-aaa")
    _finished_run(client, "replay-bbb", status="business_outcome")

    body = client.get("/capabilities/cap_get_savings_balance/history").json()

    assert body["versions"] == [1]
    assert len(body["runs"]) == 2
    agg = body["aggregate"]
    assert agg["statuses"] == {"success": 1, "business_outcome": 1}
    # One run resolving by the recorded box is noise; the share across runs is the
    # early warning that the application moved. It only exists in aggregate.
    assert agg["resolution_tiers"] == {"anchor_text": 2, "recorded_bbox": 2}
    assert agg["drift_share"] == 0.5
    # A business outcome is a correct answer and deliberately does not count as a
    # failure against the flow.
    assert agg["success_rate"] == 0.5
    # Perception dominates everything else by two orders of magnitude, so it is the
    # only number worth aggregating. `observations_per_step` shows the frame reuse
    # working: one perception per step, not two.
    assert agg["observe_share"] == 0.9
    assert agg["observations_per_step"] == 1.0


def test_a_second_run_is_refused_while_one_holds_the_session(client: Any) -> None:
    rt = client.app.state.runtime
    rt.begin("replay-already-running")

    refused = client.post("/runs/discover", json={"goal": "do a thing"})

    # 409, not a queue. There is one browser on one display, and a second run
    # would not wait politely — it would drive the same pixels.
    assert refused.status_code == 409
    assert "replay-already-running" in refused.json()["detail"]


def test_a_run_is_visible_the_moment_it_is_accepted(client: Any) -> None:
    # Its first seconds go on starting a browser and signing in, during which it
    # has written no evidence. A console that answers 404 for a run the operator
    # just started is a console they stop believing.
    rt = client.app.state.runtime
    rt.begin("discover-pending")
    rt.pending["discover-pending"] = {
        "run_id": "discover-pending",
        "status": "running",
        "kind": "discovery",
        "goal": "read a balance",
        "steps": [],
        "started_at": "2026-08-16T00:00:00+00:00",
    }

    assert client.get("/runs/discover-pending").json()["status"] == "running"
    assert [r["run_id"] for r in client.get("/runs").json()] == ["discover-pending"]


def test_starting_a_replay_of_an_unknown_capability_is_a_404(client: Any) -> None:
    assert (
        client.post("/runs/replay", json={"capability_id": "nope", "inputs": {}}).status_code
        == 404
    )
    # …and the refusal does not leave the session claimed behind it.
    assert client.app.state.runtime.active_run is None


def test_the_queue_can_include_what_was_already_dealt_with(client: Any) -> None:
    _park_a_run(client)
    client.post("/interventions/int_abc123/take", params={"operator": "op-1"})
    client.post(
        "/interventions/int_abc123/resolve",
        json={"outcome": "resume", "operator": "op-1", "note": "done"},
    )

    assert client.get("/interventions").json() == []
    history = client.get("/interventions", params={"include_resolved": True}).json()
    assert [i["state"] for i in history] == ["resolved"]


def test_evidence_carries_the_handoff_record_not_only_its_frames(client: Any) -> None:
    _park_a_run(client)
    client.post("/interventions/int_abc123/take", params={"operator": "op-1"})
    client.post(
        "/interventions/int_abc123/resolve",
        json={"outcome": "resume", "operator": "op-1", "note": "confirmed with the member"},
    )

    body = client.get("/runs/run-1/evidence").json()

    # What the operator was told and what they decided is the record of a human
    # touching regulated data. It was on disk with nothing reading it.
    assert body["intervention"]["request"]["reason"] == "risky_action_confirmation"
    assert body["intervention"]["resolution"]["note"] == "confirmed with the member"
