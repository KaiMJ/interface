"""Learning a business outcome by demonstrating one.

Synthesis asks the model to name the legitimate alternative results a caller must
branch on — "no such member", "not entitled to view this record". Those are
screens the successful recording never visited, so the model is guessing their
wording, and a detector built on guessed wording is worse than no detector at all.
Measured on the real recording run: it proposed `"Accounts"` as the detector for
`account_not_found`, which is a column header on every screen of the flow. Every
success would have been reported as a business outcome.

Two mechanisms, and neither of them asks a model to be right about a screen it did
not see.

**Falsify what was proposed.** A detector cannot be positively validated — the
screen was not visited — but it can be refuted. If the phrase appears on any frame
the successful run passed through, it is not a detector for an alternative
outcome. `discovery.synthesize` drops those and records why.

**Learn the wording from a run that reaches the screen.** Replay the capability
twice, once with the inputs it was recorded with and once with inputs that reach
the other result, and difference the two final frames. Lines present only in the
second are that outcome's signature. No model, no guess: the phrase is copied off
the screen that produces it, then parameterized so it describes the capability
rather than one run.

The pieces here are pure functions over evidence directories, so the mechanism is
testable without a browser and works on evidence already on disk.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from ..resolve import unrender
from ..schema import (
    BusinessOutcome,
    Capability,
    CheckKind,
    Checkpoint,
    Status,
    ValueType,
)

_STEP_FILE = re.compile(r"step-(\d+)\.json$")


class NothingToLearn(RuntimeError):
    """The two runs did not differ in any readable way.

    Either they reached the same screen, or the difference is not expressible as
    text — an icon, a colour, a disabled button. Both are honest answers and
    neither should produce a detector.
    """


def _observations(evidence_dir: Path) -> list[Path]:
    return sorted(
        (p for p in (Path(evidence_dir) / "observations").glob("step-*.json")),
        key=lambda p: int(m.group(1)) if (m := _STEP_FILE.search(p.name)) else 0,
    )


def _lines(path: Path) -> list[str]:
    payload = json.loads(path.read_text())
    return [
        text
        for element in payload.get("elements", ())
        if (text := str(element.get("text") or element.get("name") or "").strip())
    ]


def final_lines(evidence_dir: Path) -> list[str]:
    """The readable text of a run's last observation — where it ended up.

    Reads the evidence rather than holding onto an observation, so this works on
    any run that has already happened, including one from last week.
    """
    observations = _observations(evidence_dir)
    return _lines(observations[-1]) if observations else []


def all_lines(evidence_dir: Path) -> list[str]:
    """Every line a run read, on every screen it passed through.

    This is the right reference side of the comparison, and getting it wrong is
    instructive: comparing final frames alone taught the first attempt that
    "Try 12345, 22841, 30992, 44100, 57310 — or a surname." meant "member not
    found". It is the search page's hint text. The successful run *passed through*
    that page on its way to the profile, so the phrase says nothing about how a
    run ended — only about where both of them went."""
    return [line for path in _observations(evidence_dir) for line in _lines(path)]


def distinguishing_text(reference: Sequence[str], outcome: Sequence[str]) -> str:
    """The line that says this run ended differently.

    `reference` is every line the successful run read anywhere; `outcome` is the
    screen the other run ended on. Anything both runs saw is shared furniture.

    Longest line present in the outcome run and absent from the reference run.
    Longest because a screen announcing a different result says so in a sentence,
    while the chrome it shares with every other screen is short — and because the
    alternative is a scoring function with weights nobody can defend.

    Comparison is case- and whitespace-insensitive; the returned text is the
    original, because a detector has to match what is actually rendered.
    """
    seen = {normalized_line(line) for line in reference}
    only = [line for line in outcome if normalized_line(line) and normalized_line(line) not in seen]
    if not only:
        raise NothingToLearn(
            "the two runs' final screens read the same; there is nothing to detect"
        )
    return max(only, key=len)


def with_outcome(
    cap: Capability,
    name: str,
    description: str,
    detector_text: str,
    inputs: dict[str, object],
    version: int | None = None,
) -> Capability:
    """A new draft version of `cap` that declares one more business outcome.

    A new version rather than an edit: the version production is calling keeps
    working and stays approved, and the diff between v1 and v2 is what a reviewer
    reads to decide whether the new outcome is real.
    """
    parameterized = unrender(detector_text, dict(inputs)) or detector_text
    declared = {spec.name: spec.type for spec in cap.inputs}
    fields = {
        input_name: declared.get(input_name, ValueType.STRING)
        for input_name in declared
        if f"{{{{{input_name}}}}}" in parameterized
    }

    outcome = BusinessOutcome(
        name=name,
        description=description,
        detector=Checkpoint(kind=CheckKind.TEXT_PRESENT, value=parameterized),
        result_fields=fields,
    )
    remaining = [o for o in cap.business_outcomes if o.name != name]
    return cap.model_copy(
        update={
            "version": version if version is not None else cap.version + 1,
            # Back to draft: the capability now claims something a human has not
            # reviewed, and approval is a statement about the whole contract.
            "status": Status.DRAFT,
            "business_outcomes": [*remaining, outcome],
        }
    )


def normalized_line(line: str) -> str:
    """How two lines of screen text are compared for identity.

    Shared with screen derivation, which asks the same question of a different set
    of frames: is this line one I have seen elsewhere?
    """
    return " ".join(line.casefold().split())
