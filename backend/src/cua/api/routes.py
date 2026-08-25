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
    DiscoveryResult,
    FailureDetail,
    FailureKind,
    InterventionResolution,
    ReplayResult,
    RunStatus,
    Status,
)

router = APIRouter()

_STEP_FRAME = re.compile(r"step-(\d+)\.png")


class DiscoverRequest(BaseModel):
    goal: str
    start_url: str = ""
    # Which application to drive; selects its policy file. Defaults to the
    # deployment's configured app.
    app: str | None = None
    # Also the parameter declaration: any recorded literal matching one of these
    # becomes a placeholder at synthesis. How "who decided 12345 was a parameter?"
    # gets a deterministic answer.
    inputs: dict[str, Any] = {}
    capability_id: str | None = None


class InvokeRequest(BaseModel):
    inputs: dict[str, Any] = {}
    version: int | None = None


class StartReplayRequest(BaseModel):
    capability_id: str
    inputs: dict[str, Any] = {}
    version: int | None = None


class ResolveRequest(BaseModel):
    outcome: str            # "resume" | "abort"
    operator: str = "unknown"
    note: str = ""


def _begin(rt: Any, run_id: str, is_run: bool = True) -> None:
    """Claim the session for a run, or refuse.

    409, not a queue. There is one browser on one X display, so a second run would
    not wait politely — it would drive the same pixels. An operator console makes
    starting a second run one mis-click away, which is exactly why the refusal has
    to live in the control plane rather than in the UI.
    """
    # Imported here rather than at module scope: `main` imports this module to
    # build the router, so a top-level import back into it is a cycle.
    from .main import RunInProgress

    try:
        rt.begin(run_id, is_run=is_run)
    except RunInProgress as e:
        raise HTTPException(status_code=409, detail=str(e)) from e


def _launch(rt: Any, run_id: str, coro: Any, summary: dict[str, Any]) -> None:
    """Run a coroutine in the background, releasing the session when it ends.

    The task is held on the runtime rather than fired and forgotten: an
    unreferenced asyncio task can be garbage-collected mid-run, and a run that
    vanishes with its evidence half-written is the worst possible failure for a
    system whose whole argument is its audit trail.

    `summary` makes the run visible from the moment it is accepted. Its first
    seconds go on starting a browser and signing in, during which it has written
    no evidence — and a console that answers 404 for a run the operator just
    started is a console they stop believing.
    """
    rt.pending[run_id] = {
        "run_id": run_id,
        "status": RunStatus.RUNNING.value,
        "steps": [],
        "started_at": now_iso(),
        **summary,
    }

    async def guarded() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 - the run records its own failure
            pass
        finally:
            rt.end(run_id)
            rt.tasks.pop(run_id, None)
            rt.pending.pop(run_id, None)

    rt.tasks[run_id] = asyncio.create_task(guarded())


