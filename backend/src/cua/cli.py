"""CLI — the demo path.

The README's commands map to the subcommands here, so a reviewer can run the whole
thread without touching HTTP.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable
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
from .schema import Capability, DiscoveryResult, ReplayResult, RunStatus, Status

# `step-04.png` is what the step acted on and `step-04.after.png` what the application showed
# once it had — byte-identical to `step-05.png`, being the same screen. `ImageFileScreen`
# advances only when the driver acts, so the feed it needs is one frame per *transition*: each
# step's own frame, then the last after-frame for the final checkpoint. Feeding both spellings
# feeds every screen twice and the run ends up a frame behind.
_FRAME_FILE = re.compile(r"step-(\d+)(\.after)?\.png$")


def _observation_sequence(frames: Iterable[Path]) -> list[Path]:
    """The screens the run actually saw, in order, each appearing once."""
    numbered = [(m, p) for p in frames if (m := _FRAME_FILE.search(p.name))]
    primary = sorted(
        (p for m, p in numbered if not m.group(2)), key=lambda p: int(_step_of(p))
    )
    afters = sorted((p for m, p in numbered if m.group(2)), key=lambda p: int(_step_of(p)))
    return [*primary, *afters[-1:]] if primary else afters


def _step_of(path: Path) -> str:
    m = _FRAME_FILE.search(path.name)
    return m.group(1) if m else "0"


_SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def _run_id(given: str, prefix: str) -> str:
    """A chosen name, or a random one.

    `replay-47afdc71` tells a reviewer opening `evidence/` nothing; `replay-fault-banner`
    tells them what the run was. The smoke scripts have always named their own runs — this
    is the same thing, reachable from the CLI.

    A run id becomes a directory name, so it is checked rather than trusted, and an existing
    directory is never written into: evidence that silently absorbed a second run would be
    worse than no evidence.
    """
    if not given:
        return f"{prefix}-{uuid4().hex[:8]}"
    name = given if given.startswith(f"{prefix}-") else f"{prefix}-{given}"
    if not _SAFE_RUN_ID.match(name):
        raise typer.BadParameter(f"{given!r} is not a usable directory name ([a-z0-9-])")
    if (Path(settings().evidence_dir) / name).exists():
        raise typer.BadParameter(f"evidence/{name} already exists; pick another --run-id")
    return name


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


async def _session(cfg: Any, app: str | None = None) -> Any:
    """Start the browser and sign in, using the named app's policy.

    Signing in is a precondition of every capability and is not itself one: it is the only flow
    that types a credential, so no artifact ever has to reference one. Which credential and
    which recipe are facts about the application, so both arrive with the policy.
    """
    from .runtime import AuthenticationFailed, build_session

    session = build_session(cfg, app)
    await session.start()
    try:
        await session.authenticate(cfg.target_username, cfg.target_password)
    except AuthenticationFailed as e:
        # Sign-on happens before `replay()`, outside the block that turns every other error
        # into a typed result. Stop with the same shape any other refusal has, and leave
        # nothing holding the browser.
        await session.stop()
        typer.echo(f"could not sign in to {app or cfg.default_app}: {e}", err=True)
        typer.echo(
            "if another run is already on the display, wait for it to finish", err=True
        )
        raise typer.Exit(1) from e
    return session


async def _arm_faults(session: Any, base_url: str, names: list[str]) -> None:
    """Switch on a demo-app fault in the automation's own browser. Harness only.

    Faults live in a cookie, so a reviewer's tab and the automation's browser do not share
    them and there is no way to arm one from outside that browser. This drives the session to
    the fault panel and back, before the run and before the engine exists.

    Deliberately *not* reachable from the replay path: it uses the driver directly, `/dev` is
    excluded from the app's allowlist, and nothing about it is recordable.
    """
    typer.echo(f"[harness] arming faults: {', '.join(names)}", err=True)
    await session.driver.navigate(f"{base_url.rstrip('/')}/api/faults?set={','.join(names)}")
    await session.observe()


# The flag is named explicitly: typer would otherwise derive `--app-name` from
# the parameter, and `app` is taken by the Typer instance itself.
APP_OPT = typer.Option(
    "--app", help="Application to run against; selects policies/<app>.yaml"
)


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal")],
    app_name: Annotated[str, APP_OPT] = "",
    start_url: Annotated[str, typer.Option(help="Entry point")] = "",
    input: Annotated[list[str] | None, typer.Option(help="key=value, repeatable")] = None,
    capability_id: Annotated[str, typer.Option(help="Id for the emitted artifact")] = "",
    run_id: Annotated[
        str, typer.Option(help="Name this run's evidence directory instead of taking a random one.")
    ] = "",
) -> None:
    """LLM-driven run against the live surface; emits a draft capability."""
    cfg = settings()
    inputs = _parse_inputs(input or [])

    async def run() -> tuple[DiscoveryResult, Capability | None]:
        from .runtime import build_catalog, build_discovery, build_policy, entry_url

        policy = build_policy(cfg, app_name or None)
        session = await _session(cfg, app_name or None)
        loop = build_discovery(cfg, session, _run_id(run_id, "discover"), app_name or None)
        try:
            result = await loop.run(goal, start_url or entry_url(cfg, policy), inputs)
            state = loop.state
            if result.status is not RunStatus.SUCCESS or state is None:
                return result, None
            cap = await synthesize(
                state,
                inputs,
                loop.llm,
                capability_id=capability_id,
                # From the policy, so the artifact names an application that has guardrails
                # rather than whichever URL this deployment sits behind.
                app=policy.app_ref(),
                viewport=cfg.viewport,
                policy=policy,
            )
            path = build_catalog(cfg).save(cap)
            loop.evidence.capability(cap)
            loop.evidence.note("synthesis", state.declaration)
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
            "app": cap.app.name if cap else (app_name or cfg.default_app),
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
    app_name: Annotated[str, APP_OPT] = "",
    frames: Annotated[
        str,
        typer.Option(
            help=(
                "Replay against a previous run's recorded frames instead of a live "
                "browser: no display, no model, no target app."
            )
        ),
    ] = "",
    fault: Annotated[
        list[str] | None,
        typer.Option(
            help=(
                "TEST HARNESS: arm a fault in the demo app before the run "
                "(modal, slow, expired, error500, ...). Repeatable."
            )
        ),
    ] = None,
    run_id: Annotated[
        str,
        typer.Option(
            help=(
                "Name this run's evidence directory instead of taking a random one. "
                "`replay-banner` reads as what it is where `replay-47afdc71` does not."
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
        from .runtime import (
            build_catalog,
            build_offline_replay,
            build_policy,
            build_replay,
            entry_url,
        )

        cap = build_catalog(cfg).load(capability_id, version or None)
        # The capability names its own application; `--app` overrides that for a tenant
        # running the same product under a different policy.
        target_app = app_name or cap.app.name or None
        policy = build_policy(cfg, target_app)

        if frames:
            # The decision path, re-derived from previously recorded pixels: the same frames
            # produce the same resolutions, checkpoints and result contract. Says nothing
            # about how the application responds, since nothing is clicked.
            recorded = _observation_sequence(Path(frames).glob("*.png"))
            if not recorded:
                raise typer.BadParameter(f"no frames in {frames}")
            engine = build_offline_replay(
                cfg,
                recorded,
                _run_id(run_id, "offline"),
                url=entry_url(cfg, policy),
                app=target_app,
            )
            return await engine.replay(cap, inputs)

        session = await _session(cfg, target_app)
        if fault:
            await _arm_faults(session, entry_url(cfg, policy), fault)
        engine = build_replay(cfg, session, _run_id(run_id, "replay"), target_app)
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
    app_name: Annotated[str, APP_OPT] = "",
) -> None:
    """Teach a capability what a legitimate alternative result looks like.

    Runs it twice — once with the inputs it was recorded with, once with yours — and takes the
    detector from the difference between the two final screens, so the wording is copied off
    the screen that produces it rather than guessed.

    Emits a new draft version. The version production is calling is untouched.
    """
    cfg = settings()
    overrides = _parse_inputs(input or [])
    if not overrides:
        raise typer.BadParameter("give at least one --input that reaches the other result")

    async def run() -> tuple[Capability, str, ReplayResult, ReplayResult]:
        from .runtime import build_catalog, build_policy, build_replay

        store = build_catalog(cfg)
        cap = store.load(capability_id, version or None)
        reference = {i.name: i.example for i in cap.inputs if i.example}
        missing = [i.name for i in cap.inputs if i.required and not i.example]
        if missing:
            raise typer.BadParameter(
                f"{cap.ref} has no recorded example for {', '.join(missing)}, so there is "
                f"no reference run to compare against"
            )

        target_app = app_name or cap.app.name or None
        session = await _session(cfg, target_app)
        try:
            happy = await build_replay(
                cfg, session, f"learn-ref-{uuid4().hex[:6]}", target_app
            ).replay(cap, reference)
            if happy.status is not RunStatus.SUCCESS:
                raise typer.BadParameter(
                    f"the reference run did not succeed ({happy.status.value}); the "
                    f"comparison would be against the wrong screen"
                )
            other = await build_replay(
                cfg, session, f"learn-out-{uuid4().hex[:6]}", target_app
            ).replay(cap, {**reference, **overrides})
            if other.status is RunStatus.SUCCESS:
                raise typer.BadParameter(
                    "the run with your inputs also succeeded, so it did not reach a "
                    "different result"
                )
            # Already working. Learning it again would compare two screens that now differ
            # only by this record's own data, producing a worse detector than the one in place.
            if (
                other.status is RunStatus.BUSINESS_OUTCOME
                and other.outcome is not None
                and other.outcome.name == name
            ):
                raise typer.BadParameter(
                    f"{cap.ref} already returns {name!r} for these inputs; there is "
                    f"nothing to learn. Use --version to teach an earlier version."
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
            # So a rediscovered app-level detector is inherited by name rather than frozen
            # into a copy that cannot follow a policy edit.
            policy=build_policy(cfg, target_app),
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
            # Null when the wording turned out to be the app's own, so the detector resolves
            # from policy at run time. `observed_as` holds what was read off the screen either
            # way, so a null here reads as "inherited", not "nothing found".
            "detector": outcome.detector.value if outcome.detector else None,
            "inherited_from_policy": outcome.detector is None,
            "observed_as": detector,
            "result_fields": {k: v.value for k, v in outcome.result_fields.items()},
            "reference_run": happy.run_id,
            "outcome_run": other.run_id,
        }
    )


@app.command()
def catalog(
    status: Annotated[str, typer.Option()] = "",
    app_name: Annotated[
        str, typer.Option("--app", help="Show only this application's capabilities")
    ] = "",
) -> None:
    """List saved capabilities with their inputs, outputs and outcomes."""
    from .runtime import build_catalog

    caps = build_catalog(settings()).list(Status(status) if status else None, app=app_name or None)
    _echo(
        [
            {
                "ref": c.ref,
                "app": c.app.name,
                "status": c.status.value,
                "goal": c.goal,
                "inputs": {i.name: i.type.value for i in c.inputs},
                "outputs": {o.name: o.type.value for o in c.outputs},
                # Suffixed rather than dropped: a reviewer needs to see which outcomes are
                # only guesses before approving.
                "outcomes": [
                    o.name if o.verified else f"{o.name} (unverified)"
                    for o in c.business_outcomes
                ],
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
def diagnose(
    run_id: Annotated[str, typer.Argument(help="a run that failed or escalated")],
    app_name: Annotated[str, APP_OPT] = "",
) -> None:
    """Propose a declaration for the screen a run stopped on.

    Reads the run's evidence, shows the model the lines that were on the failing screen, and
    asks which kind of condition it is and which line identifies it — an index, never a phrase,
    so a detector it made up is not expressible. The answer is falsified against every
    successful run of the same capability, and what comes out is YAML for a person to paste.

    Nothing here touches a session or a capability, and the patch lands in the application's
    policy rather than in one artifact.
    """
    from .diagnose import diagnose as run_diagnosis
    from .diagnose import load_run, reference_lines
    from .discovery.llm import LLMClient
    from .runtime import build_policy

    cfg = settings()
    root = Path(cfg.evidence_dir)
    evidence_dir = root / run_id
    if not (evidence_dir / "run.json").exists():
        raise typer.BadParameter(f"no evidence for {run_id}")

    run = load_run(evidence_dir)
    if run.status in ("success", "business_outcome"):
        # Nothing stopped, so there is nothing undeclared to name, and no model call to make.
        raise typer.BadParameter(
            f"{run_id} ended as {run.status}; there is nothing undeclared to diagnose"
        )

    policy = build_policy(cfg, app_name or None)
    llm = LLMClient(
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        temperature=cfg.temperature,
        api_base=cfg.api_base,
        fallbacks=cfg.fallback_models,
    )
    reference = reference_lines(root, run.capability, exclude=run_id)
    result = asyncio.run(run_diagnosis(run, policy, llm, reference))

    # Written beside the run it diagnoses, rejections included: those are what shows the
    # falsification ran.
    (evidence_dir / "diagnosis.json").write_text(
        json.dumps(result.to_json(), indent=2) + "\n"
    )
    _echo(result.to_json())
    if result.actionable:
        typer.echo(f"\nAdd to policies/{policy.app}.yaml:\n")
        typer.echo(result.patch)
    elif result.rejected:
        typer.echo(f"\nNo patch proposed: {result.rejected}")
    raise typer.Exit(0 if result.actionable else 1)


@app.command()
def manifest() -> None:
    """The agent-facing tool catalog: approved capabilities as callable tools."""
    from .runtime import build_catalog

    _echo(list(build_catalog(settings()).tool_manifest()))


if __name__ == "__main__":
    app()
