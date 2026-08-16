"""Classifying what we are looking at.

One function, called after every step, that decides which of the four classes the
current frame belongs to. Keeping it in one place is what stops the taxonomy from
being re-litigated at each call site — the most common way a clean error model
rots.

    BUSINESS_OUTCOME  declared per capability. Checked FIRST: "no such member" is
                      an answer the caller branches on, and if the checkpoint check
                      ran first it would be reported as a checkpoint failure.
    RECOVERABLE       declared per app policy. Apply the handler, re-observe, retry
                      the step. Bounded by max_per_run.
    OK                the step's checkpoint holds.
    FAILURE           everything else.

That last line is the design decision, not a gap. Under vision-first perception we
cannot enumerate every screen an enterprise app can produce. A system that guesses
at an unrecognized state in a banking application is worse than one that stops and
says what it saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..schema import Capability, Observation


class Classification(str, Enum):
    OK = "ok"
    BUSINESS_OUTCOME = "business_outcome"
    RECOVERABLE = "recoverable"
    FAILURE = "failure"


@dataclass(frozen=True)
class Classified:
    kind: Classification
    name: str | None = None              # outcome name or recovery name
    expected: str | None = None
    observed: str | None = None
    fields: dict[str, Any] | None = None


def classify(
    obs: Observation,
    cap: Capability,
    step: Any,
    policy: Any,
    params: dict[str, Any],
    recovery_counts: dict[str, int],
) -> Classified:
    """Decide what the current frame is. Order is load-bearing; see module docstring."""
    raise NotImplementedError
