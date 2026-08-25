"""The discovery agent's action space.

The most consequential decision in the discovery design: the action space is exactly the
set of primitives an artifact can contain, so the model cannot express anything replay
cannot execute. The alternative — let it click freely and infer intent from the
transcript afterwards — makes replayability a post-hoc inference rather than a property
of construction.
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
        "Text that will be LITERALLY ON THE SCREEN after this action — it is matched "
        "as a substring of what is read off the screen, so it must be words the "
        "application renders. Good: a heading, a panel title, a button label, a "
        "confirmation line — 'Step 2 of 3'. Bad, and it will not match: 'the "
        "record is shown', 'search results appear' — nothing renders those words. "
        "It must ALSO be true the next time this runs, for a DIFFERENT record. This "
        "recording is replayed with other inputs, so words belonging to the record "
        "in front of you make an assertion that is false for everyone else. AVOID a "
        "balance, an amount, a date, a person's name, a count, a status. Prefer what "
        "the application says about itself no matter who is on screen. "
        "Good: 'Member Profile' / 'Accounts' / 'Transfer submitted'. "
        "Bad: '$18,204.55' (right now, and wrong for the next member), "
        "'Marcus Webb', '2019-11-02'. "
        "It is checked: an amount or a date is dropped and the step is recorded with "
        "no checkpoint at all, which is weaker than one you chose well. "
        "Keep it short: a few words, not a sentence."
    ),
}
_ANCHOR = {
    "type": "string",
    "description": (
        "OPTIONAL. The shortest text ON THIS ELEMENT that will still identify it "
        "next month. An element's full text is often part durable and part "
        "volatile, and only the durable part belongs in a recording. Prefer an "
        "id, a code, an account or reference number, a fixed label. AVOID "
        "anything that moves: a balance, a date, a count, a status, a relative "
        "time. If the whole visible text is already stable, give that. "
        "Good: '29883' / 'View' / 'Primary Savings'. "
        "Bad: '29883 - Checking - $4,820.19' (the balance changes, and this "
        "recording is replayed after it has), 'Updated 2 minutes ago'. "
        "It is checked: it must be text actually on the element you chose, and "
        "it must still pick out that same element. If it does not, it is dropped "
        "and the full text is recorded instead."
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
        "Scroll the page. Prefer find_and_click or find_and_extract when you are "
        "looking for a record.",
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
    # Two tools rather than one with an `action` enum, and the reason is `expect`.
    # Finding-and-clicking changes the screen, so an expectation about the screen
    # after it is a real check. Finding-and-reading changes nothing, so the only
    # honest answer to "what will be on screen after this" is the value just read —
    # and requiring one produced a recorded capability asserting `$18,204.55`, which
    # was true for the member it was recorded on and false for every other. The
    # field is not described better here; it is absent, so the answer cannot be given.
    _tool(
        "find_and_click",
        (
            "Find the row matching a predicate inside a region and click it. Use "
            "this instead of scrolling and clicking whenever the thing you want is "
            "identified by its content rather than its position — a member, a "
            "transaction, an account. It records WHAT you were looking for, which "
            "is what makes the recording work again tomorrow when the list has "
            "changed."
        ),
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
            "capability's result. Same search as find_and_click — content, not "
            "position — for when you want the value on the row rather than to open "
            "it. Reading does not change the screen, so there is nothing to expect "
            "afterwards: finding the row IS the check, and replay fails the step if "
            "no row matches."
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
            "Read the value of a numbered element into the capability's result. For a "
            "value in a TABLE, prefer find_and_extract: this tool records the value's "
            "position relative to a neighbour, and a position counted on this record "
            "is wrong on a record with a column filled in differently. find_and_extract "
            "records the row's content and the column's header instead."
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

# Which primitive each tool records as, for the policy check. `finish` and
# `escalate` end the run and touch nothing, so they are not in the map.
# The tools that search by content, and what each does with the row it finds. An
# extract is a read, and was policy-checked as a click while both shapes shared one
# tool name — two names let the guardrail see which it is.
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


# Below this a "row" is a heading or a stray label, not a table header. Three is the
# narrowest thing that still reads as a table on the target app's screens.
MIN_HEADER_CELLS = 3

_MONEY = re.compile(r"[$£€]\s*[\d,]*\d|\b\d[\d,]*\.\d{2}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")


def durable_expect(
    expect: str,
    identifiers: Sequence[str] = (),
    extracted: Sequence[str] = (),
) -> str | None:
    """`expect` if it can still be true next month, otherwise None.

    A checkpoint answers "am I on the right screen". The model is asked for text that
    will be on screen *after* the action, and the truest answer to that question is
    often the record it is looking at — which makes an assertion that holds for this
    member and no other. Measured: a recording of `get_account_balance` asserted
    `$18,204.55` at its final step and failed on every replay for a different member,
    having navigated perfectly.

    Two things are refuted, both facts rather than judgment: an amount or a date, which
    are the classes `_ANCHOR` already warns against by name, and anything this run has
    already read off the screen, which is by definition one record's data.

    A **declared input** is deliberately not refuted. `expect: '12345'` after typing the
    member id is a real check that the keystrokes landed, and `parameterize` rewrites it
    to `{{member_id}}`, which generalizes — refuting it would delete a working assertion.

    The residual, stated: a name, a branch, a status. `Marcus Webb` is durable-looking
    text that identifies one record, and one frame cannot tell it from a heading. What
    catches that is the reviewer at `approve`, and a second run with different inputs.
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
    bbox, so `Target` is written from measured data rather than the model's description of
    what it thought it was clicking. Its own words go into `intent` and `target_desc`, which
    is what risk classification and pre-click verification need and a coordinate cannot
    support. `identifiers` are the run's declared inputs, which is what makes "the balance
    beside {{account_nickname}}" come out of a recording instead of "the balance beside
    Active".
    """
    intent = str(tool_input.get("intent", tool_name))
    # A read has no `expect` in its schema, and is given none here either — belt to the
    # schema's braces, so the guarantee holds against a provider that ignores
    # `additionalProperties: false`. `extract` was always shaped this way; splitting
    # `find_and_act` is what made the row-reading half agree with it.
    reads = tool_name == "extract" or FIND_TOOLS.get(tool_name) is Primitive.EXTRACT
    # Otherwise the model proposes an expectation and `durable_expect` tries to refute
    # it before it becomes an assertion, as `_shorter_anchor` does for a proposed anchor.
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
    recorded box last, logging a drift event when it is used. Two cases need the anchor to
    come from a *neighbour*, and both were found by reading a recorded artifact: an empty
    control has no text to anchor on, and anchoring an extraction on the value it read
    identifies that balance only until the balance changes. Which neighbour is measured here
    rather than guessed by the model.

    A third case needs the anchor to be *part* of the element's own text — "29883 - Checking -
    $4,820.19" is durable in one half and volatile in the other. That is semantic rather than
    geometric, so the model is asked, and its answer is falsified against the frame it was
    proposed on: a proposal not on the element, or that no longer picks it out, is dropped.
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

    Two candidates, and the order between them is the design. A value the **caller declared**
    as an input wins, because naming it as the thing that varies per invocation is exactly
    what "identifies this record" means; only when the durable part is not a declared input do
    we fall back to what the **model** proposed.

    Fact before judgment, because judgment can be wrong in a way this function cannot detect:
    asked for an anchor on `29883 - Checking - $4,820.19`, a model may answer `$4,820.19`,
    which is on the element, unique on the frame, and passes every check here — "will this
    still be true next month" is not answerable from one frame, and the declared input `29883`
    is. Both candidates face `_identifies`: the text must be on the chosen element, and
    resolving it against this frame must land back on that element.

    The residual, stated: when the durable part is not a declared input *and* the model picks
    the volatile half, this accepts it. What catches it is the reviewer, then the
    resolution-tier drift signal in production.
    """
    from ..resolve import Resolver

    resolver = Resolver(allow_vlm=False)
    lowered = label.casefold()
    candidates: list[str] = [i for i in identifiers if i and i.casefold() in lowered]
    if proposed:
        candidates.append(proposed)

    for candidate in candidates:
        text = candidate.strip()
        # No point replacing a string with itself, and a "shorter" anchor that is
        # the whole label is just the label.
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
    # `_pick` breaks ties by nearest-to-the-recorded-box, so a resolution that
    # merely landed here by proximity is not evidence. Require the anchor to have
    # matched exactly one candidate.
    return found.candidates == 1 and found.tier is ResolutionTier.ANCHOR_TEXT


