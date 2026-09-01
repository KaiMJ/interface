"""Synthesize a recorded discovery run into a capability."""

from __future__ import annotations

import re
from typing import Any

from ..clock import now_iso
from ..resolve import apply, evaluate, unrender
from ..schema import (
    ActStep,
    AppRef,
    BusinessOutcome,
    Capability,
    CheckKind,
    Checkpoint,
    FindAndActStep,
    InputSpec,
    Normalizer,
    OutputSpec,
    Predicate,
    Primitive,
    Recording,
    Status,
    Step,
    Target,
    ValueType,
    Viewport,
)
from .prompts import DECLARATION_SCHEMA, SYNTHESIS

MONEY = (Normalizer.COLLAPSE_WS, Normalizer.STRIP_CURRENCY)
_COMPARE = (Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS)
_NORMALIZE_SEP = "\n"

# Prevent a changing record value from becoming a success condition.
MAX_SUCCESS_TEXT = 120


def prune(steps: list[Any]) -> list[Any]:
    """Drop a re-read under a name already taken, and a navigate the next one supersedes.

    Only what is decidable from the steps themselves. Judging redundancy from the frames
    would mean overruling, after the fact, a step the run verified at the time.
    """
    kept: list[Any] = []
    read: set[str] = set()
    for step in steps:
        # Keep one extraction per output name.
        name = getattr(step, "extract_as", None) or getattr(step, "on_found_extract_as", None)
        if name:
            if name in read:
                continue
            read.add(str(name))

        superseded = (
            kept
            and isinstance(step, ActStep)
            and step.action is Primitive.NAVIGATE
            and isinstance(kept[-1], ActStep)
            and kept[-1].action is Primitive.NAVIGATE
        )
        if superseded:
            kept[-1] = step
            continue
        kept.append(step)
    # Preserve ids until outputs are remapped.
    return kept


