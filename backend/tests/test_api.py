"""The control plane.

Three callers, and the tests follow them: an agent reading the catalog and
invoking a capability, an operator working the intervention queue, and the
console watching a run.

The browser is not involved. `Runtime` holds the session pool as a collaborator
precisely so a test can hand it something else — here, a pool that returns a
session made of the same fakes the engine tests use.
"""

from __future__ import annotations

import asyncio
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
        outputs=[OutputSpec(name="balance", type=ValueType.NUMBER, from_step=1)],
        steps=[
            ActStep(
                id=1,
                action=Primitive.NAVIGATE,
                value="http://targetapp:8080/members/{{member_id}}",
            )
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
    assert client.get("/health").json() == {"status": "ok"}


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


def test_an_unknown_capability_is_a_404_not_a_crash(client: Any) -> None:
    assert client.get("/capabilities/nope").status_code == 404
    assert client.post("/capabilities/nope/invoke", json={"inputs": {}}).status_code == 404


# ---------------------------------------------------------------------------
# the operator's queue
# ---------------------------------------------------------------------------


def _park_a_run(client: Any) -> tuple[Any, asyncio.Task[Any]]:
    """Raise an intervention the way the engine does, and leave it open."""
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

    async def park() -> Any:
        return await control.escalate(request)

    loop = asyncio.new_event_loop()
    task = loop.create_task(park())
    loop.run_until_complete(asyncio.sleep(0))  # let it register and yield
    return control, task


def test_a_pending_intervention_carries_enough_context_to_act_on(client: Any) -> None:
    control, _ = _park_a_run(client)

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
    control, _ = _park_a_run(client)
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
