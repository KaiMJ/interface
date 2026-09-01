"""The discovery agent's action space.

Exactly the set of primitives an artifact can contain, so the model cannot express anything
replay cannot execute and replayability is a property of construction rather than a post-hoc
inference over a transcript.
"""

from __future__ import annotations

import re
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
    ResolutionTier,
    Risk,
    Scan,
    ScanAdvance,
    Step,
    Target,
)

# One line each: the rules behind `expect`, `anchor` and `risk` live in the system prompt
# rather than being restated — and re-billed — per tool.
_MARK = {
    "type": "integer",
    "description": "The number drawn on the element in the screenshot.",
}
_INTENT = {
    "type": "string",
    "description": (
        "What you are doing, in the words a reviewer would use: "
        "'click the View button on the row for the account being looked up'."
    ),
}
_EXPECT = {
    "type": "string",
    "description": (
        "A few words that will be literally on the screen after this action, and "
        "still true for a different record. Your instructions give the full rule."
    ),
}
_ANCHOR = {
    "type": "string",
    "description": (
        "OPTIONAL. The shortest durable text on this element. Your instructions "
        "give the full rule."
    ),
}
_SCOPE_ANCHOR = {
    "type": "string",
    "description": (
        "Visible text marking the top of the region to search — a column header, a "
        "section title. The search runs below it."
    ),
}
_TERMS = {
    "type": "array",
    "items": {"type": "string"},
    "description": "All of these must appear in the row you want.",
}
_NOT_FOUND_OUTCOME = {
    "type": "string",
    "description": (
        "Name for the legitimate business outcome when no row matches — e.g. "
        "transaction_not_found. Omit only if a missing row should be an error."
    ),
}
_RISK = {
    "type": "string",
    "enum": ["safe", "risky"],
    "description": "risky if this changes records or is hard to undo. safe otherwise.",
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
        {
            "mark": _MARK,
            "intent": _INTENT,
            "expect": _EXPECT,
            "risk": _RISK,
            "anchor": _ANCHOR,
        },
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
            "anchor": _ANCHOR,
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
        "Scroll the page.",
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
    # Two tools rather than one with an `action` enum, because of `expect`. Find-and-click
    # changes the screen, so an expectation about it is a real check; find-and-read changes
    # nothing, so the only honest answer is the value just read — one record's data, and a
    # checkpoint false for every other input. The field is absent here, not merely discouraged.
    _tool(
        "find_and_click",
        "Find the row matching a predicate inside a region and click it.",
        {
            "scope_anchor": _SCOPE_ANCHOR,
            "terms": _TERMS,
            "not_found_outcome": _NOT_FOUND_OUTCOME,
            "intent": _INTENT,
            "expect": _EXPECT,
            "risk": _RISK,
        },
        ["scope_anchor", "terms", "intent", "expect", "risk"],
    ),
    _tool(
        "find_and_extract",
        (
            "Find the row matching a predicate inside a region and read it into the "
            "capability's result. Reading changes nothing, so there is no `expect`: "
            "finding the row is the check, and replay fails the step if none matches."
        ),
        {
            "scope_anchor": _SCOPE_ANCHOR,
            "terms": _TERMS,
            "output_name": {
                "type": "string",
                "description": "The name this value has in the result, e.g. balance.",
            },
            "column": {
                "type": "string",
                "description": (
                    "The COLUMN HEADER above the value you want, copied off the screen "
                    "— 'Current Balance', 'Amount', 'Posted'. Give this whenever the "
                    "row is in a table: the cell is then located by its header, which "
                    "is what a person reads, and the result is the value alone. Omit "
                    "it and the whole row comes back as one string, which makes the "
                    "caller parse a screen this system already parsed."
                ),
            },
            "not_found_outcome": _NOT_FOUND_OUTCOME,
            "intent": _INTENT,
        },
        ["scope_anchor", "terms", "output_name", "intent"],
    ),
    _tool(
        "extract",
        (
            "Read the value of a numbered element into the capability's result. In a "
            "TABLE prefer find_and_extract: this records a position relative to a "
            "neighbour, which is wrong on a record whose columns are filled in "
            "differently."
        ),
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

# Which primitive each tool records as, for the policy check. `finish` and `escalate` end the
# run and touch nothing, so they are absent.
# The tools that search by content. An extract is a read, and is policy-checked as one.
FIND_TOOLS: dict[str, Primitive] = {
    "find_and_click": Primitive.CLICK,
    "find_and_extract": Primitive.EXTRACT,
}

PRIMITIVES: dict[str, Primitive] = {
    "click": Primitive.CLICK,
    "type_text": Primitive.TYPE,
    "press_key": Primitive.KEY,
    "navigate": Primitive.NAVIGATE,
    "scroll": Primitive.SCROLL,
    "extract": Primitive.EXTRACT,
    **FIND_TOOLS,
}

META = ("finish", "escalate")


def tool_definitions(allowed: frozenset[str]) -> list[dict[str, Any]]:
    """Build the tool list, filtered by policy.

    Filtered rather than checked-after: an action the policy forbids is not offered to the
    model at all, so no turn is spent on one that would be refused.
    """
    return [
        tool
        for tool in TOOLS
        if tool["function"]["name"] in META
        or PRIMITIVES[tool["function"]["name"]].value in allowed
    ]


# Below this a "row" is a heading or a stray label, not a table header.
MIN_HEADER_CELLS = 3

_MONEY = re.compile(r"[$£€]\s*[\d,]*\d|\b\d[\d,]*\.\d{2}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def durable_expect(
    expect: str,
    identifiers: Sequence[str] = (),
    extracted: Sequence[str] = (),
) -> str | None:
    """`expect` if it can still be true next month, otherwise None.

    A checkpoint answers "am I on the right screen", but the truest answer to "what will be on
    screen after this action" is often the record being looked at — an assertion that holds for
    this member and no other.

    Two things are refuted, both facts rather than judgment: an amount or a date, and anything
    this run has already read off the screen, which is by definition one record's data.

    A **declared input** is deliberately not refuted: `expect: '12345'` after typing the member
    id is a real check that the keystrokes landed, and `parameterize` rewrites it to
    `{{member_id}}`, which generalizes.

    The residual, stated: a name, a branch, a status. One frame cannot tell durable-looking
    text that identifies one record from a heading. What catches that is the reviewer at
    `approve`, and a second run with different inputs.
    """
    text = (expect or "").strip()
    if not text:
        return None
    if any(i and i.casefold() in text.casefold() for i in identifiers):
        return text
    if _MONEY.search(text) or _DATE.search(text):
        return None
    folded = text.casefold()
    if any(value and folded in str(value).casefold() for value in extracted):
        return None
    return text


def to_step(
    tool_name: str,
    tool_input: dict[str, Any],
    chosen_element: Any,
    step_id: int,
    obs: Any = None,
    identifiers: Sequence[str] = (),
    extracted: Sequence[str] = (),
) -> Step:
    """Turn one model tool-call into a typed artifact step.

    The model supplies a mark id; the element behind that mark supplies role, name, text and
    bbox, so `Target` is written from measured data rather than the model's description of what
    it thought it was clicking. Its own words go into `intent` and `target_desc`, which risk
    classification and pre-click verification need. `identifiers` are the run's declared inputs,
    which is what makes "the balance beside {{account_nickname}}" come out of a recording.
    """
    intent = str(tool_input.get("intent", tool_name))
    # A read has no `expect` in its schema and is given none here either, so the guarantee
    # holds against a provider that ignores `additionalProperties: false`.
    reads = tool_name == "extract" or FIND_TOOLS.get(tool_name) is Primitive.EXTRACT
    # Otherwise `durable_expect` tries to refute the proposal before it becomes an assertion,
    # as `_shorter_anchor` does for a proposed anchor.
    expect = (
        None
        if reads
        else durable_expect(
            str(tool_input.get("expect", "")), identifiers=identifiers, extracted=extracted
        )
    )
    risk = Risk.RISKY if tool_input.get("risk") == "risky" else Risk.SAFE
    checkpoint = (
        Checkpoint(kind=CheckKind.TEXT_PRESENT, value=expect, match=MatchMode.CONTAINS)
        if expect
        else None
    )

    if tool_name in FIND_TOOLS:
        # One artifact step kind either way: the split is in what the model may say,
        # not in what replay executes.
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
            on_found_action=FIND_TOOLS[tool_name],
            on_found_extract_as=tool_input.get("output_name"),
            on_found_extract_column=tool_input.get("column") or None,
            on_not_found_outcome=tool_input.get("not_found_outcome") or None,
        )

    target = _target_for(
        intent,
        chosen_element,
        obs,
        reads_a_value=tool_name == "extract",
        identifiers=identifiers,
        proposed_anchor=str(tool_input.get("anchor", "")).strip(),
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
    proposed_anchor: str = "",
) -> Target | None:
    """Write the target from what was actually on screen.

    Anchor text first, since it survives relayout and rebranding; role and name next; the
    recorded box last, logging a drift event when it is used. Two cases need the anchor to come
    from a *neighbour*: an empty control has no text to anchor on, and anchoring an extraction
    on the value it read identifies that balance only until the balance changes. Which
    neighbour is measured here rather than guessed by the model.

    A third case needs the anchor to be *part* of the element's own text — "29883 - Checking -
    $4,820.19" is durable in one half and volatile in the other. That is semantic rather than
    geometric, so the model is asked, and its answer is falsified against the frame it was
    proposed on.
    """
    if element is None:
        return None
    label = (element.text or element.name or "").strip()

    if (reads_a_value or not label) and obs is not None:
        relative = _relative_target(intent, element, obs, identifiers, in_table=reads_a_value)
        if relative is not None:
            return relative

    if label and obs is not None:
        shorter = _shorter_anchor(proposed_anchor, label, element, obs, identifiers)
        if shorter is not None:
            return Target(
                intent=intent,
                target_desc=label,
                anchor_text=shorter,
                anchor_match=MatchMode.CONTAINS,
                role=element.role,
                name=element.name,
                bbox=element.bbox,
            )

    return Target(
        intent=intent,
        target_desc=label or f"the {element.role or 'element'} at this position",
        anchor_text=label or None,
        anchor_match=MatchMode.CONTAINS,
        role=element.role,
        name=element.name,
        bbox=element.bbox,
    )



def _shorter_anchor(
    proposed: str,
    label: str,
    element: Element,
    obs: Any,
    identifiers: Sequence[str] = (),
) -> str | None:
    """A durable substring of `label`, or None to keep the whole thing.

    A value the **caller declared** as an input wins, since naming what varies per invocation
    is what "identifies this record" means; only when the durable part is not a declared input
    does the **model's** proposal apply. Fact before judgment: on `29883 - Checking -
    $4,820.19` a model may answer `$4,820.19`, which is on the element, unique on the frame,
    and passes every check here, while "will this still be true next month" is not answerable
    from one frame. Both candidates face `_identifies`: the text must be on the chosen element,
    and resolving it against this frame must land back on that element.

    The residual, stated: when the durable part is not a declared input and the model picks the
    volatile half, this accepts it. The reviewer and the resolution-tier drift signal catch it.
    """
    from ..resolve import Resolver

    resolver = Resolver(allow_vlm=False)
    lowered = label.casefold()
    candidates: list[str] = [i for i in identifiers if i and i.casefold() in lowered]
    if proposed:
        candidates.append(proposed)

    for candidate in candidates:
        text = candidate.strip()
        # A "shorter" anchor that is the whole label is just the label.
        if not text or len(text) >= len(label):
            continue
        if text.casefold() not in lowered:
            continue  # not on the element the model chose; it invented it
        if _identifies(resolver, text, element, obs):
            return text
    return None


def _identifies(resolver: Any, text: str, element: Element, obs: Any) -> bool:
    """Does this text still pick out this element, on the frame it came from?"""
    probe = Target(
        intent="anchor check",
        target_desc=text,
        anchor_text=text,
        anchor_match=MatchMode.CONTAINS,
        bbox=element.bbox,
    )
    try:
        found = resolver.resolve(probe, obs)
    except Exception:  # noqa: BLE001 - any failure to resolve is a rejection
        return False
    # `_pick` breaks ties by nearest-to-the-recorded-box, so a resolution that landed here by
    # proximity is not evidence; require exactly one candidate.
    return found.candidates == 1 and found.tier is ResolutionTier.ANCHOR_TEXT


def _column_over(element: Element, obs: Any) -> str | None:
    """The header of the column this element sits under, if it sits in one.

    Measured, not asked: the header row is found above the element and the column is the band
    between one header and the next, which is the same rule replay uses to find the cell again.

    Recorded alongside `relation_index` rather than instead of it, since the index still
    resolves a value merely beside a label; a screen with no header row returns None.
    """
    from ..perception import ElementIndex, cell_in_column, column_span

    index = ElementIndex(obs.elements)
    # The element must be in a table row itself: a nav bar is three or more texts sitting above
    # everything, and would otherwise read as a header row.
    own = next((r for r in index.rows() if any(e.id == element.id for e in r)), [element])
    if len(own) < MIN_HEADER_CELLS:
        return None
    header_rows = [
        row
        for row in index.rows()
        if len(row) >= MIN_HEADER_CELLS
        and max(e.bbox.y + e.bbox.h for e in row) <= element.bbox.y + 1e-6
    ]
    # A page can carry several tables, and only the nearest header row governs: falling
    # through to one further up would name a column from a different table.
    nearest = max(header_rows, key=lambda r: max(e.bbox.y for e in r), default=None)
    if nearest is None:
        return None
    for header in nearest:
        name = (header.text or header.name or "").strip()
        if not name:
            continue
        span = column_span(obs, name, above=element.bbox.y)
        if span is not None and cell_in_column([element], span) is not None:
            return name
    return None


def _relative_target(
    intent: str,
    element: Element,
    obs: Any,
    identifiers: Sequence[str] = (),
    in_table: bool = False,
) -> Target | None:
    """Anchor on the text to the left that best identifies this element.

    Nearest is not best: in an accounts grid the cell immediately left of a balance is its
    status, and "Active" is on every row, so anchoring there picks a row by proximity again.
    Two things beat proximity, in order: text the caller declared as an input, which becomes
    `{{account_nickname}}` at synthesis; then text that appears exactly once on the screen.
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
            # Only for a value being read: a column addresses a cell in a grid, and an empty
            # control beside a label is not one.
            column=_column_over(element, obs) if in_table else None,
            role=element.role,
            bbox=element.bbox,
        )
        if best is None or (score, -distance) > (best[0], -best[1]):
            best = (score, distance, candidate)
        if score == 2:
            break
    return best[2] if best is not None else None