def _column_over(element: Element, obs: Any) -> str | None:
    """The header of the column this element sits under, if it sits in one.

    Measured, not asked: the header row is found above the element and the column is
    the band between one header and the next, so a value belongs to the header whose
    band contains it — the way a person reads a table, and the same rule replay uses
    to find the cell again.

    Recorded alongside `relation_index` rather than instead of it. The index still
    resolves a value that is merely *beside* a label rather than under a header, and
    a screen with no header row returns None here and loses nothing.
    """
    from ..perception import ElementIndex, cell_in_column, column_span

    index = ElementIndex(obs.elements)
    # The element has to be in a table row itself. A navigation bar is a row of three
    # or more texts and sits above everything, so without this the search box on the
    # member page took "Member Search" — a nav item — as its column, and replay typed
    # into whatever sat in that band. Measured on a live run: the query went out empty.
    own = next((r for r in index.rows() if any(e.id == element.id for e in r)), [element])
    if len(own) < MIN_HEADER_CELLS:
        return None
    header_rows = [
        row
        for row in index.rows()
        if len(row) >= MIN_HEADER_CELLS
        and max(e.bbox.y + e.bbox.h for e in row) <= element.bbox.y + 1e-6
    ]
    # Nearest first: a page can carry several tables, and the one above this element
    # is the one whose columns it is in.
    for row in sorted(header_rows, key=lambda r: -max(e.bbox.y for e in r)):
        for header in row:
            name = (header.text or header.name or "").strip()
            if not name:
                continue
            span = column_span(obs, name, above=element.bbox.y)
            if span is not None and cell_in_column([element], span) is not None:
                return name
        # The nearest header row is the one that governs. Falling through to a row
        # further up would name a column from a different table.
        return None
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
    status, and "Active" is on every row — anchoring there means replay picks a row by
    proximity to a recorded coordinate, which is what anchoring was supposed to stop doing.
    Two things beat proximity, in order: text the caller declared as an input, which becomes
    `{{account_nickname}}` at synthesis and makes the step data-dependent; then text that
    appears exactly once on the screen, uniqueness being the cheapest available proxy for
    "this identifies the row".
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
            # Only for a value being read. A column addresses a cell in a grid; an
            # empty control beside a label is neither, and asking the question there
            # is how a nav bar gets mistaken for a header row.
            column=_column_over(element, obs) if in_table else None,
            role=element.role,
            bbox=element.bbox,
        )
        if best is None or (score, -distance) > (best[0], -best[1]):
            best = (score, distance, candidate)
        if score == 2:
            break
    return best[2] if best is not None else None
