#!/usr/bin/env python3
"""Runtime conditions, against the live application.

    docker compose exec desktop python3 scripts/smoke_recover.py

What happens when the application does something legitimate and inconvenient in the middle of
a replay. One capability — `cap_get_account_balance`, the recorded one — run three times with
a different fault armed, for the three tiers of the taxonomy:

  modal    an interstitial is already on screen. Cleared before the step acts,
           because the demo app's dialog deliberately does not move the page: a
           recorded coordinate still resolves to the right control underneath, so
           a click issued into it is simply eaten. Result: SUCCESS, step
           `recovered`.

  slow     the page the next step needs has not rendered yet. Not a spinner — the
           *previous* page, fully rendered and perfectly stable, which is what a
           server-side delay looks like to a system that reads pixels. Result:
           SUCCESS, with the wait on the step record.

  expired  the session died mid-flow. Signing back in lands on the landing page, so what is
           lost is the run's *place* in the flow and the handler is "sign in and start the
           capability over" — allowed only while nothing irreversible has happened. Every
           step here is safe, so it re-runs itself. Result: SUCCESS, with the restart on the
           record.

           The other half of that gate — an expiry *after* a risky step, which parks for a
           person — is `smoke_escalate.py`.

Faults are armed by driving the automation's own browser through
`/api/faults?set=…` — they live in a cookie, deliberately, so that a reviewer's
tab and the automation's browser do not share them. `/api/faults` and `/dev` are
both outside the app's allowlist: arming a fault is something done *to* the
automation, never by it.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from uuid import uuid4

from cua.catalog import Catalog
from cua.config import settings
from cua.runtime import REGISTRY, build_policy, build_replay, build_session, entry_url
from cua.schema import ReplayResult, RunStatus

CFG = settings()
BASE_URL = entry_url(CFG, build_policy(CFG))
INPUTS = {"member_id": "12345", "account_nickname": "Primary Savings"}

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


async def arm(session: Any, *names: str) -> None:
    """Switch a fault on inside the automation's browser. See the module docstring."""
    await session.driver.navigate(f"{BASE_URL.rstrip('/')}/api/faults?set={','.join(names)}")
    await session.observe()


def report(result: ReplayResult) -> None:
    for s in result.steps:
        note = f"  [{s.note}]" if s.note else ""
        print(
            f"        step {s.step_id} {s.status.value:9} "
            f"attempts={s.attempts} {s.duration_ms:>6}ms{note}"
        )


async def run_once(session: Any, cap: Any, label: str) -> ReplayResult:
    run_id = f"recover-{label}-{uuid4().hex[:6]}"
    engine = build_replay(CFG, session, run_id)
    result = await engine.replay(cap, INPUTS)
    REGISTRY.forget(run_id)
    return result


# ---------------------------------------------------------------------------


async def case_modal(session: Any, cap: Any) -> None:
    step("modal — an interstitial that would eat the click")
    await arm(session, "modal")
    result = await run_once(session, cap, "modal")

    if result.status is not RunStatus.SUCCESS:
        bad(f"expected success, got {result.status.value}: {result.failure}")
    recovered = [s for s in result.steps if s.recovery_applied]
    if not recovered:
        bad("nothing recorded a recovery; was the fault armed?")
    else:
        ok(f"{recovered[0].recovery_applied} cleared at step {recovered[0].step_id}")
        ok(f"outputs {result.outputs} — evidence {result.evidence_dir}")
    report(result)


async def case_slow(session: Any, cap: Any) -> None:
    step("slow — the screen the step needs has not arrived yet")
    await arm(session, "slow")
    result = await run_once(session, cap, "slow")

    if result.status is not RunStatus.SUCCESS:
        bad(f"expected success, got {result.status.value}: {result.failure}")
        report(result)
        return

    waited = [s for s in result.steps if s.note and "waited" in s.note]
    if not waited:
        bad("no step recorded a wait; the delay may not have landed on one")
    else:
        ok(f"step {waited[0].step_id} {waited[0].note}")
        # The point of waiting rather than falling back: the anchor is still the
        # tier that resolved it, so this does not read as drift in the evidence.
        ok(f"resolved via {waited[0].resolution.value} — not a recorded_bbox fallback")
    ok(f"outputs {result.outputs} — evidence {result.evidence_dir}")
    report(result)


async def case_expired(session: Any, cap: Any) -> None:
    step("expired — the run signs back in and starts over")
    await arm(session, "expired")

    run_id = f"recover-expired-{uuid4().hex[:6]}"
    engine = build_replay(CFG, session, run_id)
    control = REGISTRY.get(run_id)          # created by build_replay
    result = await engine.replay(cap, INPUTS)

    if control.intervention is not None:
        bad("the run parked; a read-only capability should recover on its own")
    REGISTRY.forget(run_id)

    restarted = [s for s in result.steps if s.recovery_applied == "session_expired"]
    if restarted:
        ok(f"the session died at step {restarted[0].step_id} and was signed back into")
    else:
        bad("nothing on the record says the session expired — did the fault arm?")

    if len(result.steps) > len(cap.steps):
        # The steps taken before the expiry stay in the log: a run that executed step 2
        # twice says so.
        ok(f"{len(result.steps)} step records for a {len(cap.steps)}-step capability")
    else:
        bad("the capability did not start over")

    if result.status is not RunStatus.SUCCESS:
        bad(f"expected the run to finish on its own, got {result.status.value}")
    else:
        ok(f"finished without waking anyone: outputs {result.outputs}")
        ok(f"evidence {result.evidence_dir}")
    report(result)


async def main() -> int:
    cap = Catalog(CFG.artifacts_dir).load("cap_get_account_balance")
    session = build_session(CFG)

    step("session")
    await session.start()
    await session.authenticate(CFG.target_username, CFG.target_password)
    ok(f"signed in on {CFG.display}, replaying {cap.ref}")

    try:
        await case_modal(session, cap)
        await case_slow(session, cap)
        await case_expired(session, cap)
    finally:
        await arm(session)          # leave the app clean for the next run
        await session.stop()

    step("summary")
    if failures:
        for f in failures:
            print(f"  FAIL  {f}")
        return 1
    ok("all three runtime conditions handled as declared")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
