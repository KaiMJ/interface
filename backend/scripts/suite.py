#!/usr/bin/env python3
"""Record one capability, then run it at everything the app can do to it.

    docker compose exec desktop python3 scripts/suite.py
    docker compose exec desktop python3 scripts/suite.py --only generalize
    docker compose exec desktop python3 scripts/suite.py --skip discover

Three cases are *supposed* to come back as something other than success. What it produces is
a filled `artifacts/` and `evidence/` and one table saying which run demonstrates which claim.

Four groups, answering different questions:

  discover    can the flow be recorded at all, from a live screen
  generalize  does the recording hold for records it never saw
  outcomes    are legitimate non-answers reported as answers rather than crashes
  faults      do the declared recoveries fire, and does an app error stay an error

Exit code is the number of cases whose result was not the one declared, so CI can
call this without parsing anything.

The declared results assume a capability that has been *taught* its outcomes. A
recording cannot confirm a business-outcome detector — it describes a screen the
successful run never reached — so a freshly recorded artifact declares none that
fire, and `--fresh` therefore reports the outcome cases as misses until:

    cua learn-outcome cap_get_account_balance --name member_not_found \
        --description "no member exists with that id" --input member_id=99999
    cua learn-outcome cap_get_account_balance --name permission_denied \
        --description "the operator may not view this member" \
        --input member_id=44100 --input account_nickname="Business Operating"

That is the intended order: record, demonstrate, then measure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CAP = "cap_get_account_balance"

# Read off targetapp/lib/data.ts. Every member's *own* accounts: an account belonging to a
# different member is a bad test input, not a finding.
MEMBERS: dict[str, list[str]] = {
    "12345": ["Everyday Checking", "Primary Savings", "MM Reserve"],
    "22841": ["Free Checking", "Rainy Day"],
    "30992": ["Basic Checking"],
    "44100": ["Business Operating", "Business Reserve"],
    "57310": ["Joint Checking", "Vacation Fund", "18mo CD"],
}

# `cua replay` maps the result taxonomy onto exit codes on purpose, so a shell
# caller sees the same distinction the artifact declares.
EXIT = {0: "success-or-outcome", 1: "failure", 2: "escalated"}


@dataclass
class Case:
    group: str
    name: str
    why: str                      # the claim this case is evidence for
    argv: list[str]
    expect: str                   # the declared result: what "working" means here
    faults: list[str] = field(default_factory=list)


@dataclass
class Result:
    case: Case
    code: int
    run_id: str
    status: str
    detail: str

    @property
    def met(self) -> bool:
        return self.status == self.case.expect


def cases() -> list[Case]:
    out = [
        Case(
            group="discover",
            name="record the balance flow",
            why="a live screen becomes a typed artifact with no data baked into it",
            argv=[
                "discover",
                "--goal",
                "Read current balance for member 12345",
                "--input",
                "member_id=12345",
                "--input",
                "account_nickname=Primary Savings",
                "--capability-id",
                CAP,
            ],
            expect="success",
        )
    ]

    # Every member against one of their own accounts, plus a second account for the
    # members that have one. The recording saw exactly one of these.
    for member, accounts in MEMBERS.items():
        for account in accounts[:2]:
            out.append(
                Case(
                    group="generalize",
                    name=f"{member} · {account}",
                    why="the recording holds for a record it never saw",
                    argv=[
                        "replay",
                        CAP,
                        "--input",
                        f"member_id={member}",
                        "--input",
                        f"account_nickname={account}",
                    ],
                    # 44100 is the restricted-member fixture: a legitimate answer, but only
                    # once the capability names `permission_denied`. On a freshly recorded
                    # artifact it is still a hard failure and the suite says so.
                    expect="business_outcome" if member == "44100" else "success",
                )
            )

    out += [
        Case(
            group="outcomes",
            name="unknown member",
            why="an absent record is an answer, not a crash",
            argv=[
                "replay",
                CAP,
                "--input",
                "member_id=99999",
                "--input",
                "account_nickname=Checking",
            ],
            expect="business_outcome",
        ),
        Case(
            group="outcomes",
            name="unknown account on a real member",
            why="the row is missing, not the member — a different answer again",
            argv=[
                "replay",
                CAP,
                "--input",
                "member_id=12345",
                "--input",
                "account_nickname=Nonexistent Account",
            ],
            # No outcome is declared for this yet, so it is a hard failure today.
            expect="failure",
        ),
    ]

    # One fault per guard it is meant to exercise. All replay the same capability
    # with the same inputs, so the only variable is the fault.
    for fault, why, expect in (
        ("banner", "content pushed down — anchors hold where a recorded box would not", "success"),
        ("modal", "an unexpected dialog is cleared by a declared recovery", "success"),
        ("slow", "a 4s delay is waited out by polling a checkpoint, not by sleeping", "success"),
        ("expired", "a session bounce is recovered, or escalated if it cannot be", "success"),
        ("denied", "a declared outcome the capability has opted into is an answer",
         "business_outcome"),
        ("error500", "an application error is the app's fault and still not an answer", "failure"),
    ):
        out.append(
            Case(
                group="faults",
                name=fault,
                why=why,
                argv=[
                    "replay",
                    CAP,
                    "--input",
                    "member_id=12345",
                    "--input",
                    "account_nickname=Primary Savings",
                ],
                expect=expect,
                faults=[fault],
            )
        )
    return out


def _result_json(stdout: str) -> dict[str, Any] | None:
    """The result object the CLI printed, out of whatever else landed on stdout.

    `cli._echo` pretty-prints with `indent=2`, so the object spans many lines and a
    line-at-a-time parse never sees valid JSON. Torch and the OCR stack also write to stdout,
    so the object is found rather than assumed to be the whole of it.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(stdout):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stdout[i:])
        except ValueError:
            continue
        if isinstance(value, dict) and ("status" in value or "run_id" in value):
            return value
    return None


