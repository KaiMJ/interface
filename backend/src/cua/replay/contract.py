"""The caller's side of a capability: inputs in, typed outputs out.

The only part of replay that never looks at a screen — answerable from the artifact and the
caller's arguments alone, which is why it runs *before* anything is touched. A type error
should be a rejected call, not a run that gets four steps in and types "None" into an amount.
"""

from __future__ import annotations

import re
from typing import Any

from ..resolve import apply
from ..schema import Capability, FailureDetail, FailureKind, Status, ValueType


class ContractError(Exception):
    """A structured rejection. The engine turns it into a terminal result."""

    def __init__(self, failure: FailureDetail) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _absent(value: Any) -> bool:
    """Nothing supplied — including a string that only looks like it was.

    Empty is not missing to a dict, so without this an empty input renders its placeholder to
    nothing, the anchor tier has no needle, and the ladder falls through to "any text element".
    """
    return value is None or (isinstance(value, str) and not value.strip())


def validate_inputs(
    cap: Capability, inputs: dict[str, Any], require_approved: bool = False
) -> dict[str, Any]:
    """Coerce and check the caller's arguments against the declared `InputSpec`s.

    Undeclared arguments are dropped rather than passed through: a template can only reference
    declared inputs, and accepting extras invites a caller to believe they are steering
    something.
    """
    if require_approved and cap.status is not Status.APPROVED:
        raise ContractError(
            FailureDetail(
                kind=FailureKind.POLICY_DENIED,
                message=f"{cap.ref} is {cap.status.value}; unattended replay needs approval",
            )
        )

    params: dict[str, Any] = {}
    for spec in cap.inputs:
        if spec.name not in inputs or _absent(inputs[spec.name]):
            if spec.required:
                raise ContractError(
                    FailureDetail(
                        kind=FailureKind.INTERNAL,
                        message=f"missing required input {spec.name!r}",
                    )
                )
            continue
        # Coercion and constraints share one handler: both mean "the caller sent something
        # this capability cannot accept", and both must reach a structured result.
        try:
            params[spec.name] = coerce(inputs[spec.name], spec.type)
            check_constraints(spec, params[spec.name], params)
        except (TypeError, ValueError) as e:
            raise ContractError(
                FailureDetail(
                    kind=FailureKind.INTERNAL,
                    message=f"input {spec.name!r}: {e}",
                    expected=spec.type.value,
                    observed=repr(inputs[spec.name]),
                )
            ) from e

    return params


def extract_outputs(cap: Capability, extracted: dict[int, Any]) -> dict[str, Any]:
    """Read declared outputs from what the extract steps recorded.

    `extracted` is keyed by step id, which is why `OutputSpec.from_step` has to name a step
    that extracts — checked at construction, so the only way to reach `None` here is that the
    step ran and read nothing.
    """
    outputs: dict[str, Any] = {}
    for spec in cap.outputs:
        raw = extracted.get(spec.from_step)
        if raw is None:
            if spec.required:
                raise ContractError(
                    FailureDetail(
                        kind=FailureKind.EXTRACTION_FAILED,
                        step_id=spec.from_step,
                        message=f"no value was extracted for output {spec.name!r}",
                    )
                )
            continue
        try:
            if isinstance(raw, list):
                outputs[spec.name] = [coerce(apply(v, spec.normalize), spec.type) for v in raw]
            else:
                outputs[spec.name] = coerce(apply(raw, spec.normalize), spec.type)
        except (TypeError, ValueError) as e:
            raise ContractError(
                FailureDetail(
                    kind=FailureKind.EXTRACTION_FAILED,
                    step_id=spec.from_step,
                    message=f"output {spec.name!r} is not a {spec.type.value}: {e}",
                    observed=str(raw)[:200],
                )
            ) from e

        # Typed is not the same as plausible: declared bounds are what stands between a misread
        # digit and a number the caller acts on. Distinct from "could not read it" — this says
        # the screen was read and what came back cannot be right.
        try:
            values = outputs[spec.name]
            for v in values if isinstance(values, list) else [values]:
                check_constraints(spec, v, {})
        except (TypeError, ValueError) as e:
            raise ContractError(
                FailureDetail(
                    kind=FailureKind.OUTPUT_REJECTED,
                    step_id=spec.from_step,
                    message=f"output {spec.name!r} is out of contract: {e}",
                    expected=_bounds(spec),
                    observed=str(outputs[spec.name])[:200],
                )
            ) from e
    return outputs


def _bounds(spec: Any) -> str:
    """The declared constraint, as the operator needs to read it in the failure."""
    c = spec.constraints
    parts = [
        f"pattern {c.pattern}" if c.pattern else "",
        f"min {c.min}" if c.min is not None else "",
        f"max {c.max}" if c.max is not None else "",
        f"one of {list(c.choices)}" if c.choices else "",
    ]
    return f"{spec.name}: " + ", ".join(p for p in parts if p)


def coerce(value: Any, kind: ValueType) -> Any:
    if kind is ValueType.INTEGER:
        return int(str(value).strip())
    if kind is ValueType.NUMBER:
        return float(str(value).strip())
    if kind is ValueType.BOOLEAN:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "1")
    return str(value).strip()


def check_constraints(spec: Any, value: Any, params: dict[str, Any]) -> None:
    c = spec.constraints
    if c is None:
        return
    if c.pattern and not re.match(c.pattern, str(value)):
        raise ValueError(f"{value!r} does not match {c.pattern}")
    if c.min is not None and float(value) < c.min:
        raise ValueError(f"{value!r} is below the minimum {c.min}")
    if c.max is not None and float(value) > c.max:
        raise ValueError(f"{value!r} is above the maximum {c.max}")
    if c.choices and str(value) not in c.choices:
        raise ValueError(f"{value!r} is not one of {c.choices}")
    if c.not_equal_to and str(value) == str(params.get(c.not_equal_to)):
        # "transfer from X to Y" where X == Y — a rule the application would also enforce,
        # caught before we touch it.
        raise ValueError(f"must differ from {c.not_equal_to}")
