#!/usr/bin/env python3
"""`find_and_act` against a real list.

    docker compose exec desktop python3 scripts/smoke_scan.py

The step type the design leans on hardest, and the one that is easiest to believe
without checking. A member's transaction history is 22 rows today and may be 3
tomorrow, so the row a caller wants has no stable position — recording
`scroll, scroll, click(y)` is wrong four separate ways and recording the predicate
is right in all four.

What this exercises, in the order the failures matter:

  1. the scope is located by anchor text (a column header), not a recorded box
  2. rows are reconstructed from text boxes — a table row does not exist in pixels
  3. the predicate is evaluated with the artifact's own normalizers
  4. a named column is read out of the matched row, so the caller gets an amount
     rather than a row of text they have to parse
  5. exhausting the list without a match is a *business outcome*, not a failure
  6. several matches invoke the artifact's declared policy rather than a guess

Run against the live app, which is the only way any of this is proven: the scan
loop has no idea it is being tested.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from cua.config import settings
from cua.runtime import build_replay, build_session
from cua.schema import Capability, MultiplePolicy, RunStatus

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def capability(base_url: str) -> Capability:
    text = (FIXTURES / "find_transaction.json").read_text().replace(
        "http://targetapp:8080", base_url
    )
    return Capability.model_validate_json(text)


async def main() -> int:
    cfg = settings()
    cap = capability(cfg.target_base_url)

    session = build_session(cfg)
    step("session")
    await session.start()
    await session.authenticate(cfg.target_username, cfg.target_password)
    ok(f"signed in on {cfg.display}")

    async def run(label: str, merchant: str, artifact: Capability = cap) -> object:
        engine = build_replay(cfg, session, f"scan-{label}-{uuid4().hex[:6]}")
        return await engine.replay(artifact, {"member_id": "12345", "merchant": merchant})

    try:
        # -------------------------------------------------------------------
        step("a row that appears exactly once")
        # PACIFIC WIRELESS is one row of 22, below the fold on a 900px display.
        # Its amount is a parenthesised debit, which is also the string OCR is
        # most likely to mangle.
        result = await run("unique", "PACIFIC WIRELESS")
        if result.status is RunStatus.SUCCESS:
            ok(f"{result.outputs} in {result.duration_ms}ms")
            amount = result.outputs.get("amount")
            if not isinstance(amount, float) or amount >= 0:
                bad(f"expected a negative number for a debit, got {amount!r}")
        else:
            bad(f"expected success, got {result.status.value}: {result.failure}")

        # -------------------------------------------------------------------
        step("a row that is not there")
        result = await run("missing", "NORTHWIND TRADING CO")
        if result.status is RunStatus.BUSINESS_OUTCOME and result.outcome:
            # The distinction the brief singles out: the scan saw the whole list
            # and the record is absent. That is an answer.
            ok(f"{result.outcome.name} fields={result.outcome.fields}")
        else:
            bad(f"expected transaction_not_found, got {result.status.value}")

        # -------------------------------------------------------------------
        step("a merchant on four rows")
        # HARBORVIEW PROPERTY MGMT appears four times. Under `first` this is a
        # legitimate read; the point is that the artifact decides, not the engine.
        result = await run("ambiguous", "HARBORVIEW PROPERTY MGMT")
        if result.status is RunStatus.SUCCESS:
            ok(f"policy=first -> {result.outputs}")
        else:
            bad(f"expected a first-match read, got {result.status.value}: {result.failure}")

        step("the same ambiguity, on a capability that says escalate")
        # Same screen, same predicate, different declared policy. On a write task
        # acting on the wrong record is unrecoverable, so the run stops.
        escalating = cap.model_copy(
            update={
                "steps": [
                    cap.steps[0],
                    cap.steps[1].model_copy(update={"on_multiple": MultiplePolicy.ESCALATE}),
                ]
            }
        )
        task = asyncio.create_task(run("escalating", "HARBORVIEW PROPERTY MGMT", escalating))
        from cua.runtime import REGISTRY

        for _ in range(600):
            pending = REGISTRY.pending()
            if pending:
                ok(f"raised {pending[0].reason.value}: {pending[0].message}")
                control = REGISTRY.by_intervention(pending[0].id)
                control.take_control("smoke-operator")
                from cua.clock import now_iso
                from cua.schema import InterventionResolution

                control.release(
                    InterventionResolution(
                        id=pending[0].id,
                        outcome="abort",
                        operator="smoke-operator",
                        note="four rows match; a person picks",
                        resolved_at=now_iso(),
                    )
                )
                break
            await asyncio.sleep(0.1)
        else:
            bad("no intervention was raised for an ambiguous match")
        result = await task
        if result.status is RunStatus.ESCALATED:
            ok("escalated rather than picking a row")
        else:
            bad(f"expected escalated, got {result.status.value}")

    finally:
        await session.stop()

    print("\n" + "=" * 64)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("scan OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
