#!/usr/bin/env python3
"""Write the evidence index into `evidence/README.md`, read from the runs themselves.

What each *kind* of run demonstrates is prose a person writes once; which runs exist and how
they ended is read off `run.json`, the same file the caller received. A hand-written table
goes stale the first time the runs are regenerated.

    python3 scripts/index_evidence.py            # rewrite the table in evidence/README.md
    python3 scripts/index_evidence.py --check    # exit 1 if that table and evidence/ disagree
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "evidence"
# Every prefix a run id can carry; a missing one silently exempts those runs from the check.
_RUN_REF = re.compile(r"`((?:discover|replay|recover|learn|escalate|scan|offline)-[a-z0-9-]+)`")


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


_BEGIN = "<!-- BEGIN INDEX -->"
_END = "<!-- END INDEX -->"


def write() -> int:
    """Replace whatever is between the markers with the current table. Written rather than
    printed, so the file cannot be left claiming there are no runs."""
    readme = ROOT / "README.md"
    text = readme.read_text()
    if _BEGIN not in text or _END not in text:
        print(f"evidence/README.md has no {_BEGIN} / {_END} markers", file=sys.stderr)
        return 1
    head, _, rest = text.partition(_BEGIN)
    _, _, tail = rest.partition(_END)
    readme.write_text(f"{head}{_BEGIN}\n{table(rows())}\n{_END}{tail}")
    print(f"wrote {len(rows())} runs into evidence/README.md")
    return 0


def check() -> int:
    """The index and the directory must agree, in both directions: naming a run that is gone,
    and naming nothing while the runs are all there, are both misleading."""
    readme = ROOT / "README.md"
    named = set(_RUN_REF.findall(readme.read_text())) if readme.exists() else set()
    present = {p.parent.name for p in ROOT.glob("*/run.json")}
    problems = 0
    for run in sorted(named - present):
        print(f"evidence/README.md names {run}, which is not in evidence/", file=sys.stderr)
        problems += 1
    for run in sorted(present - named):
        print(f"evidence/{run} exists but the index does not name it", file=sys.stderr)
        problems += 1
    if problems:
        print("run: python3 backend/scripts/index_evidence.py", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sys.exit(check() if args.check else write())
