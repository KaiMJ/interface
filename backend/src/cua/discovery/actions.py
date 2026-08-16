"""The discovery agent's action space.

This is the most consequential decision in the discovery design, so it is worth
being blunt about it: *the action space is exactly the set of primitives an
artifact can contain*. The model cannot express anything replay cannot execute.

The alternative — let the model click freely, then infer intent from the transcript
afterwards — is the standard approach and it is fragile in a specific, predictable
way. A model that solves "find the transaction from ACME" by scrolling four times
and clicking has recorded `scroll, scroll, scroll, scroll, click(y)`, and no
post-hoc analysis can recover the fact that it meant "the row containing ACME".
Replay it tomorrow when the list has three fewer rows and it clicks the wrong
record. Giving the model `find_and_act` instead means the predicate is captured
because the model had to state it.

`escalate` is in the action space for the same reason: a model that has no way to
say "I am stuck" will instead do something plausible and wrong.
"""

from __future__ import annotations

from typing import Any

# Tool schemas handed to the model. Mirrors schema.Primitive plus the two
# meta-actions (`finish`, `escalate`) that end a run.
TOOLS: list[dict[str, Any]] = []


def tool_definitions(allowed: frozenset[str]) -> list[dict[str, Any]]:
    """Build the tool list, filtered by policy.

    Filtered rather than checked-after: an action the policy forbids is not
    offered to the model at all. Cheaper than refusing it later, and it keeps the
    model from spending turns trying to do something it will never be permitted to
    do.
    """
    raise NotImplementedError


def to_step(tool_name: str, tool_input: dict[str, Any], chosen_element: Any, step_id: int) -> Any:
    """Turn one model tool-call into a typed artifact step.

    The model supplies a mark id; the element behind that mark supplies role, name,
    text and bbox. So `Target` is written from measured data, not from the model's
    description of what it thought it was clicking. The model's own words go into
    `intent` and `target_desc`, where they are used for risk classification and
    pre-click verification — the two things a coordinate cannot support.
    """
    raise NotImplementedError
