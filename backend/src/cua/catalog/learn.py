"""Learning a business outcome by demonstrating one.

Synthesis asks the model to name the alternative results a caller branches on, but those
are screens the successful recording never visited — so it guesses the wording, and on
the real run proposed a column header present on every screen of the flow. Nothing here
asks a model to be right about a screen it did not see: the detector comes from the
difference between a run that succeeded and a run that did not.
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

    The right reference side, and getting it wrong is instructive: comparing final
    frames alone taught the first attempt that the search page's hint text meant
    "member not found". Both runs passed through that page.
    """
    return [line for path in _observations(evidence_dir) for line in _lines(path)]


def distinguishing_text(reference: Sequence[str], outcome: Sequence[str]) -> str:
    """The line that says this run ended differently.

    Longest line present in `outcome` and absent from `reference`. Longest because a
    screen announcing a result says so in a sentence while shared chrome is short,
    and because the alternative is a scoring function nobody can defend. Compared
    case- and whitespace-insensitively; returned as rendered.
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
    """A new draft version declaring one more business outcome.

    A version rather than an edit: what production is calling stays approved, and
    the v1/v2 diff is what a reviewer reads.
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
            # Back to draft: approval is a statement about the whole contract.
            "status": Status.DRAFT,
            "business_outcomes": [*remaining, outcome],
        }
    )


def normalized_line(line: str) -> str:
    """How two lines of screen text are compared for identity."""
    return " ".join(line.casefold().split())
