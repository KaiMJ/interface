"""Schema tests.

The schemas are the deliverable most likely to be read closely, so they get the
tests. Everything else is stubbed and will get tests as it lands.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cua.schema import (
    ActStep,
    AppRef,
    Bbox,
    BusinessOutcome,
    Capability,
    CheckKind,
    Checkpoint,
    FindAndActStep,
    InputSpec,
    OutputSpec,
    Predicate,
    Primitive,
    Risk,
    Target,
    ValueType,
)


def _target(**kw: object) -> Target:
    base = {"intent": "click the Search button", "target_desc": "primary submit on the search form"}
    base.update(kw)
    return Target(**base)  # type: ignore[arg-type]


def test_bbox_center_and_iou() -> None:
    b = Bbox(x=0.2, y=0.4, w=0.2, h=0.2)
    assert b.center.x == pytest.approx(0.3)
    assert b.center.y == pytest.approx(0.5)
    assert b.iou(b) == pytest.approx(1.0)
    assert b.iou(Bbox(x=0.8, y=0.8, w=0.1, h=0.1)) == 0.0


def test_bbox_rejects_out_of_range() -> None:
    """Coordinates are normalized. A pixel value slipping in should not parse."""
    with pytest.raises(ValidationError):
        Bbox(x=640, y=400, w=100, h=40)


def test_artifacts_are_frozen() -> None:
    t = _target(anchor_text="Search")
    with pytest.raises(ValidationError):
        t.intent = "something else"  # type: ignore[misc]


def test_step_union_discriminates() -> None:
    """A capability round-trips through JSON with both step kinds intact."""
    cap = Capability(
        id="cap_test",
        goal="test",
        app=AppRef(name="targetapp", base_url_pattern="http://targetapp:8080"),
        inputs=[InputSpec(name="member_id", type=ValueType.STRING)],
        outputs=[OutputSpec(name="balance", type=ValueType.NUMBER, from_step=2)],
        steps=[
            ActStep(id=1, action=Primitive.CLICK, target=_target(anchor_text="Search")),
            FindAndActStep(
                id=2,
                scope=_target(anchor_text="Account | Type | Balance"),
                predicate=Predicate(terms=("{{member_id}}",)),
                risk=Risk.SAFE,
            ),
        ],
        success=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Available balance"),
    )
    reloaded = Capability.model_validate_json(cap.model_dump_json())
    assert [s.kind for s in reloaded.steps] == ["act", "find_and_act"]
    assert reloaded.ref == "cap_test@v1"


def test_unknown_field_rejected() -> None:
    """extra='forbid' — a typo in a hand-edited artifact must not be ignored."""
    with pytest.raises(ValidationError):
        Checkpoint(kind=CheckKind.TEXT_PRESENT, value="x", tolerance=0.5)  # type: ignore[call-arg]


def test_find_and_act_defaults_to_escalating_on_ambiguity() -> None:
    """Acting on the wrong record is unrecoverable; the safe default must be the
    one you get without thinking about it."""
    step = FindAndActStep(
        id=1,
        scope=_target(anchor_text="Date | Description | Amount"),
        predicate=Predicate(terms=("{{merchant}}",)),
    )
    assert step.on_multiple.value == "escalate"
    assert step.scan.overlap > 0, "a zero-overlap scan skips rows straddling the boundary"


def test_business_outcome_is_not_a_failure_shape() -> None:
    """A business outcome carries typed result fields, not an error string."""
    bo = BusinessOutcome(
        name="member_not_found",
        detector=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="No member matches"),
        result_fields={"member_id": ValueType.STRING},
    )
    assert "member_id" in bo.result_fields
