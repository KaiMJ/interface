#!/usr/bin/env python3
"""Regenerate diagram.html's file:line anchors from the source it cites.

The page points a reader at functions in the backend. Hardcoding their line
numbers means the next refactor silently makes every one of them wrong — which
is exactly what happened between commits 4a2c5cd and 859c1fd, under a caption
claiming the numbers were current. This walks the AST instead and rewrites a
generated block in place, so the page stays a single self-contained file and
`make diagram` is the only thing that has to run.

  scripts/anchors.py           rewrite the block
  scripts/anchors.py --check   exit 1 if it is out of date
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "backend" / "src" / "cua"
PAGE = ROOT / "diagram.html"

BEGIN = "/* @generated:anchors — scripts/anchors.py; do not edit by hand */"
END = "/* @end:anchors */"

# Every symbol the page cites, and every module it counts. A name may be
# qualified (`Catalog.save`) when the bare one appears more than once; an
# ambiguous citation is an error rather than a guess.
CITED: dict[str, list[str]] = {
    "replay/engine.py": [
        "_clear_the_way", "_check_policy", "_wait_until_ready",
        "_resolve", "_run_act", "_run_find_and_act", "_await_effect",
    ],
    "replay/outcomes.py": ["classify", "conditions"],
    "replay/contract.py": ["validate_inputs", "extract_outputs", "check_constraints"],
    "replay/scan.py": ["Scanner.scan"],
    "replay/tenant.py": ["rebase"],
    "resolve/resolver.py": [
        "resolve_traced", "_by_anchor_text", "_by_role_name",
        "_by_recorded_bbox", "_by_vlm", "_narrow",
    ],
    "resolve/verify.py": ["verify_target", "verify_effect"],
    "escalation/control.py": [
        "assert_automation", "park", "take_control", "release", "escalate",
    ],
    "escalation/watch.py": ["HumanActionWatcher.start", "HumanActionWatcher.stop"],
    "runtime/wiring.py": ["build_discovery", "build_replay"],
    "runtime/session.py": [],
    "discovery/loop.py": ["DiscoveryLoop.run", "_step", "_act", "_is_stuck"],
    "discovery/synthesize.py": ["synthesize", "declare", "_falsify", "parameterize"],
    "discovery/actions.py": ["tool_definitions", "to_step"],
    "discovery/llm.py": [],
    "discovery/prompts.py": [],
    "policy/policy.py": ["Policy.decide", "Policy.check_url"],
    "policy/redact.py": [],
    "catalog/store.py": ["Catalog.save", "Catalog.approve", "Catalog.tool_manifest"],
    "catalog/learn.py": [],
    "diagnose.py": ["diagnose", "reference_lines", "_downgrade"],
    "evidence/writer.py": ["EvidenceWriter.frame", "EvidenceWriter.step"],
    "evidence/log.py": [],
    "schema/artifact.py": [],
    "schema/results.py": [],
    "schema/common.py": [],
    "schema/elements.py": [],
    "schema/intervention.py": [],
    "perception/base.py": [],
    "perception/ocr.py": [],
    "perception/merge.py": [],
    "perception/index.py": [],
    "perception/detect.py": [],
    "perception/screen.py": [],
    "perception/som.py": [],
    "action/browser.py": [],
    "action/base.py": [],
    "action/offline.py": [],
    "action/desktop.py": [],
    "resolve/normalize.py": [],
    "resolve/template.py": [],
    "api/routes.py": [],
    "api/main.py": [],
    "cli.py": [],
    "config.py": [],
    "calibration.py": [],
}


def definitions(tree: ast.Module) -> dict[str, list[int]]:
    """Map every function to its line, under both its bare and its qualified
    name, so a citation can disambiguate when it needs to and stay short when
    it does not."""
    found: dict[str, list[int]] = {}

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.setdefault(child.name, []).append(child.lineno)
                if prefix:
                    found.setdefault(f"{prefix}.{child.name}", []).append(child.lineno)
                visit(child, prefix)
            elif isinstance(child, ast.ClassDef):
                visit(child, child.name)
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def collect() -> tuple[dict[str, dict], list[str]]:
    data: dict[str, dict] = {}
    problems: list[str] = []
    for rel, symbols in CITED.items():
        path = SRC / rel
        if not path.exists():
            problems.append(f"{rel}: no such module")
            continue
        text = path.read_text()
        entry: dict[str, object] = {"lines": len(text.splitlines())}
        if symbols:
            table = definitions(ast.parse(text))
            at: dict[str, int] = {}
            for sym in symbols:
                lines = table.get(sym, [])
                if not lines:
                    problems.append(f"{rel}: no def {sym}")
                elif len(lines) > 1:
                    problems.append(f"{rel}: {sym} is ambiguous ({len(lines)} defs) — qualify it")
                else:
                    at[sym.split(".")[-1]] = lines[0]
            entry["at"] = at
        data[rel] = entry
    return data, problems


def block(data: dict[str, dict]) -> str:
    rows = ",\n".join(
        f'  "{rel}": {json.dumps(entry, separators=(",", ":"))}' for rel, entry in data.items()
    )
    return f"{BEGIN}\nconst SRC = {{\n{rows}\n}};\n{END}"


def main() -> int:
    data, problems = collect()
    for p in problems:
        print(f"anchors: {p}", file=sys.stderr)
    if problems:
        return 2

    page = PAGE.read_text()
    if BEGIN not in page or END not in page:
        print(f"anchors: {PAGE.name} has no @generated:anchors block", file=sys.stderr)
        return 2

    head, rest = page.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    updated = head + block(data) + tail
    counted = sum(len(e.get("at", {})) for e in data.values())

    if "--check" in sys.argv:
        if updated != page:
            print("anchors: diagram.html is stale — run `make diagram`", file=sys.stderr)
            return 1
        print(f"anchors: current — {counted} symbols across {len(data)} modules")
        return 0

    PAGE.write_text(updated)
    print(f"anchors: {counted} symbols across {len(data)} modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
