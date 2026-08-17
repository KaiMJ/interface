#!/usr/bin/env python3
"""Replay a capability against the live application, with no model anywhere.

    docker compose exec desktop python3 scripts/smoke_replay.py

The capability here is hand-written rather than recorded, on purpose: this script
answers "does the deterministic path work against the real app", and a recorded
artifact would answer that question and the discovery question at once, so a
failure would not say which half broke.

Three invocations, one per result class the caller has to tell apart:

    member 12345   -> SUCCESS with a typed balance
    member 99999   -> BUSINESS_OUTCOME member_not_found (an answer, not a crash)
    member 77777   -> BUSINESS_OUTCOME permission_denied (also not "not found")

The same artifact, three different results, no code path per case.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from cua.config import settings
from cua.runtime import build_replay, build_session
from cua.schema import (
    Capability,
    ReplayResult,
    RunStatus,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def savings_capability(base_url: str) -> Capability:
    """Read a member's balance. Hand-written, so a failure here is the engine's.

    The same shape synthesis emits: a templated navigate, an extraction anchored
    relative to a label, a typed output, and the two legitimate non-answers this
    screen can produce."""
    text = (FIXTURES / "read_balance.json").read_text().replace("http://targetapp:8080", base_url)
    return Capability.model_validate_json(text)


def describe(result: ReplayResult) -> str:
    if result.status is RunStatus.SUCCESS:
        return f"SUCCESS outputs={result.outputs}"
    if result.status is RunStatus.BUSINESS_OUTCOME and result.outcome:
        return f"BUSINESS_OUTCOME {result.outcome.name} fields={result.outcome.fields}"
    if result.status is RunStatus.FAILURE and result.failure:
        return (
            f"FAILURE {result.failure.kind.value} at step {result.failure.step_id}: "
            f"{result.failure.message} | expected={result.failure.expected!r} "
            f"observed={result.failure.observed!r}"
        )
    return f"{result.status.value} {result.intervention_id or ''}"


async def main() -> int:
    cfg = settings()
    cap = savings_capability(cfg.target_base_url)

    session = build_session(cfg)
    step("session")
    await session.start()
    await session.authenticate(cfg.target_username, cfg.target_password)
    ok(f"signed in as {cfg.target_username!r} on {cfg.display}")

    try:
        for member_id, expect in (
            ("12345", RunStatus.SUCCESS),
            ("99999", RunStatus.BUSINESS_OUTCOME),
            ("44100", RunStatus.BUSINESS_OUTCOME),
        ):
            step(f"replay member {member_id}")
            engine = build_replay(cfg, session, f"replay-{member_id}-{uuid4().hex[:6]}")
            result = await engine.replay(
                cap, {"member_id": member_id, "account_nickname": "Primary Savings"}
            )
            line = describe(result)
            if result.status is expect:
                ok(f"{line}  ({result.duration_ms}ms, evidence {result.evidence_dir})")
            else:
                bad(f"expected {expect.value}, got {line}")

            for s in result.steps:
                print(f"        step {s.step_id} {s.status.value:9} via {s.resolution.value}")
    finally:
        await session.stop()

    print("\n" + "=" * 64)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("replay OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