def run(case: Case, evidence: Path) -> Result:
    argv = ["cua", *case.argv]
    for fault in case.faults:
        argv += ["--fault", fault]

    began = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True)
    took = time.monotonic() - began

    payload = _result_json(proc.stdout)
    if payload is None:
        # No result object means the CLI did not get far enough to be handed one — a refused
        # contract, an existing artifact, a crash. The last stderr line is the closest thing
        # to a reason.
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        return Result(case, proc.returncode, "", "error", tail[-1] if tail else "")

    status = str(payload.get("status") or "unknown")
    run_id = str(payload.get("run_id") or "")
    failure = payload.get("failure") or {}
    outcome = payload.get("outcome") or {}
    detail = str(failure.get("message") or outcome.get("name") or "")
    outputs = payload.get("outputs")
    if outputs:
        detail = f"{json.dumps(outputs)} {detail}".strip()

    if run_id and not (evidence / run_id).exists():
        detail = f"{detail} (no evidence written)".strip()
    return Result(case, proc.returncode, run_id, status, f"{detail} [{took:.0f}s]".strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", action="append", help="run only these groups, repeatable")
    ap.add_argument("--skip", action="append", help="skip these groups, repeatable")
    ap.add_argument("--evidence", default="/data/evidence")
    ap.add_argument("--artifacts", default="/data/artifacts")
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="re-record: remove this suite's own artifact first (the catalog "
        "refuses to overwrite, on purpose — this says you meant it)",
    )
    ap.add_argument("--json", default="", help="also write the table here as JSON")
    args = ap.parse_args()

    # The catalog will not replace a capability production may be calling, so a second suite
    # run cannot re-record. The replay cases below are about the artifact that exists.
    existing = sorted(Path(args.artifacts).glob(f"{CAP}.v*.json"))
    if existing and args.fresh:
        for path in existing:
            path.unlink()
        existing = []

    selected = [
        c
        for c in cases()
        if (not args.only or c.group in args.only) and c.group not in (args.skip or ())
    ]
    evidence = Path(args.evidence)

    results: list[Result] = []
    group = ""
    for case in selected:
        if case.group != group:
            group = case.group
            print(f"\n── {group} " + "─" * (58 - len(group)), flush=True)
        print(f"  {case.name:38} ", end="", flush=True)
        if case.group == "discover" and existing:
            print(
                f"-- skipped            {existing[0].name} exists; "
                f"--fresh to re-record",
                flush=True,
            )
            continue
        result = run(case, evidence)
        results.append(result)
        mark = "ok " if result.met else "!! "
        print(f"{mark}{result.status:18} {result.detail}", flush=True)

    missed = [r for r in results if not r.met]
    print(f"\n{len(results) - len(missed)}/{len(results)} cases matched their declared result")
    for r in missed:
        print(f"  !! {r.case.group}/{r.case.name}: expected {r.case.expect}, got {r.status}")

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                [
                    {
                        "group": r.case.group,
                        "case": r.case.name,
                        "demonstrates": r.case.why,
                        "expected": r.case.expect,
                        "status": r.status,
                        "run_id": r.run_id,
                        "met": r.met,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
        print(f"wrote {args.json}")
    return len(missed)


if __name__ == "__main__":
    sys.exit(main())
