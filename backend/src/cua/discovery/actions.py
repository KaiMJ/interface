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

Two fields appear on every acting tool and neither is decoration:

  `intent`  what the model believes it is doing. It is what the policy layer
            classifies for risk — `click(0.42, 0.71)` cannot be judged reversible
            or not — and it is what a human reviewer reads.
  `expect`  a phrase the model expects to see *after* the action. It is checked
            immediately: a step whose expectation did not come true is not
            recorded, and the model is told. That is what makes a recording
            replayable by construction rather than by hope — every step in a saved
            artifact has already had its checkpoint verified once, on the run that
            created it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..schema import (
    ActStep,
    CheckKind,
    Checkpoint,
    Element,
    FindAndActStep,
    MatchMode,
    Predicate,
    Primitive,
    Relation,
    Risk,
    Scan,
    ScanAdvance,
    Step,
    Target,
)

_MARK = {
    "type": "integer",
    "description": "The number drawn on the element in the screenshot.",
}
_INTENT = {
    "type": "string",
    "description": (
        "What you are doing, in the words a reviewer would use: "
        "'click the View button on the savings account row'."
    ),
}
_EXPECT = {
    "type": "string",
    "description": (
        "Text that will be LITERALLY ON THE SCREEN after this action — it is matched "
        "as a substring of what is read off the screen, so it must be words the "
        "application renders. Good: 'Member Profile', 'Transfer Complete', "
        "'Step 2 of 3'. Bad, and it will not match: 'the profile for 12345 is "
        "shown', 'search results appear'. Prefer a heading, a panel title or a "
        "button label. Keep it short — a few words, not a sentence."
    ),
}
_RISK = {
    "type": "string",
    "enum": ["safe", "risky"],
    "description": (
        "risky if this action changes the institution's records or is hard to undo "
        "— submitting a transfer, confirming, deleting. safe otherwise."
    ),
}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


# Tool schemas handed to the model. Mirrors schema.Primitive plus the two
# meta-actions (`finish`, `escalate`) that end a run.
TOOLS: list[dict[str, Any]] = [
    _tool(
        "click",
        "Click one of the numbered elements.",
        {"mark": _MARK, "intent": _INTENT, "expect": _EXPECT, "risk": _RISK},
        ["mark", "intent", "expect", "risk"],
    ),
    _tool(
        "type_text",
        "Click a numbered element and type into it.",
        {
            "mark": _MARK,
            "text": {"type": "string", "description": "The exact text to type."},
            "intent": _INTENT,
            "expect": _EXPECT,
        },
        ["mark", "text", "intent", "expect"],
    ),
    _tool(
        "press_key",
        "Press a single key, e.g. Enter or Escape.",
        {
            "key": {"type": "string"},
            "intent": _INTENT,
            "expect": _EXPECT,
        },
        ["key", "intent", "expect"],
    ),
    _tool(
        "navigate",
        "Go to a URL. Only URLs inside the allowlist are permitted.",
        {"url": {"type": "string"}, "intent": _INTENT, "expect": _EXPECT},
        ["url", "intent", "expect"],
    ),
    _tool(
        "scroll",
        "Scroll the page. Prefer find_and_act when you are looking for a record.",
        {
            "amount": {
                "type": "number",
                "description": "Fraction of a screen. Positive scrolls down.",
            },
            "intent": _INTENT,
            "expect": _EXPECT,
        },
        ["amount", "intent", "expect"],
    ),
    _tool(
        "find_and_act",
        (
            "Find the row matching a predicate inside a region and act on it. Use "
            "this instead of scrolling and clicking whenever the thing you want is "
            "identified by its content rather than its position — a member, a "
            "transaction, an account. It records WHAT you were looking for, which "
            "is what makes the recording work again tomorrow when the list has "
            "changed."
        ),
        {
            "scope_anchor": {
                "type": "string",
                "description": (
                    "Visible text marking the top of the region to search — a column "
                    "header, a section title. The search runs below it."
                ),
            },
            "terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "All of these must appear in the row you want.",
            },
            "action": {
                "type": "string",
                "enum": ["click", "extract"],
                "description": "Click the row, or read it.",
            },
            "output_name": {
                "type": "string",
                "description": "For extract: the name this value has in the result.",
            },
            "not_found_outcome": {
                "type": "string",
                "description": (
                    "Name for the legitimate business outcome when no row matches — "
                    "e.g. transaction_not_found. Omit only if a missing row should be "
                    "an error."
                ),
            },
            "intent": _INTENT,
            "expect": _EXPECT,
            "risk": _RISK,
        },
        ["scope_anchor", "terms", "action", "intent", "expect", "risk"],
    ),
    _tool(
        "extract",
        "Read the value of a numbered element into the capability's result.",
        {
            "mark": _MARK,
            "output_name": {
                "type": "string",
                "description": "The name this value has in the result, e.g. balance.",
            },
            "intent": _INTENT,
        },
        ["mark", "output_name", "intent"],
    ),
    _tool(
        "finish",
        "The goal is complete. Say what was achieved and what the caller receives.",
        {
            "summary": {"type": "string"},
            "success_text": {
                "type": "string",
                "description": (
                    "A distinctive phrase visible on the final screen that proves the "
                    "goal was reached. It is checked against the frame."
                ),
            },
        },
        ["summary", "success_text"],
    ),
    _tool(
        "escalate",
        (
            "You are stuck, or the next step is risky and you are not certain. This "
            "is a legitimate answer and hands the session to a human operator. It is "
            "preferred over guessing."
        ),
        {"reason": {"type": "string"}},
        ["reason"],
    ),
]

