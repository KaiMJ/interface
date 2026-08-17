"""HTTP surface."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..catalog import CapabilityNotFound
from ..clock import now_iso
from ..discovery import synthesize
from ..escalation import HumanActionWatcher
from ..evidence import EvidenceWriter
from ..schema import (
    AppRef,
    DiscoveryResult,
    InterventionResolution,
    ReplayResult,
    RunStatus,
    Status,
)

router = APIRouter()

_STEP_FRAME = re.compile(r"step-(\d+)\.png")


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


def _rt(request: Request) -> Any:
    """The process-wide runtime: settings, catalog, session pool, control registry."""
    return request.app.state.runtime


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --- capabilities ----------------------------------------------------------


@router.get("/capabilities")
async def list_capabilities(request: Request) -> list[dict[str, Any]]:
    """The catalog. Doubles as the agent-facing tool manifest."""
    rt = _rt(request)
    return [
        {
            "ref": c.ref,
            "id": c.id,
            "version": c.version,
            "status": c.status.value,
            "goal": c.goal,
            "description": c.description,
            "inputs": {i.name: i.type.value for i in c.inputs},
            "outputs": {o.name: o.type.value for o in c.outputs},
            "outcomes": [o.name for o in c.business_outcomes],
            "steps": len(c.steps),
        }
        for c in rt.catalog.list()
    ]


@router.get("/capabilities/manifest")
async def manifest(request: Request) -> list[dict[str, Any]]:
    """Approved capabilities as callable tool definitions.

    Ahead of `/capabilities/{id}` in the router on purpose — `manifest` would
    otherwise be read as a capability id.
    """
    return list(_rt(request).catalog.tool_manifest())


@router.get("/capabilities/{capability_id}")
async def get_capability(
    request: Request, capability_id: str, version: int | None = None
) -> dict[str, Any]:
    try:
        cap = _rt(request).catalog.load(capability_id, version)
    except CapabilityNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return dict(cap.model_dump(mode="json", exclude_none=True))


@router.post("/capabilities/{capability_id}/invoke")
async def invoke(request: Request, capability_id: str, req: InvokeRequest) -> ReplayResult:
    """Deterministic replay. The production path an AI agent calls.

    Returns 200 for all three legitimate results — success, business outcome, and
    escalation — with the distinction carried in `status`. A business outcome is
    not an HTTP error: "no such member" is an answer, and encoding it as a 404
    would push the caller into inspecting status codes to tell "you asked wrongly"
    apart from "the record does not exist".

    Hard failures return 500 with a populated `failure` block.
    """
    rt = _rt(request)
    try:
        cap = rt.catalog.load(capability_id, req.version)
    except CapabilityNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    from ..runtime import build_replay

    run_id = f"replay-{uuid4().hex[:8]}"
    session = await rt.session()
    engine = build_replay(rt.settings, session, run_id)
    result = await engine.replay(cap, req.inputs)
    rt.runs[run_id] = result

    if result.status is RunStatus.FAILURE:
        # A failure is still a fully-formed result: the caller gets the step, the
        # expectation and what was actually on screen, not a bare 500.
        raise HTTPException(status_code=500, detail=result.model_dump(mode="json"))
    return result


@router.post("/capabilities/{capability_id}/approve")
async def approve(
    request: Request, capability_id: str, version: int, operator: str
) -> dict[str, Any]:
    """draft -> approved. Gate on unattended replay."""
    try:
        cap = _rt(request).catalog.approve(capability_id, version, operator)
    except CapabilityNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ref": cap.ref, "status": cap.status.value, "approved_by": operator}


# --- discovery -------------------------------------------------------------


@router.post("/discover")
async def discover(request: Request, req: DiscoverRequest) -> DiscoveryResult:
    """Run the LLM-driven loop and emit a draft capability."""
    rt = _rt(request)
    from ..runtime import build_discovery

    run_id = f"discover-{uuid4().hex[:8]}"
    session = await rt.session()
    loop = build_discovery(rt.settings, session, run_id)
    result = await loop.run(req.goal, req.start_url or rt.settings.target_base_url, req.inputs)

    if result.status is RunStatus.SUCCESS:
        cap = await synthesize(
            loop.state,
            req.inputs,
            loop.llm,
            capability_id=req.capability_id or "",
            app=AppRef(
                name="targetapp",
                base_url_pattern=f"^{rt.settings.target_base_url}(/.*)?$",
            ),
            viewport=rt.settings.viewport,
        )
        rt.catalog.save(cap)
        loop.evidence.capability(cap)
        result.capability_ref = cap.ref
        loop.evidence.result(result)

    rt.runs[run_id] = result
    return result


# --- runs ------------------------------------------------------------------


@router.get("/runs")
async def list_runs(request: Request) -> list[dict[str, Any]]:
    """Every run, not only the ones this process started.

    The evidence directory is the durable record; `rt.runs` is a live view of what
    this process is running now. Listing only the latter is why a console watching
    a CLI-driven run showed an empty list — the two most common ways to start a
    run would each be invisible to the other.
    """
    rt = _rt(request)
    listed: dict[str, dict[str, Any]] = {}

    for path in sorted(Path(rt.settings.evidence_dir).glob("*/run.json")):
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # a run mid-write; it will be there on the next poll
        run_id = str(payload.get("run_id") or path.parent.name)
        listed[run_id] = {
            "run_id": run_id,
            "status": payload.get("status", "running"),
            "kind": "discovery" if "goal" in payload else "replay",
            "capability": payload.get("capability") or payload.get("capability_ref"),
            "started_at": payload.get("started_at", ""),
            "finished_at": payload.get("finished_at", ""),
        }

    for run_id, result in rt.runs.items():
        listed[run_id] = {
            "run_id": run_id,
            "status": result.status.value,
            "kind": "replay" if isinstance(result, ReplayResult) else "discovery",
            "capability": getattr(result, "capability", None)
            or getattr(result, "capability_ref", None),
            "started_at": result.started_at,
            "finished_at": result.finished_at,
        }

    return sorted(listed.values(), key=lambda r: str(r["started_at"]), reverse=True)


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    rt = _rt(request)
    result = rt.runs.get(run_id)
    if result is None:
        # Not in memory does not mean it never happened: evidence outlives the
        # process, and a reviewer asking about yesterday's run should get it.
        path = Path(rt.settings.evidence_dir) / run_id / "run.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"no run {run_id}")
        return dict(json.loads(path.read_text()))
    return dict(result.model_dump(mode="json"))


@router.get("/runs/{run_id}/evidence")
async def run_evidence(request: Request, run_id: str) -> dict[str, Any]:
    """What a run left behind, as a manifest the console can render.

    Paths are relative and resolved through `/runs/{id}/evidence/{path}`. The
    console is a separate origin and must not be handed absolute container paths
    it cannot fetch.
    """
    root = _run_dir(request, run_id)
    frames = root / "frames"

    # Strict, because a directory of evidence is not a schema: anything that is
    # not exactly `step-<n>.png` is some other artefact and is skipped rather than
    # parsed hopefully.
    steps = []
    for frame in sorted(frames.glob("step-*.png")):
        match = _STEP_FRAME.fullmatch(frame.name)
        if not match:
            continue
        step_id = int(match.group(1))
        annotated = frames / f"{frame.stem}.annotated.png"
        observation = root / "observations" / f"{frame.stem}.json"
        steps.append(
            {
                "step_id": step_id,
                "frame": f"frames/{frame.name}",
                # The numbered overlay is what the model was shown, and any
                # argument about a decision it made is litigated against it.
                "annotated": f"frames/{annotated.name}" if annotated.exists() else None,
                "observation": (
                    f"observations/{observation.name}" if observation.exists() else None
                ),
            }
        )

    intervention = root / "intervention"
    handoff = {
        name: f"intervention/{name}.png"
        for name in ("handoff", "handback")
        if (intervention / f"{name}.png").exists()
    }
    return {
        "run_id": run_id,
        "steps": steps,
        "intervention": handoff or None,
        "human_actions": _read_jsonl(intervention / "human_actions.jsonl"),
        "capability": _read_json(root / "capability.json"),
        "synthesis": _read_json(root / "synthesis.json"),
    }


@router.get("/runs/{run_id}/evidence/{path:path}")
async def run_evidence_file(request: Request, run_id: str, path: str) -> FileResponse:
    """One file from a run's evidence directory.

    Resolved and then checked to be inside that directory. A run id and a relative
    path both arrive from the network, and serving files by concatenation is how a
    debugging surface starts returning /etc/passwd.
    """
    root = _run_dir(request, run_id)
    target = (root / path).resolve()
    if not target.is_file() or root.resolve() not in target.parents:
        raise HTTPException(status_code=404, detail=f"no evidence at {path}")
    return FileResponse(target)


@router.get("/policy")
async def policy(request: Request) -> dict[str, Any]:
    """The guardrails in force, read-only.

    "What is this agent permitted to do" is the first question anyone asks about
    an automation with a browser, and answering it should not require reading a
    YAML file inside a container. Read-only on purpose: editing guardrails from a
    debug console is a hole, not a feature.
    """
    from ..runtime import build_policy

    loaded = build_policy(_rt(request).settings)
    return {
        "app": loaded.app,
        "allowed_url_patterns": list(loaded.allowed_url_patterns),
        "allowed_actions": sorted(a.value for a in loaded.allowed_actions),
        "risky_disposition": loaded.risky_disposition,
        "risky_intent_patterns": list(loaded.risky_intent_patterns),
        "recoveries": [
            {
                "name": r.name,
                "detector": r.detector_value,
                "actions": [dict(a) for a in r.actions],
                "max_per_run": r.max_per_run,
            }
            for r in loaded.recoveries
        ],
        "app_errors": [{"name": c.name, "detector": c.detector_value} for c in loaded.app_errors],
        "escalations": [
            {"name": c.name, "detector": c.detector_value} for c in loaded.escalations
        ],
        "redaction": {
            "patterns": len(loaded.redact_patterns),
            "declared_values": "redacted",
            "pattern_masking": "seam only — see REPORT §6",
        },
    }


@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str) -> Any:
    """SSE stream of step events. What the console tails.

    Tails the run's own `steps.jsonl` rather than subscribing to an in-process
    bus. The evidence file is written before the step that might not return, so
    the stream and the audit trail cannot disagree — and a run started by the CLI
    is watchable from the console for free.
    """
    steps = Path(_rt(request).settings.evidence_dir) / run_id / "steps.jsonl"

    async def stream() -> Any:
        seen = 0
        idle = 0
        while idle < 600:  # ~5 minutes of silence ends the stream
            if await request.is_disconnected():
                return
            lines = steps.read_text().splitlines() if steps.exists() else []
            if len(lines) > seen:
                idle = 0
                for line in lines[seen:]:
                    yield {"event": "step", "data": line}
                seen = len(lines)
            else:
                idle += 1
            yield {"event": "ping", "data": now_iso()}
            await asyncio.sleep(0.5)

    return EventSourceResponse(stream())


# --- interventions ---------------------------------------------------------


@router.get("/interventions")
async def list_interventions(request: Request) -> list[dict[str, Any]]:
    """Pending escalations. The operator's queue."""
    return [i.model_dump(mode="json") for i in _rt(request).registry.pending()]