def parameterize(steps: list[Any], inputs: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Substitute declared input values with placeholders.

    Returns the rewritten steps and the derived `InputSpec` list. Longest-match
    first, so an input of `123` does not corrupt a recorded `12345` belonging to a
    different input.
    """
    used: set[str] = set()

    def sub(text: str | None) -> str | None:
        if not text:
            return text
        substituted = unrender(text, inputs) or text
        used.update(n for n in inputs if f"{{{{{n}}}}}" in substituted)
        return substituted

    rewritten: list[Any] = []
    for step in steps:
        checkpoint = step.checkpoint
        if checkpoint is not None:
            checkpoint = checkpoint.model_copy(update={"value": sub(checkpoint.value)})

        if isinstance(step, FindAndActStep):
            rewritten.append(
                step.model_copy(
                    update={
                        "checkpoint": checkpoint,
                        "scope": _sub_target(step.scope, sub),
                        "predicate": Predicate(
                            match=step.predicate.match,
                            terms=tuple(sub(t) or "" for t in step.predicate.terms),
                            normalize=step.predicate.normalize,
                        ),
                        # A dynamic column header is also a template.
                        "on_found_extract_column": sub(step.on_found_extract_column),
                    }
                )
            )
            continue

        rewritten.append(
            step.model_copy(
                update={
                    "checkpoint": checkpoint,
                    "value": sub(step.value),
                    "target": _sub_target(step.target, sub) if step.target else None,
                }
            )
        )

    specs = [
        InputSpec(
            name=name,
            type=_value_type(inputs[name]),
            description=f"supplied as {inputs[name]!r} on the recording run",
            example=str(inputs[name]),
        )
        for name in inputs
        # Do not expose inputs that do not affect the flow.
        if name in used
    ]
    return rewritten, specs


async def declare(state: Any, llm: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    """Ask the model for the description, success phrase and business outcomes.

    Bounded: a fixed schema, one call, no tool use, and the success phrase is
    checked against the final recorded frame before it is accepted.
    """
    final = state.observations[-1] if state.observations else None
    final_text = (
        " ".join(t for t in ((e.text or "").strip() for e in final.elements) if t)[:4000]
        if final is not None
        else ""
    )
    prompt = SYNTHESIS.format(
        goal=state.goal,
        steps="\n".join(state.history) or "(none)",
        inputs="\n".join(f"  {name}: {value!r}" for name, value in inputs.items()) or "  (none)",
        final_text=final_text or "(nothing readable)",
    )
    proposal = await llm.structured(
        system="You describe a recorded automation as a contract. Be precise and literal.",
        prompt=prompt,
        schema=DECLARATION_SCHEMA,
    )

    success_text = str(proposal.get("success_text", "")).strip()
    absent = (
        final is not None
        and bool(success_text)
        and not evaluate(Checkpoint(kind=CheckKind.TEXT_PRESENT, value=success_text), final)
    )
    if absent or len(success_text) > MAX_SUCCESS_TEXT:
        # Use the run's verified condition instead.
        proposal["success_text"] = state.success_text
        proposal["success_text_rejected"] = success_text
    if not proposal.get("success_text"):
        proposal["success_text"] = state.success_text

    proposed_id = str(proposal.get("capability_id", "")).strip()
    chosen, why = _name(proposed_id, inputs, state.goal)
    proposal["capability_id"] = chosen
    if why:
        proposal["capability_id_rejected"] = {"proposed": proposed_id, "because": why}

    kept, rejected = _falsify(proposal.get("business_outcomes", ()), state)
    proposal["business_outcomes"] = kept
    proposal["business_outcomes_rejected"] = rejected

    declared: dict[str, Any] = dict(proposal)
    state.declaration = declared
    return declared


def _falsify(proposed: Any, state: Any) -> tuple[list[Any], list[Any]]:
    """Reject outcome detectors visible on a successful recording."""
    seen = _NORMALIZE_SEP.join(
        apply(" ".join((e.text or "").strip() for e in obs.elements), _COMPARE)
        for obs in state.observations
    )
    kept: list[Any] = []
    rejected: list[Any] = []
    for outcome in proposed:
        detector = str(outcome.get("detector_text", "")).strip()
        if not detector:
            rejected.append({**outcome, "rejected_because": "no detector text"})
        elif apply(detector, _COMPARE) in seen:
            rejected.append(
                {
                    **outcome,
                    "rejected_because": (
                        "this text is on a frame the successful run passed through, "
                        "so it cannot distinguish a different outcome"
                    ),
                }
            )
        else:
            kept.append(outcome)
    return kept, rejected


async def synthesize(
    state: Any,
    inputs: dict[str, Any],
    llm: Any,
    capability_id: str = "",
    app: AppRef | None = None,
    viewport: Viewport | None = None,
    policy: Any = None,
) -> Capability:
    """Full pipeline. Validates the result parses as a `Capability` before returning."""
    steps = prune(state.steps)
    steps, specs = parameterize(steps, inputs)
    proposal = await declare(state, llm, inputs)

    # Outputs are read off the recording's own step ids, then both are renumbered together,
    # so the artifact reads 1..n without breaking the extraction-to-output link.
    outputs = _outputs(steps, state)
    steps, renumbered = _renumber(steps)
    outputs = [o.model_copy(update={"from_step": renumbered[o.from_step]}) for o in outputs]
    success = Checkpoint(
        kind=CheckKind.TEXT_PRESENT,
        value=str(proposal.get("success_text") or state.success_text or ""),
    )

    # What the application already knows, inherited by name. A recording cannot discover these
    # detectors — the successful run never reaches those screens — so without inheritance a
    # fresh capability hard-fails on legitimate answers.
    #
    # Name-only, so `effective_outcomes` resolves the detector at run time and one policy edit
    # reaches every capability on that app. An outcome this flow cannot reach is inert and a
    # reviewer prunes it.
    inherited_outcomes = [
        BusinessOutcome(name=o.name, description=o.description)
        for o in getattr(policy, "business_outcomes", ())
    ]
    known = {o.name for o in inherited_outcomes}

    # What the model guessed, minus anything the application already declares: the policy's
    # wording is demonstrated and the model's is not.
    proposed_outcomes = [
        BusinessOutcome(
            name=_slug(str(o.get("name", ""))),
            description=str(o.get("description", "")),
            detector=Checkpoint(kind=CheckKind.TEXT_PRESENT, value=str(o.get("detector_text", ""))),
            result_fields={spec.name: spec.type for spec in specs},
            # Survived refutation, which is not confirmation: `_falsify` can only rule a
            # detector out. Recorded for a reviewer and withheld from the tool manifest until
            # `cua learn-outcome` or `cua diagnose` demonstrates it.
            verified=False,
        )
        for o in proposal.get("business_outcomes", ())
        if o.get("name") and o.get("detector_text") and _slug(str(o.get("name", ""))) not in known
    ]

    cap = Capability(
        # `--capability-id` wins outright; otherwise the model's proposal, having survived
        # `_name`; otherwise a slug of the goal.
        id=capability_id or str(proposal.get("capability_id")) or _slug(state.goal),
        status=Status.DRAFT,
        goal=state.goal,
        description=str(proposal.get("description", "")),
        app=app or AppRef(name="unknown", base_url_pattern=".*"),
        inputs=specs,
        outputs=outputs,
        steps=[_typed(s) for s in steps],
        success=success,
        business_outcomes=[*inherited_outcomes, *proposed_outcomes],
        recording=Recording(
            run_id=state.run_id,
            model=getattr(llm, "model", ""),
            viewport=viewport or Viewport(width=1440, height=900),
            recorded_at=now_iso(),
            step_count=len(steps),
        ),
    )
    # Round-trips through its own schema before anyone is told it exists.
    return Capability.model_validate_json(cap.model_dump_json())


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _renumber(steps: list[Any]) -> tuple[list[Any], dict[int, int]]:
    """Number the surviving steps 1..n, and say how the old ids map to the new."""
    mapping = {step.id: i + 1 for i, step in enumerate(steps)}
    return [s.model_copy(update={"id": mapping[s.id]}) for s in steps], mapping


def _sub_target(target: Target | None, sub: Any) -> Target | None:
    if target is None:
        return None
    return target.model_copy(
        update={
            "anchor_text": sub(target.anchor_text),
            # A dynamic column header is also a template.
            "column": sub(target.column),
            "name": sub(target.name),
            "intent": sub(target.intent),
            "target_desc": sub(target.target_desc),
        }
    )


def _outputs(steps: list[Any], state: Any) -> list[OutputSpec]:
    """The output contract, read off the recording.

    Every extraction already carries the name the model chose and the step it came from, so a
    contract has one source of truth about its own shape.
    """
    outputs: list[OutputSpec] = []
    for step in steps:
        name = getattr(step, "extract_as", None) or getattr(step, "on_found_extract_as", None)
        if not name:
            continue
        sample = state.extracted.get(step.id, "")
        outputs.append(
            OutputSpec(
                name=str(name),
                type=_extracted_type(sample),
                from_step=step.id,
                normalize=MONEY,
                description=f"read from the screen during recording as {sample!r}",
            )
        )
    return outputs


def _extracted_type(sample: str) -> ValueType:
    """Types come from what was actually read, not from the name of the field."""
    try:
        float(apply(sample, MONEY))
    except (TypeError, ValueError):
        return ValueType.STRING
    return ValueType.NUMBER


def _value_type(value: Any) -> ValueType:
    if isinstance(value, bool):
        return ValueType.BOOLEAN
    if isinstance(value, int):
        return ValueType.INTEGER
    if isinstance(value, float):
        return ValueType.NUMBER
    # A string stays a string even when it looks like a number: member and account numbers
    # have leading zeros, and "007" is not 7.
    return ValueType.STRING


def _typed(step: Any) -> Step:
    """Narrow the loop's `Any` steps back to the artifact's discriminated union.

    They have been `ActStep` and `FindAndActStep` all along — the loop holds them
    as `Any` only because it never needs to look inside them.
    """
    assert isinstance(step, ActStep | FindAndActStep)
    return step


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:60] or "capability"


_ID = re.compile(r"^[a-z][a-z0-9_]{2,47}$")


def _name(proposed: str, inputs: dict[str, Any], goal: str) -> tuple[str, str]:
    """The capability's id: proposed by the model, then refuted by code.

    Two checks: snake_case at 3-48 characters, and none of the caller's declared values, since
    the id of a flow parameterised by member id should not contain a member id. Falls back to a
    slug of the goal. Returns the id and, when the proposal was refused, why — kept in
    `synthesis.json`.
    """
    # Normalised directly rather than through `_slug`, whose empty-string fallback is the
    # literal name "capability" and would pass the shape check below.
    candidate = re.sub(r"[^a-z0-9]+", "_", proposed.lower()).strip("_")
    if not candidate:
        return _slug(goal), "the model proposed no usable id"
    if not _ID.fullmatch(candidate):
        return _slug(goal), f"{candidate!r} is not a usable name"

    for name, value in inputs.items():
        literal = _slug(str(value))
        if literal and literal in candidate:
            return (
                _slug(goal),
                f"{candidate!r} contains the value of {name!r} — that is the "
                f"argument, not the flow",
            )
    return candidate, ""
