"""HTTP surface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ..schema import DiscoveryResult, ReplayResult

router = APIRouter()


class DiscoverRequest(BaseModel):
    goal: str
    start_url: str
    # Concrete values for this run. Also the parameter declaration: any recorded
    # literal matching one of these becomes a placeholder at synthesis time, which
    # is how "who decided 12345 was a parameter?" gets a deterministic answer.
    inputs: dict[str, Any] = {}
    capability_id: str | None = None


class InvokeRequest(BaseModel):
    inputs: dict[str, Any] = {}
    version: int | None = None


class ResolveRequest(BaseModel):
    outcome: str            # "resume" | "abort"
    operator: str = "unknown"
    note: str = ""


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --- capabilities ----------------------------------------------------------


@router.get("/capabilities")
async def list_capabilities() -> list[dict[str, Any]]:
    """The catalog. Doubles as the agent-facing tool manifest."""
    raise NotImplementedError


@router.get("/capabilities/{capability_id}")
async def get_capability(capability_id: str, version: int | None = None) -> dict[str, Any]:
    raise NotImplementedError


@router.post("/capabilities/{capability_id}/invoke")
async def invoke(capability_id: str, req: InvokeRequest) -> ReplayResult:
    """Deterministic replay. The production path an AI agent calls.

    Returns 200 for all three legitimate results — success, business outcome, and
    escalation — with the distinction carried in `status`. A business outcome is
    not an HTTP error: "no such member" is an answer, and encoding it as a 404
    would push the caller into inspecting status codes to tell "you asked wrongly"
    apart from "the record does not exist".

    Hard failures return 500 with a populated `failure` block.
    """
    raise NotImplementedError


@router.post("/capabilities/{capability_id}/approve")
async def approve(capability_id: str, version: int, operator: str) -> dict[str, Any]:
    """draft -> approved. Gate on unattended replay."""
    raise NotImplementedError


# --- discovery -------------------------------------------------------------


@router.post("/discover")
async def discover(req: DiscoverRequest) -> DiscoveryResult:
    """Run the LLM-driven loop and emit a draft capability."""
    raise NotImplementedError


# --- runs ------------------------------------------------------------------


@router.get("/runs")
async def list_runs() -> list[dict[str, Any]]:
    raise NotImplementedError


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    raise NotImplementedError


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> Any:
    """SSE stream of step events. What the console tails."""
    raise NotImplementedError


# --- interventions ---------------------------------------------------------


@router.get("/interventions")
async def list_interventions() -> list[dict[str, Any]]:
    """Pending escalations. The operator's queue."""
    raise NotImplementedError


@router.get("/interventions/{intervention_id}")
async def get_intervention(intervention_id: str) -> dict[str, Any]:
    """Full context: capability, goal, step, reason, screenshot, VNC URL."""
    raise NotImplementedError


@router.post("/interventions/{intervention_id}/take")
async def take_control(intervention_id: str, operator: str) -> dict[str, Any]:
    """Operator claims the live session.

    Flips the control token to HUMAN and starts the X-layer action watcher. Until
    this is called the automation has stopped but nobody holds control — which is
    the state that makes "the agent clicked while I was typing" impossible rather
    than unlikely.
    """
    raise NotImplementedError


@router.post("/interventions/{intervention_id}/resolve")
async def resolve_intervention(intervention_id: str, req: ResolveRequest) -> dict[str, Any]:
    """Hand control back and resume, or abort the run.

    On resume the runner re-observes rather than trusting a step counter: the human
    may have moved the app several screens on. Replay skips forward to the first
    step whose checkpoint already holds.
    """
    raise NotImplementedError
