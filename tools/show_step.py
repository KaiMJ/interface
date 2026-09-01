#!/usr/bin/env python3
"""What one step was handed and what it produced.

    python3 tools/show_step.py <run-id>            # every step, one line each
    python3 tools/show_step.py <run-id> 4          # step 4 in full
    python3 tools/show_step.py <run-id> 4 --text   # what perception read on that frame

Reads `evidence/<run-id>/` only, so it works on any run that has already happened.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "evidence"


def steps(run: Path):
    return [json.loads(l) for l in (run / "steps.jsonl").read_text().splitlines() if l.strip()]


def summary(run: Path) -> None:
    result = json.loads((run / "run.json").read_text())
    print(f"{run.name}  {result.get('status')}  {result.get('capability') or ''}")
    if result.get("inputs"):
        print(f"  inputs  : {json.dumps(result['inputs'])}")
    if result.get("outputs"):
        print(f"  outputs : {json.dumps(result['outputs'])}")
    if result.get("outcome"):
        print(f"  outcome : {json.dumps(result['outcome'])}")
    if result.get("failure"):
        print(f"  failure : {json.dumps(result['failure'])}")
    print()
    for d in steps(run):
        print(f"{d['step_id']:>3} {d['status']:<10} via={str(d.get('resolution')):<13}"
              f" {d.get('duration_ms') or 0:>6}ms  {d['intent'][:60]}")


def detail(run: Path, step_id: int, show_text: bool) -> None:
    for d in steps(run):
        if d["step_id"] != step_id:
            continue
        print(f"=== step {step_id} — {d['status']}\n")
        print(f"INTENT      {d['intent']}")
        print(f"EXPECTED    {d.get('expected')}")
        print(f"OBSERVED    {str(d.get('observed'))[:300]}")
        print(f"SETTLED BY  {d.get('settled_by')}   attempts={d.get('attempts')}")
        if d.get("note"):
            print(f"NOTE        {d['note']}")
        if d.get("recovery_applied"):
            print(f"RECOVERY    {d['recovery_applied']}")
        print(f"\nGUARDRAIL   {json.dumps(d.get('policy'), indent=2)}")
        print(f"\nRESOLUTION  {json.dumps(d.get('resolution_trace'), indent=2)}")
        if d.get("model_turn"):
            turn = {k: v for k, v in d["model_turn"].items() if k != "prompt"}
            print(f"\nMODEL TURN  {json.dumps(turn, indent=2)}")
            print("\n(the full prompt is in model_turn.prompt)")
        print(f"\nCOST        {json.dumps(d.get('phases'))}")
        print(f"\nFRAMES      {json.dumps(d.get('evidence'), indent=2)}")
        if show_text:
            for which in (f"step-{step_id:02d}.json", f"step-{step_id:02d}.after.json"):
                path = run / "observations" / which
                if not path.exists():
                    continue
                els = json.loads(path.read_text())["elements"]
                print(f"\n--- {which}: {len(els)} elements perception found")
                for e in els:
                    label = (e.get("text") or e.get("name") or "").strip()
                    if label:
                        b = e["bbox"]
                        print(f"  {e['id']:<6} {e.get('role','') :<9} "
                              f"({b['x']:.3f},{b['y']:.3f})  {label[:60]}")
        return
    print(f"no step {step_id} in {run.name}", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    run_dir = ROOT / sys.argv[1]
    if not run_dir.exists():
        print(f"no run {sys.argv[1]} in evidence/", file=sys.stderr)
        raise SystemExit(1)
    if len(sys.argv) == 2:
        summary(run_dir)
    else:
        detail(run_dir, int(sys.argv[2]), "--text" in sys.argv)
