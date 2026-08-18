#!/usr/bin/env python3
"""Print the evidence index as a Markdown table, read from the runs themselves.

`evidence/README.md` used to carry a hand-written table of run ids and what each
one showed. It drifted the moment runs were regenerated, and a stale index is
worse than no index: a reviewer following it finds nothing and stops believing the
rest of the document.

So the table is generated. What each *kind* of run demonstrates is prose a person
writes once; which runs exist and how they ended is read off `run.json`, which is
the same file the caller received.

    python3 scripts/index_evidence.py            # the table
    python3 scripts/index_evidence.py --check    # exit 1 if README names a run that is gone
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "evidence"
_RUN_REF = re.compile(r"`((?:discover|replay|recover|learn)-[a-z0-9-]+)`")


def summarize(run: Path) -> dict[str, str]:
    payload = json.loads((run / "run.json").read_text())
    status = str(payload.get("status", "?"))
    detail = ""
    if outcome := payload.get("outcome"):
        detail = f"outcome `{outcome.get('name', '')}`"
    elif failure := payload.get("failure"):
        detail = f"`{failure.get('kind', '')}` at step {failure.get('step_id', '?')}"
    elif outputs := payload.get("outputs"):
        detail = ", ".join(f"`{k}` = {v}" for k, v in outputs.items())
    elif reason := payload.get("stop_reason"):
        detail = str(reason)
    steps = payload.get("steps") or []
    return {
        "run": run.name,
        "capability": str(payload.get("capability") or payload.get("capability_ref") or "—"),
        "status": status,
        "steps": str(len(steps)),
        "detail": detail[:110],
        "intervention": "yes" if any((run / "intervention").glob("*")) else "",
    }


def rows() -> list[dict[str, str]]:
    return [
        summarize(run.parent)
        for run in sorted(ROOT.glob("*/run.json"))
    ]


def table(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "_No runs in this directory yet._"
    head = "| Run | Capability | Result | Steps | What it produced | Handoff |"
    rule = "|---|---|---|---|---|---|"
    body = [
        f"| `{e['run']}` | `{e['capability']}` | **{e['status']}** | {e['steps']} "
        f"| {e['detail']} | {e['intervention']} |"
        for e in entries
    ]
    return "\n".join([head, rule, *body])


def check() -> int:
    """Every run id the README names must exist. The failure this catches is the
    one that actually happened: runs cleaned up, the index left behind."""
    readme = ROOT / "README.md"
    named = set(_RUN_REF.findall(readme.read_text())) if readme.exists() else set()
    present = {p.parent.name for p in ROOT.glob("*/run.json")}
    missing = sorted(named - present)
    for run in missing:
        print(f"evidence/README.md names {run}, which is not in evidence/", file=sys.stderr)
    return 1 if missing else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sys.exit(check() if args.check else (print(table(rows())) or 0))