# Which primitive each tool records as, for the policy check. `finish` and
# `escalate` end the run and touch nothing, so they are not in the map.
PRIMITIVES: dict[str, Primitive] = {
    "click": Primitive.CLICK,
    "type_text": Primitive.TYPE,
    "press_key": Primitive.KEY,
    "navigate": Primitive.NAVIGATE,
    "scroll": Primitive.SCROLL,
    "extract": Primitive.EXTRACT,
    "find_and_act": Primitive.CLICK,
}

META = ("finish", "escalate")


def tool_definitions(allowed: frozenset[str]) -> list[dict[str, Any]]:
    """Build the tool list, filtered by policy.

    Filtered rather than checked-after: an action the policy forbids is not
    offered to the model at all. Cheaper than refusing it later, and it keeps the
    model from spending turns trying to do something it will never be permitted to
    do.
    """
    return [
        tool
        for tool in TOOLS
        if tool["function"]["name"] in META
        or PRIMITIVES[tool["function"]["name"]].value in allowed
    ]


def to_step(
    tool_name: str,
    tool_input: dict[str, Any],
    chosen_element: Any,
    step_id: int,
    obs: Any = None,
    identifiers: Sequence[str] = (),
) -> Step:
    """Turn one model tool-call into a typed artifact step.

    The model supplies a mark id; the element behind that mark supplies role, name,
    text and bbox. So `Target` is written from measured data, not from the model's
    description of what it thought it was clicking. The model's own words go into
    `intent` and `target_desc`, where they are used for risk classification and
    pre-click verification — the two things a coordinate cannot support.

    The observation is passed in for the same reason: when the chosen element has
    no stable text of its own, the anchor is measured from what is beside it
    rather than invented. `identifiers` are the run's declared input values, which
    is what makes "the balance beside {{account_nickname}}" come out of a
    recording instead of "the balance beside Active".
    """
    intent = str(tool_input.get("intent", tool_name))
    expect = str(tool_input.get("expect", "")).strip()
    risk = Risk.RISKY if tool_input.get("risk") == "risky" else Risk.SAFE
    checkpoint = (
        Checkpoint(kind=CheckKind.TEXT_PRESENT, value=expect, match=MatchMode.CONTAINS)
        if expect
        else None
    )

    if tool_name == "find_and_act":
        return FindAndActStep(
            id=step_id,
            risk=risk,
            checkpoint=checkpoint,
            scope=Target(
                intent=f"the region below {tool_input.get('scope_anchor')!r}",
                target_desc="the region to search",
                anchor_text=str(tool_input.get("scope_anchor", "")),
            ),
            predicate=Predicate(terms=tuple(str(t) for t in tool_input.get("terms", ()))),
            scan=Scan(advance=ScanAdvance.SCROLL),
            on_found_action=(
                Primitive.EXTRACT if tool_input.get("action") == "extract" else Primitive.CLICK
            ),
            on_found_extract_as=tool_input.get("output_name"),
            on_not_found_outcome=tool_input.get("not_found_outcome") or None,
        )

    target = _target_for(
        intent,
        chosen_element,
        obs,
        reads_a_value=tool_name == "extract",
        identifiers=identifiers,
    )
    if tool_name == "click":
        return ActStep(id=step_id, action=Primitive.CLICK, risk=risk, target=target,
                       checkpoint=checkpoint)
    if tool_name == "type_text":
        return ActStep(
            id=step_id,
            action=Primitive.TYPE,
            risk=risk,
            target=target,
            value=str(tool_input.get("text", "")),
            checkpoint=checkpoint,
        )
    if tool_name == "press_key":
        return ActStep(id=step_id, action=Primitive.KEY, risk=risk,
                       value=str(tool_input.get("key", "Enter")), checkpoint=checkpoint)
    if tool_name == "navigate":
        return ActStep(id=step_id, action=Primitive.NAVIGATE, risk=risk,
                       value=str(tool_input.get("url", "")), checkpoint=checkpoint)
    if tool_name == "scroll":
        return ActStep(id=step_id, action=Primitive.SCROLL, risk=risk, target=target,
                       value=str(tool_input.get("amount", 0.8)), checkpoint=checkpoint)
    if tool_name == "extract":
        return ActStep(
            id=step_id,
            action=Primitive.EXTRACT,
            risk=Risk.SAFE,
            target=target,
            extract_as=str(tool_input.get("output_name", f"value_{step_id}")),
            checkpoint=checkpoint,
        )
    raise ValueError(f"no step mapping for tool {tool_name!r}")


