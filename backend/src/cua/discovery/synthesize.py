"""Run -> capability. The synthesis pass.

Open question in PLAN.md; resolved here as a three-stage pipeline, deterministic
first and model-assisted only where determinism cannot answer.

  1. PRUNE      (deterministic)
     Drop steps that did not advance the state. This stage turned out to be
     nearly empty, and that is a design result rather than an omission: the loop
     only records a step whose stated expectation came true, so the failed and
     retried actions this stage was meant to remove never enter the list. What is
     left to drop is a navigation immediately superseded by another.

  2. PARAMETERIZE (deterministic, from known inputs)
     The run was given concrete inputs. Any recorded literal equal to one of them
     becomes `{{that_input}}` — in typed values, in anchor text, in predicates, and
     in checkpoint values. This is the answer to "who decided 12345 was a
     parameter?": nobody did, at synthesis time. The caller declared it when they
     started the run.

     This is deliberately *not* an LLM guessing which numbers look like ids. It is
     exact string matching against declared inputs, which cannot invent a parameter
     and cannot miss one that was actually supplied.

  3. DECLARE    (model-assisted, bounded)
     What is left needs judgment: a description for the calling agent, the success
     phrase, and which legitimate alternative results this flow can produce. One
     structured completion, no tool use, and the success phrase is validated
     against the final recorded frame — a proposed checkpoint that does not
     actually match is rejected, not trusted.

     Outputs are *not* asked for. Every extraction the model performed already
     carries the name it chose and the step it came from, so the output contract
     is read off the recording instead of being re-derived by a second model call
     that could disagree with the first.

Stage 3 output is `status: draft`. A human approves it before unattended replay.
That approval gate is the honest place to put a human, rather than pretending
stage 3 is reliable. Business outcomes in particular are *proposals*: they
describe screens this run did not visit, so unlike the success condition there is
nothing to validate them against.
"""

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

# A success condition is a *proof*, not a transcript. Measured: a model asked for
# one returned the entire final screen — which validates, and then fails on the
# next run because a balance in the middle of it changed by twenty-five dollars.
MAX_SUCCESS_TEXT = 120


# Screens are deliberately *not* derived here, and the reason is a measurement.
#
# A first version took, for each frame a step acted on, the longest line unique to
# it among the run's other frames — the same rule that works for outcome
# detectors. On the read capability it named the three screens `s_cards_reports`
# ("s Cards Reports", an OCR fragment of the navigation bar that happened to read
# differently on each frame) and `riverside_004` ("Riverside — 004", the member's
# *branch*). The second is the instructive one: it identifies the record, not the
# screen, so the capability would have refused to run for any other member.
#
# One run cannot tell an application's chrome from one record's data, because it
# only ever sees one record. Two runs with different inputs can: text identical
# across both is chrome, text that differs is data — the same comparison outcome
# learning uses, asking about sameness instead of difference. That is where screen
# derivation belongs, and until it exists a capability declares screens because a
# human wrote them at approval, or it declares none and makes no claim about where
# it is.


def prune(steps: list[Any], observations: list[Any]) -> list[Any]:
    """Drop steps that did not advance the state.

    Conservative on purpose. Anything more aggressive would be deciding, after the
    fact, that a step the run verified at the time was unnecessary — and the cost
    of being wrong is an artifact that skips a step the application needed.
    """
    kept: list[Any] = []
    read: set[str] = set()
    for step in steps:
        # A second read of a value already read under the same name adds nothing
        # to the contract and one more thing to go wrong on replay.
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
    # Ids are left alone here. They are what ties an extraction to the output it
    # populates, and renumbering mid-pipeline is how that link gets quietly cut —
    # see `_renumber`, which does it at the end and remaps the outputs with it.
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
        # An input that appears nowhere in the recording is not a parameter of
        # this flow. Declaring it would tell a calling agent it can steer
        # something it cannot.
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
        # Rejected, not trusted, for one of two reasons: the phrase is not on the
        # screen the model just looked at, or it is a transcript of the screen
        # rather than a proof that the goal was reached. The run's own verified
        # success text — the one `finish` was checked against — replaces it.
        proposal["success_text"] = state.success_text
        proposal["success_text_rejected"] = success_text
    if not proposal.get("success_text"):
        proposal["success_text"] = state.success_text

    kept, rejected = _falsify(proposal.get("business_outcomes", ()), state)
    proposal["business_outcomes"] = kept
    proposal["business_outcomes_rejected"] = rejected

    declared: dict[str, Any] = dict(proposal)
    state.declaration = declared
    return declared


def _falsify(proposed: Any, state: Any) -> tuple[list[Any], list[Any]]:
    """Drop outcome detectors that fire on the successful run.

    An outcome describes a screen this run did not visit, so its detector cannot
    be confirmed. It can be refuted: a phrase visible while the flow *succeeded*
    cannot be what distinguishes it having gone another way. Measured on a real
    recording, the model proposed "Accounts" — a column header on every screen —
    which would have classified every success as a business outcome.

    Rejections are returned rather than discarded. The synthesis note is what a
    reviewer reads before approving, and "what was proposed and thrown away" is
    part of judging whether the rest is trustworthy.
    """
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
) -> Capability:
    """Full pipeline. Validates the result parses as a `Capability` before returning
    — a synthesis that produces an unloadable artifact is a failed run, not a
    successful one with a bad file."""
    steps = prune(state.steps, state.observations)
    steps, specs = parameterize(steps, inputs)
    proposal = await declare(state, llm, inputs)

    # Outputs are read off the recording's own step ids, then both are renumbered
    # together. The artifact a reviewer reads is numbered 1..n; the association
    # between an extraction and the value it produces survives it.
    outputs = _outputs(steps, state)
    steps, renumbered = _renumber(steps)
    outputs = [o.model_copy(update={"from_step": renumbered[o.from_step]}) for o in outputs]
    success = Checkpoint(
        kind=CheckKind.TEXT_PRESENT,
        value=str(proposal.get("success_text") or state.success_text or ""),
    )

    cap = Capability(
        id=capability_id or _slug(state.goal),
        status=Status.DRAFT,
        goal=state.goal,
        description=str(proposal.get("description", "")),
        app=app or AppRef(name="unknown", base_url_pattern=".*"),
        inputs=specs,
        outputs=outputs,
        steps=[_typed(s) for s in steps],
        success=success,
        business_outcomes=[
            BusinessOutcome(
                name=_slug(str(o.get("name", ""))),
                description=str(o.get("description", "")),
                detector=Checkpoint(
                    kind=CheckKind.TEXT_PRESENT, value=str(o.get("detector_text", ""))
                ),
                result_fields={spec.name: spec.type for spec in specs},
            )
            for o in proposal.get("business_outcomes", ())
            if o.get("name") and o.get("detector_text")
        ],
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
            "name": sub(target.name),
            "intent": sub(target.intent),
            "target_desc": sub(target.target_desc),
        }
    )


def _outputs(steps: list[Any], state: Any) -> list[OutputSpec]:
    """The output contract, read off the recording.

    Every extraction already carries the name the model chose and the step it came
    from. Asking a second model call to re-derive the outputs would introduce the
    one thing a contract cannot have: two sources of truth about its own shape.
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
    # A string stays a string even when it looks like a number: member ids and
    # account numbers have leading zeros, and turning "007" into 7 is how a
    # capability starts looking up the wrong member.
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
