"""The caller's side of a capability: inputs in, typed outputs out.

The only part of replay that never looks at a screen, answerable from the artifact and
the caller's arguments alone — which is why it runs *before* anything is touched. A type
error should be a rejected call, not a run that gets four steps in and types "None" into
an amount field.
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

    `None` was the whole test until a replay was called with
    `account_nickname=""`. Empty is not missing to a dict, so the contract passed
    it, `{{account_nickname}}` rendered to nothing, the anchor tier skipped itself
    for want of a needle, and the ladder fell to "any text element" and returned
    the first row's balance as a success. A blank required input is the caller
    saying nothing, and the place to say so is here, before a browser is opened.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def validate_inputs(
    cap: Capability, inputs: dict[str, Any], require_approved: bool = False
) -> dict[str, Any]:
    """Coerce and check the caller's arguments against the declared `InputSpec`s.

    Anything the caller sent that the capability does not declare is dropped
    rather than passed through: a template can only reference declared inputs, and
    silently accepting extras invites a caller to believe they are steering
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
        # Coercion and constraints share one handler on purpose. Both are "the
        # caller sent something this capability cannot accept", both name the
        # input and the rule, and splitting them once let a constraint violation
        # escape as a bare ValueError instead of a structured result.
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

    `extracted` is keyed by step id, which is why `OutputSpec.from_step` has to
    name a step that actually extracts — checked when the artifact is constructed
    (`Capability._referentially_intact`), so by the time we get here the only way
    to arrive at `None` is that the step ran and read nothing.
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

        # Typed is not the same as plausible. Declared bounds are the only check
        # standing between a misread digit and a number the caller will act on,
        # and it is a *different* answer from "could not read it": that one sends
        # an operator to look at the screen, this one says the screen was read and
        # what came back cannot be right.
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
        # "transfer from X to Y" where X == Y. A validation rule the application
        # would also enforce, caught before we touch it.
        raise ValueError(f"must differ from {c.not_equal_to}")
