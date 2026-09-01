"""Learning a business outcome by demonstrating one.

Synthesis asks the model to name the alternative results a caller branches on, but those are
screens the successful recording never visited, so its wording is a guess. Nothing here asks a
model about a screen it did not see: the detector comes from the difference between a run that
succeeded and a run that did not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..resolve import unrender
from ..schema import (
    BusinessOutcome,
    Capability,
    CheckKind,
    Checkpoint,
    Status,
    ValueType,
)

# Both `step-04.json` (what the step acted on) and `step-04.after.json` (what the
# application showed once it had). The second is where an outcome screen lives.
_STEP_FILE = re.compile(r"step-(\d+)(\.after)?\.json$")


class NothingToLearn(RuntimeError):
    """The two runs did not differ in any readable way.

    Either they reached the same screen, or the difference is not expressible as text — an
    icon, a colour, a disabled button. Neither should produce a detector.
    """


def _observations(evidence_dir: Path) -> list[Path]:
    """Every observation in the order the run made them, `after` frames included.

    A run that stops because the screen changed has the sentence explaining why on the *after*
    frame; without those, the comparison sees only the frame before the click and the record's
    own data.
    """
    return sorted(
        (p for p in (Path(evidence_dir) / "observations").glob("step-*.json")),
        key=lambda p: (
            (int(m.group(1)), 1 if m.group(2) else 0)
            if (m := _STEP_FILE.search(p.name))
            else (0, 0)
        ),
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

    The last observation is the `after` frame of the last step when there is one: the screen
    the run ended on, not the one it was looking at when it acted. Read from evidence, so this
    works on any run already on disk.
    """
    observations = _observations(evidence_dir)
    return _lines(observations[-1]) if observations else []


def all_lines(evidence_dir: Path) -> list[str]:
    """Every line a run read, on every screen it passed through.

    The reference side of the comparison: final frames alone would let text both runs passed
    through, such as a search page's hint, read as the distinguishing line.
    """
    return [line for path in _observations(evidence_dir) for line in _lines(path)]


def distinguishing_text(reference: Sequence[str], outcome: Sequence[str]) -> str:
    """The line that says this run ended differently.

    Longest line present in `outcome` and absent from `reference`: a screen announcing a result
    says so in a sentence, while shared chrome is short. Compared case- and
    whitespace-insensitively; returned as rendered.
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
    policy: Any = None,
) -> Capability:
    """A new draft version declaring one more business outcome.

    A version rather than an edit: what production is calling stays approved, and the v1/v2
    diff is what a reviewer reads.

    If the app policy already declares this outcome and we rediscovered its wording, the entry
    is recorded *by name* so the detector is inherited. A copy would opt out of the policy's
    `result_fields` and would not follow a later fix to its wording.
    """
    parameterized = unrender(detector_text, dict(inputs)) or detector_text

    # Containment, not equality: what was read off the screen is a whole OCR line and a policy
    # detector is usually the stable clause within it. If the app's own detector would match
    # this screen, the app owns the wording and this capability opts in by name.
    inherited = policy.outcome(name) if policy is not None else None
    if inherited is not None and normalized_line(inherited.detector_value) in normalized_line(
        parameterized
    ):
        outcome = BusinessOutcome(
            name=name,
            description=description or inherited.description,
            detector=None,
            result_fields={},
        )
        remaining = [o for o in cap.business_outcomes if o.name != name]
        return cap.model_copy(
            update={
                "version": version if version is not None else cap.version + 1,
                "status": Status.DRAFT,
                "business_outcomes": [*remaining, outcome],
            }
        )

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
