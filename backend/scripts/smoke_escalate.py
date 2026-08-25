#!/usr/bin/env python3
"""The human handoff, against the live session.

    docker compose exec desktop python3 scripts/smoke_escalate.py

A write capability that moves money. Its confirm step is `risky`, the app policy's
disposition for risky is `confirm`, so the run stops before pressing the one
button in this application that cannot be taken back — and waits for a person.

What this exercises is the part §3.6 asks for and the part that is easy to fake:

  1. the run parks on the *same* session, browser alive, form filled, nothing
     torn down
  2. control is a token with exactly one holder, and between the automation
     stopping and the operator arriving it is held by nobody — which is what makes
     "the agent clicked while I was typing" impossible rather than unlikely
  3. the driver refuses to act while a human holds it, checked here by trying
  4. what the operator did is captured at the X layer, not by asking them
  5. control comes back and the run finishes on the same session

The operator is simulated in-process here, doing exactly what the console's
buttons do over HTTP: `POST /interventions/{id}/take` then `/resolve`.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from cua.clock import now_iso
from cua.config import settings
from cua.escalation import ControlError, HumanActionWatcher
from cua.runtime import REGISTRY, build_policy, build_replay, build_session, entry_url
from cua.schema import (
    Capability,
    Controller,
    InterventionResolution,
    Point,
    RunStatus,
)

# Where this deployment's install of the app lives — from its policy, or the
# CUA_TARGET_BASE_URL override. One answer, the same one every command uses.
BASE_URL = entry_url(settings(), build_policy(settings()))

CAPABILITIES = Path(__file__).resolve().parent / "smoke_capabilities"

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def transfer_capability(base_url: str) -> Capability:
    """Move money between two of a member's accounts. Hand-written.

    Step 1 goes straight to the review screen with the transfer's parameters. The
    form before it is three `<select>` elements, and driving those is a perception
    problem worth solving separately from the escalation mechanism this script
    exists to prove."""
    text = (CAPABILITIES / "transfer_funds.json").read_text()
    text = text.replace("http://targetapp:8080", base_url)
    return Capability.model_validate_json(text)


async def wait_for_intervention(run_id: str, seconds: float = 120.0) -> object:
    control = REGISTRY.get(run_id)
    for _ in range(int(seconds * 10)):
        if control.intervention is not None:
            return control
        await asyncio.sleep(0.1)
    raise TimeoutError("no intervention was raised")


async def main() -> int:
    cfg = settings()
    cap = transfer_capability(BASE_URL)
    inputs = {
        "member_id": "12345",
        "from_account": "29883",
        "to_account": "13455",
        "amount": "25.00",
    }

    session = build_session(cfg)
    step("session")
    await session.start()
    await session.authenticate(cfg.target_username, cfg.target_password)
    ok(f"signed in on {cfg.display}")

    run_id = f"escalate-{uuid4().hex[:6]}"
    engine = build_replay(cfg, session, run_id)
    run = asyncio.create_task(engine.replay(cap, inputs))

    try:
        # -------------------------------------------------------------------
        step("the guardrail fires")
        control = await wait_for_intervention(run_id)
        request = control.intervention
        ok(f"intervention {request.id}: {request.reason.value}")
        ok(f"  step {request.step_id}: {request.step_intent}")
        ok(f"  operator connects at {request.vnc_url}")
        if control.holder is not Controller.NOBODY:
            bad(f"control is held by {control.holder.value}, expected nobody")
        else:
            ok("control is held by nobody until an operator claims it")

        # -------------------------------------------------------------------
        step("the operator takes the live session")
        control.take_control("smoke-operator")
        watcher = HumanActionWatcher(cfg.display)
        watcher.start()
        if watcher.unavailable:
            bad(f"X-layer capture unavailable: {watcher.unavailable}")
        else:
            ok("X-layer capture running")
        watcher.snapshot(
            Path(cfg.evidence_dir) / run_id / "intervention" / "handoff.png", "handoff"
        )

        try:
            await session.driver.click(Point(x=0.5, y=0.5))
            bad("the automation clicked while a human held control")
        except ControlError:
            ok("the automation is refused while a human holds control")

        # A person looking at the screen: pointer moves and a click on empty space.
        # xdotool is the stand-in for a hand on the mouse. Deliberately away from
        # the controls — this script is proving that the handoff is recorded, and
        # an operator who confirms the transfer themselves would leave the
        # automation clicking a page that has already moved on.
        for x, y in ((900, 250), (1250, 700), (1320, 820)):
            subprocess.run(
                ["xdotool", "mousemove", str(x), str(y)],
                check=False,
                env={"DISPLAY": cfg.display, "PATH": "/usr/bin:/bin"},
            )
            await asyncio.sleep(0.15)
        subprocess.run(
            ["xdotool", "mousemove", "1350", "840", "click", "1"],
            check=False,
            env={"DISPLAY": cfg.display, "PATH": "/usr/bin:/bin"},
        )
        await asyncio.sleep(0.4)

        # -------------------------------------------------------------------
        step("the operator hands back")
        actions = watcher.stop()
        watcher.snapshot(
            Path(cfg.evidence_dir) / run_id / "intervention" / "handback.png", "handback"
        )
        kinds = sorted({a.kind for a in actions})
        if actions:
            ok(f"captured {len(actions)} operator actions {kinds}")
            typed = [a for a in actions if a.kind == "key"]
            if any((a.detail or "").strip().startswith("typed:") for a in typed):
                bad("a keystroke's content reached the audit log")
            else:
                ok("keystrokes are counted, never captured")
        else:
            bad("nothing was captured at the X layer")

        control.release(
            InterventionResolution(
                id=request.id,
                outcome="resume",
                operator="smoke-operator",
                note="verified with the member by phone",
                human_actions=actions,
                resolved_at=now_iso(),
            )
        )
        if control.holder is not Controller.AUTOMATION:
            bad(f"control did not come back: holder is {control.holder.value}")
        else:
            ok("control returned to the automation")

        # -------------------------------------------------------------------
        step("the run finishes on the same session")
        result = await run
        if result.status is RunStatus.SUCCESS:
            ok(f"{result.status.value} in {result.duration_ms}ms — evidence {result.evidence_dir}")
        else:
            bad(f"expected success after resume, got {result.status.value}: {result.failure}")
        for s in result.steps:
            print(f"        step {s.step_id} {s.status.value:9} {s.intent}")

        artefacts = sorted(p.name for p in (Path(result.evidence_dir) / "intervention").glob("*"))
        ok(f"intervention evidence: {artefacts}")

    finally:
        if not run.done():
            run.cancel()
        await session.stop()

    print("\n" + "=" * 64)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("escalation OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