@router.get("/interventions/{intervention_id}")
async def get_intervention(request: Request, intervention_id: str) -> dict[str, Any]:
    """Full context: capability, goal, step, reason, screenshot, VNC URL."""
    control = _find(request, intervention_id)
    assert control.intervention is not None
    return dict(control.intervention.model_dump(mode="json"))


@router.post("/interventions/{intervention_id}/take")
async def take_control(request: Request, intervention_id: str, operator: str) -> dict[str, Any]:
    """Operator claims the live session.

    Flips the control token to HUMAN and starts the X-layer action watcher. Until
    this is called the automation has stopped but nobody holds control — which is
    the state that makes "the agent clicked while I was typing" impossible rather
    than unlikely.
    """
    rt = _rt(request)
    control = _find(request, intervention_id)
    try:
        control.take_control(operator)
    except Exception as e:  # noqa: BLE001 - a state-machine refusal is a 409
        raise HTTPException(status_code=409, detail=str(e)) from e

    watcher = HumanActionWatcher(rt.settings.display)
    watcher.start()
    # Capture is best-effort and must never block the transfer. An operator who
    # cannot take a session because the screenshotter failed is worse off than one
    # who takes it with a gap in the evidence — and the gap is reported, not
    # hidden.
    note = watcher.unavailable or _snapshot(watcher, rt, control.run_id, "handoff")
    rt.watchers[intervention_id] = watcher

    return {
        "intervention_id": intervention_id,
        "run_id": control.run_id,
        "holder": control.holder.value,
        "vnc_url": control.intervention.vnc_url if control.intervention else None,
        "capturing": note is None,
        "capture_note": note,
    }


