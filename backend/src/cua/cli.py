"""CLI — the demo path.

The README's four commands map to the four subcommands here. A reviewer should be
able to run the whole thread without touching HTTP:

    cua discover  --goal "..." --input member_id=12345
    cua replay    cap_get_savings_balance --input member_id=12345
    cua replay    cap_get_savings_balance --input member_id=99999   # business outcome
    cua catalog

`replay` runs with no API key set. That is the check that matters: if the
deterministic path needs a model, it is not deterministic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer

from .catalog import CapabilityNotFound
from .catalog.learn import (
    NothingToLearn,
    all_lines,
    distinguishing_text,
    final_lines,
    with_outcome,
)
from .config import settings
from .discovery import synthesize
from .schema import AppRef, Capability, DiscoveryResult, ReplayResult, RunStatus, Status

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    """`--input k=v` repeated -> dict."""
    parsed: dict[str, str] = {}
    for pair in pairs or ():
        name, _, value = pair.partition("=")
        if not name or not _:
            raise typer.BadParameter(f"expected key=value, got {pair!r}")
        parsed[name.strip()] = value
    return parsed


def _echo(payload: Any) -> None:
    typer.echo(json.dumps(payload, indent=2, default=str))


async def _session(cfg: Any) -> Any:
    """Start the browser and sign in.

    Signing in is a precondition of every capability and is not itself one: it is
    the only flow that types a credential, and keeping it here means no artifact
    ever has to reference one.
    """
    from .runtime import build_session

    session = build_session(cfg)
    await session.start()
    await session.authenticate(cfg.target_username, cfg.target_password)
    return session


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal")],
    start_url: Annotated[str, typer.Option(help="Entry point")] = "",
    input: Annotated[list[str] | None, typer.Option(help="key=value, repeatable")] = None,
    capability_id: Annotated[str, typer.Option(help="Id for the emitted artifact")] = "",
) -> None:
    """LLM-driven run against the live surface; emits a draft capability."""
    cfg = settings()
    inputs = _parse_inputs(input or [])

    async def run() -> tuple[DiscoveryResult, Capability | None]:
        from .runtime import build_catalog, build_discovery

        session = await _session(cfg)
        loop = build_discovery(cfg, session, f"discover-{uuid4().hex[:8]}")
        try:
            result = await loop.run(goal, start_url or cfg.target_base_url, inputs)
            if result.status is not RunStatus.SUCCESS:
                return result, None
            cap = await synthesize(
                loop.state,
                inputs,
                loop.llm,
                capability_id=capability_id,
                app=AppRef(
                    name="targetapp",
                    base_url_pattern=f"^{cfg.target_base_url}(/.*)?$",
                ),
                viewport=cfg.viewport,
            )
            path = build_catalog(cfg).save(cap)
            loop.evidence.capability(cap)
            loop.evidence.note("synthesis", loop.state.declaration)
            result.capability_ref = cap.ref
            loop.evidence.result(result)
            typer.echo(f"wrote {path}")
            return result, cap
        finally:
            await session.stop()

    result, cap = asyncio.run(run())
    _echo(
        {
            "status": result.status.value,
            "capability": result.capability_ref,
            "steps_taken": result.steps_taken,
            "llm_calls": result.llm_calls,
            "stop_reason": result.stop_reason,
            "evidence": result.evidence_dir,
            "inputs": [i.name for i in cap.inputs] if cap else [],
            "outputs": [o.name for o in cap.outputs] if cap else [],
        }
    )
    raise typer.Exit(0 if result.status is RunStatus.SUCCESS else 1)


@app.command()
def replay(
    capability_id: Annotated[str, typer.Argument()],
    input: Annotated[list[str] | None, typer.Option(help="key=value, repeatable")] = None,
    version: Annotated[int, typer.Option()] = 0,
    frames: Annotated[
        str,
        typer.Option(
            help=(
                "Replay against a previous run's recorded frames instead of a live "
                "browser: no display, no model, no target app."
            )
        ),
    ] = "",
) -> None:
    """Deterministic replay. Prints a ReplayResult and exits non-zero on failure.

    Exit codes distinguish the classes, so a shell caller sees the taxonomy too:
      0  success
      0  business outcome        (an answer, not an error)
      2  escalated
      1  hard failure
    """
    cfg = settings()
    inputs = _parse_inputs(input or [])

    async def run() -> ReplayResult:
        from .runtime import build_catalog, build_offline_replay, build_replay

        cap = build_catalog(cfg).load(capability_id, version or None)

        if frames:
            # The decision path, re-derived from pixels that were recorded
            # earlier. Proves the same frames produce the same resolutions,
            # checkpoints and result contract; proves nothing about how the
            # application responds, because nothing is being clicked.
            recorded = sorted(Path(frames).glob("*.png"))
            if not recorded:
                raise typer.BadParameter(f"no frames in {frames}")
            engine = build_offline_replay(
                cfg, recorded, f"offline-{uuid4().hex[:8]}", url=cfg.target_base_url
            )
            return await engine.replay(cap, inputs)

        session = await _session(cfg)
        engine = build_replay(cfg, session, f"replay-{uuid4().hex[:8]}")
        try:
            return await engine.replay(cap, inputs)
        finally:
            await session.stop()

    try:
        result = asyncio.run(run())
    except CapabilityNotFound as e:
        typer.echo(f"no such capability: {e}", err=True)
        raise typer.Exit(1) from e

    _echo(
        {
            "status": result.status.value,
            "capability": result.capability,
            "outputs": result.outputs,
            "outcome": result.outcome.model_dump() if result.outcome else None,
            "failure": result.failure.model_dump() if result.failure else None,
            "intervention_id": result.intervention_id,
            "duration_ms": result.duration_ms,
            "evidence": result.evidence_dir,
            "steps": [
                {
                    "id": s.step_id,
                    "intent": s.intent,
                    "status": s.status.value,
                    "via": s.resolution.value,
                }
                for s in result.steps
            ],
        }
    )
    raise typer.Exit(
        {
            RunStatus.SUCCESS: 0,
            RunStatus.BUSINESS_OUTCOME: 0,
            RunStatus.ESCALATED: 2,
            RunStatus.FAILURE: 1,
        }[result.status]
    )


@app.command("learn-outcome")
def learn_outcome(
    capability_id: Annotated[str, typer.Argument()],
    name: Annotated[str, typer.Option(help="snake_case name for the outcome")],
    description: Annotated[str, typer.Option()] = "",
    input: Annotated[
        list[str] | None,
        typer.Option(help="key=value overrides that reach the outcome, repeatable"),
    ] = None,
    version: Annotated[int, typer.Option()] = 0,
) -> None:
    """Teach a capability what a legitimate alternative result looks like.

    Runs it twice — once with the inputs it was recorded with, once with yours —
    and takes the detector from the difference between the two final screens. The
    wording is copied off the screen that produces it rather than guessed, which
    is the whole reason this exists: a model asked to imagine an error page
    proposed "Accounts" as the detector for "account not found".

    Emits a new draft version. The version production is calling is untouched.
    """
    cfg = settings()
    overrides = _parse_inputs(input or [])
    if not overrides:
        raise typer.BadParameter("give at least one --input that reaches the other result")

    async def run() -> tuple[Capability, str, ReplayResult, ReplayResult]:
        from .runtime import build_catalog, build_replay

        store = build_catalog(cfg)
        cap = store.load(capability_id, version or None)
        reference = {i.name: i.example for i in cap.inputs if i.example}
        missing = [i.name for i in cap.inputs if i.required and not i.example]
        if missing:
            raise typer.BadParameter(
                f"{cap.ref} has no recorded example for {', '.join(missing)}, so there is "
                f"no reference run to compare against"
            )

        session = await _session(cfg)
        try:
            happy = await build_replay(cfg, session, f"learn-ref-{uuid4().hex[:6]}").replay(
                cap, reference
            )
            if happy.status is not RunStatus.SUCCESS:
                raise typer.BadParameter(
                    f"the reference run did not succeed ({happy.status.value}); the "
                    f"comparison would be against the wrong screen"
                )
            other = await build_replay(cfg, session, f"learn-out-{uuid4().hex[:6]}").replay(
                cap, {**reference, **overrides}
            )
            if other.status is RunStatus.SUCCESS:
                raise typer.BadParameter(
                    "the run with your inputs also succeeded, so it did not reach a "
                    "different result"
                )
        finally:
            await session.stop()

        detector = distinguishing_text(
            all_lines(Path(happy.evidence_dir)), final_lines(Path(other.evidence_dir))
        )
        learned = with_outcome(
            cap,
            name=name,
            description=description,
            detector_text=detector,
            inputs={**reference, **overrides},
            version=max(store.versions(cap.id)) + 1,
        )
        store.save(learned)
        return learned, detector, happy, other

    try:
        cap, detector, happy, other = asyncio.run(run())
    except NothingToLearn as e:
        typer.echo(f"nothing to learn: {e}", err=True)
        raise typer.Exit(1) from e
    except CapabilityNotFound as e:
        typer.echo(f"no such capability: {e}", err=True)
        raise typer.Exit(1) from e

    outcome = cap.business_outcomes[-1]
    _echo(
        {
            "capability": cap.ref,
            "status": cap.status.value,
            "learned": outcome.name,
            "detector": outcome.detector.value,
            "observed_as": detector,
            "result_fields": {k: v.value for k, v in outcome.result_fields.items()},
            "reference_run": happy.run_id,
            "outcome_run": other.run_id,
        }
    )


@app.command()
def catalog(status: Annotated[str, typer.Option()] = "") -> None:
    """List saved capabilities with their inputs, outputs and outcomes."""
    from .runtime import build_catalog

    caps = build_catalog(settings()).list(Status(status) if status else None)
    _echo(
        [
            {
                "ref": c.ref,
                "status": c.status.value,
                "goal": c.goal,
                "inputs": {i.name: i.type.value for i in c.inputs},
                "outputs": {o.name: o.type.value for o in c.outputs},
                "outcomes": [o.name for o in c.business_outcomes],
                "steps": len(c.steps),
            }
            for c in caps
        ]
    )


@app.command()
def approve(capability_id: str, version: int, operator: str = "reviewer") -> None:
    """draft -> approved."""
    from .runtime import build_catalog

    cap = build_catalog(settings()).approve(capability_id, version, operator)
    typer.echo(f"{cap.ref} is now {cap.status.value} (approved by {operator})")


@app.command()
def manifest() -> None:
    """The agent-facing tool catalog: approved capabilities as callable tools."""
    from .runtime import build_catalog

    _echo(list(build_catalog(settings()).tool_manifest()))


if __name__ == "__main__":
    app()
