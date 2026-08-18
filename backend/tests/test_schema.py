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
    Constraints,
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
                on_found_action=Primitive.EXTRACT,
                on_found_extract_as="balance",
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


# ---------------------------------------------------------------------------
# referential integrity
#
# Each of these is otherwise an artifact that loads fine, passes review, and
# fails halfway through a run against a member's account. Construction is the
# cheap place to find out; mid-run is the expensive one.
# ---------------------------------------------------------------------------


def _valid(**overrides: object) -> Capability:
    base: dict[str, object] = {
        "id": "cap_valid",
        "goal": "read a balance",
        "app": AppRef(name="targetapp", base_url_pattern="^http://targetapp:8080(/.*)?$"),
        "inputs": [InputSpec(name="member_id", type=ValueType.STRING)],
        "outputs": [OutputSpec(name="balance", type=ValueType.NUMBER, from_step=2)],
        "steps": [
            ActStep(
                id=1,
                action=Primitive.NAVIGATE,
                value="http://targetapp:8080/members/{{member_id}}",
            ),
            ActStep(id=2, action=Primitive.EXTRACT, extract_as="balance"),
        ],
        "success": Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Member Profile"),
    }
    base.update(overrides)
    return Capability(**base)  # type: ignore[arg-type]


def test_a_well_formed_capability_is_accepted() -> None:
    assert _valid().ref == "cap_valid@v1"


def test_duplicate_step_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate step ids"):
        _valid(
            steps=[
                ActStep(id=1, action=Primitive.EXTRACT, extract_as="balance"),
                ActStep(id=1, action=Primitive.CLICK, target=_target(anchor_text="View")),
            ],
            outputs=[OutputSpec(name="balance", type=ValueType.NUMBER, from_step=1)],
        )


def test_an_output_reading_from_a_missing_step_is_rejected() -> None:
    with pytest.raises(ValidationError, match="missing step 9"):
        _valid(outputs=[OutputSpec(name="balance", type=ValueType.NUMBER, from_step=9)])


def test_an_output_reading_from_a_step_that_does_not_extract_is_rejected() -> None:
    """The subtle one: the step exists, so nothing looks wrong until the caller's
    contract silently comes back a field short."""
    with pytest.raises(ValidationError, match="does not extract"):
        _valid(outputs=[OutputSpec(name="balance", type=ValueType.NUMBER, from_step=1)])


def test_a_placeholder_naming_no_declared_input_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"undeclared input \{\{account\}\}"):
        _valid(success=Checkpoint(kind=CheckKind.TEXT_PRESENT, value="{{account}} balance"))


def test_a_placeholder_in_a_predicate_is_checked_too() -> None:
    with pytest.raises(ValidationError, match=r"undeclared input \{\{merchant\}\}"):
        _valid(
            steps=[
                FindAndActStep(
                    id=2,
                    scope=_target(anchor_text="Date | Merchant | Amount"),
                    predicate=Predicate(terms=("{{merchant}}",)),
                    on_found_action=Primitive.EXTRACT,
                    on_found_extract_as="balance",
                )
            ]
        )


def test_a_step_expecting_an_undeclared_screen_is_rejected() -> None:
    with pytest.raises(ValidationError, match="undeclared screen"):
        _valid(
            steps=[
                ActStep(id=1, action=Primitive.NAVIGATE, value="http://targetapp:8080/"),
                ActStep(
                    id=2,
                    action=Primitive.EXTRACT,
                    extract_as="balance",
                    screen="member_profile",
                ),
            ]
        )


def test_a_constraint_naming_an_undeclared_input_is_rejected() -> None:
    with pytest.raises(ValidationError, match="undeclared input 'to_account'"):
        _valid(
            inputs=[
                InputSpec(
                    name="member_id",
                    type=ValueType.STRING,
                    constraints=Constraints(not_equal_to="to_account"),
                )
            ]
        )


def test_a_risky_step_may_not_declare_a_retry() -> None:
    """`risk: risky` says the action is irreversible and `on_error: retry` asks
    for it to be run twice. A file that says both is a duplicate transfer waiting
    for a slow checkpoint, and refusing it at load time is cheaper than refusing
    it mid-run — by then the artifact has been reviewed and approved."""
    from cua.schema import OnError, Risk

    with pytest.raises(ValidationError, match="cannot be retried"):
        _valid(
            steps=[
                ActStep(
                    id=1,
                    action=Primitive.CLICK,
                    target=_target(anchor_text="Confirm Transfer"),
                    risk=Risk.RISKY,
                    on_error=OnError.RETRY,
                    retries=2,
                ),
                ActStep(id=2, action=Primitive.EXTRACT, extract_as="balance"),
            ]
        )


def test_a_retry_with_no_budget_is_rejected() -> None:
    """Otherwise `on_error: retry, retries: 0` reads as a retry policy and behaves
    as `hard_fail` — the worst kind of field, one that describes a behaviour the
    system does not have."""
    from cua.schema import OnError

    with pytest.raises(ValidationError, match="no retry budget"):
        _valid(
            steps=[
                ActStep(
                    id=1,
                    action=Primitive.CLICK,
                    target=_target(anchor_text="Search"),
                    on_error=OnError.RETRY,
                ),
                ActStep(id=2, action=Primitive.EXTRACT, extract_as="balance"),
            ]
        )


def test_an_uncompilable_base_url_pattern_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a valid regex"):
        _valid(app=AppRef(name="targetapp", base_url_pattern="^http://[unclosed"))


def test_a_declared_outcome_may_use_declared_inputs() -> None:
    """The validator must not reject the legitimate case it exists to police."""
    cap = _valid(
        business_outcomes=[
            BusinessOutcome(
                name="member_not_found",
                detector=Checkpoint(
                    kind=CheckKind.TEXT_PRESENT, value="no member {{member_id}}"
                ),
            )
        ]
    )
    assert cap.business_outcomes[0].name == "member_not_found"