@router.post("/interventions/{intervention_id}/resolve")
async def resolve_intervention(
    request: Request, intervention_id: str, req: ResolveRequest
) -> dict[str, Any]:
    """Hand control back and resume, or abort the run.

    On resume the runner re-observes rather than trusting a step counter: the human
    may have moved the app several screens on. Replay skips forward to the first
    step whose checkpoint already holds.
    """
    rt = _rt(request)
    control = _find(request, intervention_id)

    watcher = rt.watchers.pop(intervention_id, None)
    actions = watcher.stop() if watcher is not None else []
    if watcher is not None:
        _snapshot(watcher, rt, control.run_id, "handback")

    resolution = InterventionResolution(
        id=intervention_id,
        outcome=req.outcome,
        operator=req.operator,
        note=req.note,
        human_actions=actions,
        resolved_at=now_iso(),
    )
    try:
        control.release(resolution)
    except Exception as e:  # noqa: BLE001 - a state-machine refusal is a 409
        raise HTTPException(status_code=409, detail=str(e)) from e

    EvidenceWriter(rt.settings.evidence_dir, control.run_id, rt.redactor).intervention(
        control.intervention, resolution
    )
    return {
        "intervention_id": intervention_id,
        "outcome": req.outcome,
        "human_actions": len(actions),
        "holder": control.holder.value,
    }


def _run_dir(request: Request, run_id: str) -> Path:
    root = (Path(_rt(request).settings.evidence_dir) / run_id).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail=f"no evidence for {run_id}")
    return root


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def _read_jsonl(path: Path) -> list[Any]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _find(request: Request, intervention_id: str) -> Any:
    try:
        return _rt(request).registry.by_intervention(intervention_id)
    except KeyError as e:
        raise HTTPException(
            status_code=404, detail=f"no open intervention {intervention_id}"
        ) from e


def _snapshot(watcher: Any, rt: Any, run_id: str, label: str) -> str | None:
    """Returns None on success, or why the frame could not be taken."""
    try:
        watcher.snapshot(_evidence_dir(rt, run_id) / f"{label}.png", label)
    except Exception as e:  # noqa: BLE001 - evidence is best-effort, control is not
        return f"{label} frame unavailable: {type(e).__name__}"
    return None


def _evidence_dir(rt: Any, run_id: str) -> Path:
    path = Path(rt.settings.evidence_dir) / run_id / "intervention"
    path.mkdir(parents=True, exist_ok=True)
    return path


__all__ = ["DiscoverRequest", "InvokeRequest", "ResolveRequest", "Status", "router"]