def _target_for(
    intent: str,
    element: Element | None,
    obs: Any = None,
    reads_a_value: bool = False,
    identifiers: Sequence[str] = (),
) -> Target | None:
    """Write the target from what was actually on screen.

    Anchor text first, because it is the tier that survives relayout and
    rebranding; role and name next; the recorded box last, as the thing that gets
    used when the first two miss and which logs a drift event when it does.

    Two cases need the anchor to come from a *neighbour* instead of from the
    element itself, and both were found by reading a recorded artifact:

      an empty control  a form field has no text, so anchoring on it is anchoring
                        on nothing and replay falls straight through to the
                        recorded box
      a value           anchoring an extraction on the value it read is
                        self-defeating: `$18,229.55` identifies this balance only
                        until the balance changes, which for a balance is the
                        whole point

    In both cases the label beside it is what identifies it, and which neighbour
    it is gets measured here rather than guessed by the model.
    """
    if element is None:
        return None
    label = (element.text or element.name or "").strip()

    if (reads_a_value or not label) and obs is not None:
        relative = _relative_target(intent, element, obs, identifiers)
        if relative is not None:
            return relative

    return Target(
        intent=intent,
        target_desc=label or f"the {element.role or 'element'} at this position",
        anchor_text=label or None,
        anchor_match=MatchMode.CONTAINS,
        role=element.role,
        name=element.name,
        bbox=element.bbox,
    )


def _relative_target(
    intent: str, element: Element, obs: Any, identifiers: Sequence[str] = ()
) -> Target | None:
    """Anchor on the text to the left that best identifies this element.

    Nearest is not best. In an accounts grid the cell immediately left of a
    balance is its status, and "Active" is on every row — anchoring there means
    replay picks a row by proximity to a recorded coordinate, which is what
    anchoring was supposed to stop doing. Two things beat proximity, in order:

      1. text the caller declared as an input for this run. It becomes
         `{{account_nickname}}` at synthesis, and the step turns into "the balance
         beside the account the caller named" — data-dependent and right for every
         future invocation, not just this one.
      2. text that appears exactly once on the screen. Uniqueness is the cheapest
         available proxy for "this identifies the row".
    """
    from ..perception import ElementIndex

    index = ElementIndex(obs.elements)
    wanted = [i.casefold() for i in identifiers if i]
    counts: dict[str, int] = {}
    for e in obs.elements:
        key = (e.text or e.name or "").strip().casefold()
        if key:
            counts[key] = counts.get(key, 0) + 1

    best: tuple[int, int, Target] | None = None
    for distance, anchor in enumerate(index.left_of(element)):
        text = (anchor.text or anchor.name or "").strip()
        if not text:
            continue
        neighbours = index.right_of(anchor)
        position = next((i for i, e in enumerate(neighbours) if e.id == element.id), None)
        if position is None:
            continue
        score = 2 if any(w in text.casefold() for w in wanted) else 0
        if not score and counts.get(text.casefold(), 0) == 1:
            score = 1
        candidate = Target(
            intent=intent,
            target_desc=f"the value to the right of {text!r}",
            anchor_text=text,
            anchor_match=MatchMode.CONTAINS,
            relation=Relation.RIGHT_OF,
            relation_index=position,
            role=element.role,
            bbox=element.bbox,
        )
        if best is None or (score, -distance) > (best[0], -best[1]):
            best = (score, distance, candidate)
        if score == 2:
            break
    return best[2] if best is not None else None