def _rt(request: Request) -> Any:
    """The process-wide runtime: settings, catalog, session pool, control registry."""
    return request.app.state.runtime


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Up, and what is happening.

    `active_run` is here rather than on a route of its own because every client
    that polls health wants it: there is one display, so "is the session free" is
    part of "are you alive". The console greys its start button on it.
    """
    rt = _rt(request)
    return {
        "status": "ok",
        # The run holding the display, and separately whether anything is: arming a
        # fault holds it too and is not a run.
        "active_run": rt.active_run if rt.active_is_run else None,
        "session_busy": rt.active_run is not None,
        "default_app": rt.settings.default_app,
        "apps": rt.settings.apps(),
    }


# --- capabilities ----------------------------------------------------------


@router.get("/capabilities")
async def list_capabilities(request: Request, app: str | None = None) -> list[dict[str, Any]]:
    """The catalog. Doubles as the agent-facing tool manifest."""
    rt = _rt(request)
    return [
        {
            "ref": c.ref,
            "id": c.id,
            "app": c.app.name,
            "version": c.version,
            "status": c.status.value,
            "goal": c.goal,
            "description": c.description,
            "inputs": {i.name: i.type.value for i in c.inputs},
            "outputs": {o.name: o.type.value for o in c.outputs},
            "outcomes": [o.name for o in c.business_outcomes],
            "steps": len(c.steps),
        }
        for c in rt.catalog.list(app=app)
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


@router.get("/capabilities/{capability_id}/history")
async def capability_history(
    request: Request, capability_id: str, limit: int = 50
) -> dict[str, Any]:
    """Every run of this capability, plus what they say collectively.

    The per-step drift signals — which resolver tier fired, how the frame settled —
    are only meaningful in aggregate. One run resolving by `recorded_bbox` is
    noise; a capability whose last five runs did is an application that moved. That
    reading had nowhere to live: the data was on disk, per step, per run, and
    nothing ever put two runs side by side.
    """
    rt = _rt(request)
    runs = [r for r in _runs(rt) if str(r.get("capability") or "").startswith(capability_id)]
    runs = runs[:limit]

    tiers: dict[str, int] = {}
    settled: dict[str, int] = {}
    statuses: dict[str, int] = {}
    durations: list[int] = []
    observe_ms = 0
    observations = 0
    step_ms = 0
    step_count = 0
    for summary in runs:
        payload = _run_json(rt, str(summary["run_id"]))
        if payload is None:
            continue
        statuses[str(payload.get("status", "unknown"))] = (
            statuses.get(str(payload.get("status", "unknown")), 0) + 1
        )
        if payload.get("duration_ms"):
            durations.append(int(payload["duration_ms"]))
        for step in payload.get("steps", []):
            tier = str(step.get("resolution", "none"))
            tiers[tier] = tiers.get(tier, 0) + 1
            how = str(step.get("settled_by", "unset"))
            settled[how] = settled.get(how, 0) + 1
            phases = step.get("phases") or {}
            observe_ms += int(phases.get("observe_ms", 0))
            observations += int(phases.get("observations", 0))
            step_ms += int(step.get("duration_ms", 0))
            step_count += 1

    terminal = [s for s in statuses.items() if s[0] != "running"]
    total = sum(count for _, count in terminal)
    return {
        "capability_id": capability_id,
        "versions": list(rt.catalog.versions(capability_id)),
        "runs": runs,
        "aggregate": {
            "total": total,
            "statuses": statuses,
            "resolution_tiers": tiers,
            "settled_by": settled,
            # Not a quality score: the share of runs that ended on the path the
            # capability was recorded for. A business outcome is a correct answer and
            # does not count against it.
            "success_rate": (statuses.get("success", 0) / total) if total else None,
            "median_duration_ms": _median(durations),
            # Perception dominates everything else by two orders of magnitude, so
            # "how much of this capability is OCR" is the only performance question
            # worth a dashboard.
            "observe_share": (observe_ms / step_ms) if step_ms else None,
            "observations_per_step": (observations / step_count) if step_count else None,
            # The reading that matters: portable resolutions decaying into the
            # recorded box, as a share so runs of different lengths compare.
            "drift_share": (
                tiers.get("recorded_bbox", 0) / sum(tiers.values()) if tiers else None
            ),
        },
    }


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

    if cap.status is not Status.APPROVED:
        # Refused here *and* in the engine: this gives a clean 403 without opening a
        # run directory for something that never touched the app, and the engine's is
        # the one a future call site cannot bypass.
        raise HTTPException(
            status_code=403,
            detail=f"{cap.ref} is {cap.status.value}; unattended invocation needs approval",
        )

    run_id = f"replay-{uuid4().hex[:8]}"
    # The capability names the application it was recorded against, so an invoke
    # cannot execute an artifact under some other app's guardrails.
    _begin(rt, run_id)
    try:
        # The one path that refuses a draft. Elsewhere a human is driving and
        # replaying an unapproved recording is how it gets reviewed; here the caller
        # is an agent, and nobody is watching.
        result = await _replay(rt, run_id, cap, req.inputs, require_approved=True)
    finally:
        rt.end(run_id)

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
    """Run the LLM-driven loop and emit a draft capability.

    Synchronous: the caller waits for the run. That is the right shape for the CLI
    and for a script. The console uses `/runs/discover` instead, which is the same
    work started in the background — a browser tab should not have to hold a
    connection open for the length of a recording session to watch one.
    """
    rt = _rt(request)
    run_id = f"discover-{uuid4().hex[:8]}"
    _begin(rt, run_id)
    try:
        return await _discover(rt, run_id, req)
    finally:
        rt.end(run_id)


async def _discover(rt: Any, run_id: str, req: DiscoverRequest) -> DiscoveryResult:
    """The discovery run itself, shared by the sync and background entry points."""
    from ..runtime import build_discovery, build_policy, entry_url

    policy = build_policy(rt.settings, req.app)
    session = await rt.session(req.app)
    loop = build_discovery(rt.settings, session, run_id, req.app)
    result = await loop.run(
        req.goal, req.start_url or entry_url(rt.settings, policy), req.inputs
    )

    if result.status is RunStatus.SUCCESS:
        # The loop has written its terminal result, but synthesis still has to turn
        # the recording into an artifact. Without this the console shows `success`
        # with no capability for several seconds — done before the deliverable exists.
        loop.evidence.result(
            result.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "stop_reason": "synthesizing the recording into a capability",
                }
            )
        )
        try:
            cap = await synthesize(
                loop.state,
                req.inputs,
                loop.llm,
                capability_id=req.capability_id or "",
                app=policy.app_ref(),
                viewport=rt.settings.viewport,
            )
            rt.catalog.save(cap)
            loop.evidence.capability(cap)
            result.capability_ref = cap.ref
        except Exception as e:  # noqa: BLE001 - the run must still report
            # The flow was driven and only the contract could not be written.
            # `success` would claim a capability nobody can call; a failed run would
            # deny the model reached the goal. This says which part failed.
            result.status = RunStatus.FAILURE
            result.failure = FailureDetail(
                kind=FailureKind.INTERNAL,
                message=f"the goal was reached but synthesis failed: {type(e).__name__}: {e}",
            )
            result.stop_reason = "synthesis failed; the recording is in this run's evidence"
        loop.evidence.result(result)

    rt.runs[run_id] = result
    return result


# --- launching runs from the console ---------------------------------------


@router.post("/runs/discover")
async def start_discovery(request: Request, req: DiscoverRequest) -> dict[str, Any]:
    """Start a discovery run in the background and return its id immediately.

    The console's prompt box. A discovery run takes a minute or more, and holding
    the request open for it means the operator cannot watch the thing they just
    started — the run's own evidence stream is how they watch it, and it exists
    from the first step.
    """
    rt = _rt(request)
    run_id = f"discover-{uuid4().hex[:8]}"
    _begin(rt, run_id)
    _launch(
        rt,
        run_id,
        _discover(rt, run_id, req),
        {"kind": "discovery", "goal": req.goal, "app": req.app or rt.settings.default_app},
    )
    return {"run_id": run_id, "kind": "discovery", "goal": req.goal}


@router.post("/runs/replay")
async def start_replay(request: Request, req: StartReplayRequest) -> dict[str, Any]:
    """Start a replay in the background and return its id immediately.

    Deliberately a different route from `/capabilities/{id}/invoke` rather than a
    flag on it. Invoke is the production contract an AI agent calls and it returns
    a `ReplayResult`; changing what it returns based on a query parameter would
    make that contract conditional. This one is the console's re-run button.
    """
    rt = _rt(request)
    try:
        cap = rt.catalog.load(req.capability_id, req.version)
    except CapabilityNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    run_id = f"replay-{uuid4().hex[:8]}"
    _begin(rt, run_id)
    _launch(
        rt,
        run_id,
        _replay(rt, run_id, cap, req.inputs),
        {"kind": "replay", "capability": cap.ref, "app": cap.app.name},
    )
    return {"run_id": run_id, "kind": "replay", "capability": cap.ref}


async def _replay(
    rt: Any, run_id: str, cap: Any, inputs: dict[str, Any], require_approved: bool = False
) -> ReplayResult:
    from ..runtime import build_replay

    target_app = cap.app.name or None
    session = await rt.session(target_app)
    engine = build_replay(rt.settings, session, run_id, target_app, require_approved)
    result: ReplayResult = await engine.replay(cap, inputs)
    rt.runs[run_id] = result
    return result


# --- runs ------------------------------------------------------------------


@router.get("/runs")
async def list_runs(
    request: Request,
    capability: str | None = None,
    app: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Every run, not only the ones this process started.

    The evidence directory is the durable record; `rt.runs` is a live view of what
    this process is running now. Listing only the latter is why a console watching
    a CLI-driven run showed an empty list — the two most common ways to start a
    run would each be invisible to the other.

    Filters exist because "show me every run of this capability" is the question an
    operator asks about a flow they are about to trust, and the answer used to be
    "read the whole list and match the strings yourself".
    """
    runs = _runs(_rt(request))
    if capability:
        runs = [r for r in runs if str(r.get("capability") or "").startswith(capability)]
    if app:
        runs = [r for r in runs if r.get("app") == app]
    if kind:
        runs = [r for r in runs if r.get("kind") == kind]
    if status:
        runs = [r for r in runs if r.get("status") == status]
    return runs[:limit]


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: str) -> dict[str, Any]:
    rt = _rt(request)
    result = rt.runs.get(run_id)
    if result is None:
        # Evidence outlives the process, and yesterday's run is still a fair ask.
        path = Path(rt.settings.evidence_dir) / run_id / "run.json"
        if not path.exists():
            # …and not on disk either: a run accepted a second ago is still
            # starting a browser.
            starting = rt.pending.get(run_id)
            if starting is None:
                raise HTTPException(status_code=404, detail=f"no run {run_id}")
            return dict(starting)
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
        after = frames / f"{frame.stem}.after.png"
        observation = root / "observations" / f"{frame.stem}.json"
        steps.append(
            {
                "step_id": step_id,
                "frame": f"frames/{frame.name}",
                # The numbered overlay is what the model was shown, and any
                # argument about a decision it made is litigated against it.
                "annotated": f"frames/{annotated.name}" if annotated.exists() else None,
                # What the step produced, as against what it acted on. The
                # resolved target belongs on the first; a checkpoint was judged
                # on the second.
                "after": f"frames/{after.name}" if after.exists() else None,
                "observation": (
                    f"observations/{observation.name}" if observation.exists() else None
                ),
            }
        )

    intervention = root / "intervention"
    handoff: dict[str, Any] = {
        name: f"intervention/{name}.png"
        for name in ("handoff", "handback")
        if (intervention / f"{name}.png").exists()
    }
    # The request and the resolution, not only the two frames. What the operator
    # was told, what they decided, and what note they left is the record of a human
    # touching regulated data — and it was on disk with nothing reading it.
    request_json = _read_json(intervention / "request.json")
    resolution_json = _read_json(intervention / "resolution.json")
    if request_json or resolution_json:
        handoff["request"] = request_json
        handoff["resolution"] = resolution_json

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
async def policy(request: Request, app: str | None = None) -> dict[str, Any]:
    """The guardrails in force, read-only.

    "What is this agent permitted to do" is the first question anyone asks about
    an automation with a browser, and answering it should not require reading a
    YAML file inside a container. Read-only on purpose: editing guardrails from a
    debug console is a hole, not a feature.
    """
    from ..runtime import build_policy

    rt = _rt(request)
    try:
        loaded = build_policy(rt.settings, app)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {
        "app": loaded.app,
        "apps": rt.settings.apps(),
        "vendor": loaded.vendor,
        "base_url_pattern": loaded.base_url_pattern,
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
    root = Path(_rt(request).settings.evidence_dir) / run_id
    steps = root / "steps.jsonl"
    run = root / "run.json"
    thinking = root / "thinking.json"

    async def stream() -> Any:
        seen = 0
        idle = 0
        stamp = 0.0
        thought = 0.0
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

            # The run record itself, whenever it changes. The engine rewrites it
            # before every step, so this is how status, outputs and a failure reach
            # the console without it re-polling the whole run — and how a run that
            # ends between two step lines is seen to have ended at all.
            try:
                changed = run.stat().st_mtime
                if changed != stamp:
                    stamp = changed
                    idle = 0
                    yield {"event": "run", "data": run.read_text()}
            except (OSError, ValueError):
                pass

            # Which step the run is waiting on the model for. Not part of the
            # audit trail — it describes work that has not happened yet — but read
            # from a file like everything else, so no in-process bus appears here
            # for one event. The console discards it once that step lands.
            try:
                pondered = thinking.stat().st_mtime
                if pondered != thought:
                    thought = pondered
                    idle = 0
                    yield {"event": "thinking", "data": thinking.read_text()}
            except (OSError, ValueError):
                pass

            yield {"event": "ping", "data": now_iso()}
            await asyncio.sleep(0.5)

    return EventSourceResponse(stream())


# --- the fault harness -----------------------------------------------------


class FaultRequest(BaseModel):
    names: list[str] = []


@router.get("/session/faults")
async def list_faults(request: Request, app: str | None = None) -> dict[str, Any]:
    """The demo app's injectable faults, if it has any.

    A property of the *application*, not of this system: the mock bank ships a
    fault panel so §3.3's runtime conditions can be produced on demand, and a real
    core banking system does not. An app whose policy declares no `fault_url` gets
    an empty list here and no panel in the console.
    """
    from ..runtime import build_policy

    rt = _rt(request)
    policy = build_policy(rt.settings, app)
    if not policy.fault_url:
        return {"available": {}, "armed": [], "url": None}

    available: dict[str, str] = {}
    try:
        import httpx

        # Read the catalogue of faults server-side. Their *state* is not readable
        # from here and deliberately so: faults live in a cookie inside the
        # automation's browser, which is what stops a reviewer's own tab from
        # sharing them. What is armed is what we armed.
        with httpx.Client(timeout=3.0) as client:
            available = dict(client.get(policy.fault_url).json().get("available", {}))
    except Exception:  # noqa: BLE001 - the app being down is not this route's problem
        pass
    return {"available": available, "armed": list(rt.armed_faults), "url": policy.fault_url}


@router.post("/session/faults")
async def arm_faults(request: Request, req: FaultRequest, app: str | None = None) -> dict[str, Any]:
    """Arm a set of faults in the automation's own browser. Test harness only.

    Faults live in a cookie, so they cannot be set from outside the browser that
    will be driven — which is exactly why this route exists rather than the
    console calling the app directly. It drives the session there with the driver
    and back, the same way `cua replay --fault` does.

    Two things make this safe to expose. It is refused while a run holds the
    session, so it can never interleave with one. And the URL it navigates to is
    outside the app's own allowlist by construction: this is something done *to*
    the automation from outside it, never a step the agent can take — an agent
    that could arm its own faults could disarm them.
    """
    from ..runtime import build_policy

    rt = _rt(request)
    policy = build_policy(rt.settings, app)
    if not policy.fault_url:
        raise HTTPException(status_code=404, detail=f"{policy.app} declares no fault harness")

    _begin(rt, "arm-faults", is_run=False)
    try:
        session = await rt.session(app)
        names = ",".join(n for n in req.names if n)
        # Driver, not engine: no policy check, no evidence, no step. Arming is not
        # something the run did.
        await session.driver.navigate(f"{policy.fault_url}?set={names}")
        await session.driver.navigate(entry_of(rt, policy))
    finally:
        rt.end("arm-faults")

    rt.armed_faults = list(req.names)
    return {"armed": rt.armed_faults}


def entry_of(rt: Any, policy: Any) -> str:
    from ..runtime import entry_url

    return str(entry_url(rt.settings, policy))


# --- interventions ---------------------------------------------------------


@router.get("/interventions")
async def list_interventions(
    request: Request, include_resolved: bool = False
) -> list[dict[str, Any]]:
    """Pending escalations. The operator's queue.

    `include_resolved` adds the ones already dealt with, so the console can show
    what happened on this session as well as what still needs a person.
    """
    registry = _rt(request).registry
    found = registry.all() if include_resolved else registry.pending()
    return [i.model_dump(mode="json") for i in found]


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

    On resume the runner re-observes rather than trusting the frame it parked on,
    because the human may have moved the app several screens. What it does *not*
    do is search forward for the first step whose checkpoint already holds: it
    does not need to, because the next step asserts its own screen and verifies
    its own checkpoint, so an operator who left the application somewhere
    unexpected produces a loud `WRONG_SCREEN` rather than a blind click. See
    `escalation/control.py` for the two resume cases and why they differ.
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


def _runs(rt: Any) -> list[dict[str, Any]]:
    """Run summaries from disk, overlaid with what this process is running now."""
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
            "app": payload.get("app") or "",
            "capability": payload.get("capability") or payload.get("capability_ref"),
            "goal": payload.get("goal") or "",
            "steps": len(payload.get("steps", [])),
            "duration_ms": payload.get("duration_ms") or 0,
            "started_at": payload.get("started_at", ""),
            "finished_at": payload.get("finished_at", ""),
        }

    # A run that has been accepted but has not written anything yet. Listed so
    # the console shows what the operator just started, rather than nothing.
    for run_id, starting in rt.pending.items():
        listed.setdefault(run_id, {**starting, "steps": 0, "duration_ms": 0})

    for run_id, result in rt.runs.items():
        listed[run_id] = {
            "run_id": run_id,
            "status": result.status.value,
            "kind": "replay" if isinstance(result, ReplayResult) else "discovery",
            "app": getattr(result, "app", "") or "",
            "capability": getattr(result, "capability", None)
            or getattr(result, "capability_ref", None),
            "goal": getattr(result, "goal", "") or "",
            "steps": len(result.steps),
            "duration_ms": getattr(result, "duration_ms", 0) or 0,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
        }

    return sorted(listed.values(), key=lambda r: str(r["started_at"]), reverse=True)


def _run_json(rt: Any, run_id: str) -> dict[str, Any] | None:
    result = rt.runs.get(run_id)
    if result is not None:
        return dict(result.model_dump(mode="json"))
    path = Path(rt.settings.evidence_dir) / run_id / "run.json"
    try:
        return dict(json.loads(path.read_text()))
    except (OSError, ValueError):
        return None


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _run_dir(request: Request, run_id: str) -> Path:
    """One run's evidence directory, from a run id that arrived over the network.

    The id is validated before it is joined, not after. The per-file check below
    confines a *path* to this directory, but it confines it to whichever directory
    this function returned — so an id that escapes here escapes the check too, and
    `/runs/..%2F..%2Fetc/evidence/passwd` would be answered rather than refused.
    A run id is a slug we generated; anything that is not one is a 404.
    """
    if not _RUN_ID.match(run_id):
        raise HTTPException(status_code=404, detail=f"no evidence for {run_id!r}")
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


__all__ = [
    "DiscoverRequest",
    "InvokeRequest",
    "ResolveRequest",
    "StartReplayRequest",
    "Status",
    "router",
]
