"""Resolver, normalizers and verification.

The replay path's decision layer: does it find the right control, does it admit when it is
guessing, and does it refuse to act when what it is looking at is not what was recorded. Every
case runs on a synthetic observation — none of this needs pixels.
"""

from __future__ import annotations

import pytest

from cua.resolve import (
    MissingParam,
    Resolver,
    Unresolvable,
    evaluate,
    point_in,
    render,
    unrender,
    verify_effect,
    verify_target,
)
from cua.resolve.normalize import (
    apply,
    date_iso,
    strip_currency,
    strip_ellipsis,
    strip_punct,
)
from cua.schema import (
    Bbox,
    CheckKind,
    Checkpoint,
    Element,
    ElementSource,
    FailureKind,
    MatchMode,
    Normalizer,
    Observation,
    Relation,
    ResolutionTier,
    Target,
    Viewport,
)

VIEWPORT = Viewport(width=1440, height=900)


def el(
    id_: str,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str | None = None,
    role: str = "text",
    source: ElementSource = ElementSource.OCR,
) -> Element:
    return Element(
        id=id_,
        role=role,
        name=text,
        text=text,
        bbox=Bbox(x=x, y=y, w=w, h=h),
        source=source,
        conf=0.9,
    )


def observation(*elements: Element, url: str | None = None) -> Observation:
    return Observation(
        screenshot_path="/tmp/frame.png",
        viewport=VIEWPORT,
        elements=elements,
        url=url,
        frame_hash="abc123",
        taken_at="2026-08-16T00:00:00+00:00",
    )


ACCOUNTS = observation(
    el("e0", 0.05, 0.20, 0.10, 0.02, "29883"),
    el("e1", 0.20, 0.20, 0.12, 0.02, "Everyday Checking"),
    el("e2", 0.40, 0.20, 0.08, 0.02, "$4,820.19"),
    el("e3", 0.60, 0.20, 0.04, 0.02, "View", role="button", source=ElementSource.OMNIPARSER),
    el("e4", 0.05, 0.25, 0.10, 0.02, "29455"),
    el("e5", 0.20, 0.25, 0.12, 0.02, "Primary Savings"),
    el("e6", 0.40, 0.25, 0.08, 0.02, "$18,204.55"),
    el("e7", 0.60, 0.25, 0.04, 0.02, "View", role="button", source=ElementSource.OMNIPARSER),
    url="http://targetapp:8080/members/12345",
)


# ---------------------------------------------------------------------------
# normalizers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("$4,820.19", "4820.19"),
        ("($441.56)", "-441.56"),
        ("($1,234.56)", "-1234.56"),
        ("1234.56", "1234.56"),
        ("(see reverse)", "(see reverse)"),  # parentheses are not a minus sign here
    ],
)
def test_strip_currency(raw: str, expected: str) -> None:
    assert strip_currency(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("07/14/2026", "2026-07-14"),
        ("07-14-26", "2026-07-14"),
        ("Jul 14, 2026", "2026-07-14"),
        ("2026-07-14", "2026-07-14"),
        ("sometime last week", "sometime last week"),  # never invent data
    ],
)
def test_date_iso(raw: str, expected: str) -> None:
    assert date_iso(raw) == expected


@pytest.mark.parametrize(
    "fn_and_input",
    [
        (strip_currency, "($1,234.56)"),
        (date_iso, "07/14/2026"),
        (strip_ellipsis, "ACME Corporat..."),
        (strip_punct, "Member ID: 12345"),
    ],
)
def test_normalizers_are_idempotent(fn_and_input: tuple[object, str]) -> None:
    # Artifacts declare a normalizer list, and nothing stops a recording from
    # declaring one twice. Applying a normalizer twice must not change the answer.
    fn, raw = fn_and_input
    once = fn(raw)  # type: ignore[operator]
    assert fn(once) == once  # type: ignore[operator]


def test_apply_runs_in_the_declared_order() -> None:
    # strip_currency before strip_punct, or the decimal point is gone before the
    # number is parsed — which is why the order lives in the artifact.
    money = "$1,234.56"
    assert apply(money, (Normalizer.STRIP_CURRENCY, Normalizer.STRIP_PUNCT)) == "1234 56"
    assert apply(money, (Normalizer.STRIP_PUNCT, Normalizer.STRIP_CURRENCY)) == "1 234 56"


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def test_render_substitutes_declared_inputs() -> None:
    assert render("the row for member {{member_id}}", {"member_id": 12345}) == (
        "the row for member 12345"
    )


def test_render_refuses_to_silently_drop_a_missing_parameter() -> None:
    # An empty anchor matches the first row on the page, so this must fail loudly.
    with pytest.raises(MissingParam):
        render("member {{member_id}}", {})


# ---------------------------------------------------------------------------
# the resolver ladder
# ---------------------------------------------------------------------------


def test_anchor_text_beats_a_stale_recorded_box() -> None:
    target = Target(
        intent="click View on the savings account row",
        target_desc="the View button in the savings row",
        anchor_text="{{account_id}}",
        # Recorded when the row was one position higher — the everyday case, not
        # drift: the list above it had a different number of rows that day.
        bbox=Bbox(x=0.05, y=0.20, w=0.10, h=0.02),
    )
    r = Resolver().resolve(target, ACCOUNTS, {"account_id": "29455"})
    assert r.tier is ResolutionTier.ANCHOR_TEXT
    assert r.matched_text == "29455"
    assert r.drift is False
    assert r.point.y == pytest.approx(0.26)


def test_ambiguity_is_reported_not_silently_resolved() -> None:
    target = Target(intent="click View", target_desc="a View button", anchor_text="View")
    r = Resolver().resolve(target, ACCOUNTS)
    # Two rows have a View button. The caller decides: tolerable on a read,
    # escalate on a write.
    assert r.candidates == 2


def test_a_recorded_box_disambiguates_without_hiding_the_ambiguity() -> None:
    target = Target(
        intent="click View on the savings row",
        target_desc="the View button in the savings row",
        anchor_text="View",
        bbox=Bbox(x=0.60, y=0.25, w=0.04, h=0.02),
    )
    r = Resolver().resolve(target, ACCOUNTS)
    assert r.candidates == 2
    assert r.bbox.y == pytest.approx(0.25)


def test_role_name_tier_when_the_anchor_is_gone() -> None:
    target = Target(
        intent="click View",
        target_desc="a View button",
        anchor_text="Ver Detalles",  # a differently-branded tenant, say
        role="button",
        name="View",
    )
    r = Resolver().resolve(target, ACCOUNTS)
    assert r.tier is ResolutionTier.ROLE_NAME


def test_a_stale_role_does_not_veto_a_correct_name_match() -> None:
    # Normalized thresholds, so a wider window reads the View button as "control" where
    # the recording said "button". The name still identifies it.
    observed = observation(
        el("e0", 0.05, 0.20, 0.10, 0.02, "29883"),
        el("e1", 0.60, 0.20, 0.04, 0.02, "View", role="control",
           source=ElementSource.OMNIPARSER),
    )
    target = Target(
        intent="click View",
        target_desc="a View button",
        anchor_text="Ver Detalles",
        role="button",
        name="View",
    )
    r = Resolver().resolve(target, observed)
    assert r.tier is ResolutionTier.ROLE_NAME
    assert r.bbox.x == pytest.approx(0.60)


def test_role_still_narrows_when_both_are_declared() -> None:
    # Advisory is not ignored: two same-named elements, and the role picks between them.
    observed = observation(
        el("e0", 0.20, 0.20, 0.06, 0.02, "View", role="text"),
        el("e1", 0.60, 0.20, 0.04, 0.02, "View", role="button",
           source=ElementSource.OMNIPARSER),
    )
    target = Target(
        intent="click View",
        target_desc="a View button",
        anchor_text="Ver Detalles",
        role="button",
        name="View",
    )
    r = Resolver().resolve(target, observed)
    assert r.tier is ResolutionTier.ROLE_NAME
    assert r.bbox.x == pytest.approx(0.60)


def test_role_alone_is_still_a_hard_filter() -> None:
    # No name to fall back on, so role is the only key the tier has.
    target = Target(
        intent="click the export icon",
        target_desc="the export icon",
        anchor_text="Export",
        role="icon",
    )
    # Nothing here is an icon and there is no recorded box, so every tier misses; degrading
    # role would match all eight.
    with pytest.raises(Unresolvable):
        Resolver().resolve(target, ACCOUNTS)


def test_recorded_bbox_is_the_last_tier_and_flags_drift() -> None:
    target = Target(
        intent="click the export icon",
        target_desc="the export icon, which has no text",
        anchor_text="Export",
        bbox=Bbox(x=0.90, y=0.10, w=0.03, h=0.02),
    )
    r = Resolver().resolve(target, ACCOUNTS)
    assert r.tier is ResolutionTier.RECORDED_BBOX
    # The drift signal: aggregated across runs, anchors decaying into boxes is an early
    # warning.
    assert r.drift is True


def test_resolution_exhausted_rather_than_a_guess() -> None:
    target = Target(
        intent="click a control that is not on this screen",
        target_desc="something that does not exist",
        anchor_text="Nowhere",
    )
    with pytest.raises(Unresolvable):
        Resolver().resolve(target, ACCOUNTS)


def test_replay_resolver_cannot_reach_a_model_by_construction() -> None:
    # `build_replay` hands the engine a resolver with allow_vlm=False. Determinism
    # is a construction-time property, not a promise the engine keeps.
    with pytest.raises(RuntimeError):
        Resolver(allow_vlm=False)._by_vlm(
            Target(intent="x", target_desc="x"), ACCOUNTS
        )


def test_point_in_targets_the_right_edge_of_a_matched_row() -> None:
    row = Bbox(x=0.05, y=0.25, w=0.60, h=0.02)
    p = point_in(row, (0.95, 0.5))
    assert p.x == pytest.approx(0.62)
    assert p.y == pytest.approx(0.26)


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def test_verify_target_passes_when_the_region_reads_as_recorded() -> None:
    target = Target(
        intent="click View on the savings row",
        target_desc="the View button in the savings row",
        anchor_text="View",
        bbox=Bbox(x=0.60, y=0.25, w=0.04, h=0.02),
    )
    r = Resolver().resolve(target, ACCOUNTS)
    assert verify_target(target, r, ACCOUNTS).ok


def test_verify_target_catches_a_recorded_box_now_pointing_elsewhere() -> None:
    # The bbox tier resolved, so the click *would* have happened. What stops it is
    # that the region now reads "Primary Savings" and the recording said "View".
    target = Target(
        intent="click View",
        target_desc="the View button",
        anchor_text="Ver",  # anchor no longer present -> falls through to bbox
        anchor_match=MatchMode.EXACT,
        bbox=Bbox(x=0.20, y=0.25, w=0.12, h=0.02),
    )
    r = Resolver().resolve(target, ACCOUNTS)
    assert r.tier is ResolutionTier.RECORDED_BBOX
    result = verify_target(target, r, ACCOUNTS)
    assert not result.ok
    assert result.kind is FailureKind.TARGET_MISMATCH
    assert result.expected == "Ver"
    assert "Primary Savings" in (result.observed or "")


def test_verify_target_refuses_to_click_through_an_overlay() -> None:
    # A confirmation dialog has opened over the accounts table, so the row's own text is no
    # longer readable, the anchor misses, and the step falls through to a recorded coordinate
    # that is still 'correct' and would click the dialog.
    obs = observation(
        el("e0", 0.10, 0.10, 0.30, 0.02, "Member Services"),
        el(
            "e1",
            0.20,
            0.15,
            0.60,
            0.40,
            "Confirm transfer? Cancel Confirm",
            role="control",
            source=ElementSource.OMNIPARSER,
        ),
    )
    target = Target(
        intent="click View on the savings row",
        target_desc="the View button in the savings row",
        anchor_text="View",
        bbox=Bbox(x=0.60, y=0.25, w=0.04, h=0.02),
    )
    r = Resolver().resolve(target, obs)
    assert r.tier is ResolutionTier.RECORDED_BBOX
    result = verify_target(target, r, obs)
    assert not result.ok
    assert result.kind is FailureKind.TARGET_MISMATCH  # the words are gone
    assert "Confirm transfer" in (result.observed or "")


def test_verify_target_reports_an_overlay_when_there_is_nothing_to_assert() -> None:
    # Same dialog, but the step targets an icon with no text of its own, so the mismatch check
    # cannot fire and the geometric one is all that is left.
    obs = observation(
        el(
            "e0",
            0.20,
            0.15,
            0.60,
            0.40,
            "Confirm transfer? Cancel Confirm",
            role="control",
            source=ElementSource.OMNIPARSER,
        ),
    )
    target = Target(
        intent="click the export icon",
        target_desc="the export icon, which has no text",
        bbox=Bbox(x=0.60, y=0.25, w=0.02, h=0.02),
    )
    r = Resolver().resolve(target, obs)
    result = verify_target(target, r, obs)
    assert not result.ok
    assert result.kind is FailureKind.UNEXPECTED_OVERLAY


# ---------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------


def test_text_present_tolerates_the_formats_the_artifact_declared() -> None:
    check = Checkpoint(
        kind=CheckKind.TEXT_PRESENT,
        value="18204.55",
        normalize=(Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS, Normalizer.STRIP_CURRENCY),
    )
    assert evaluate(check, ACCOUNTS)


def test_text_absent_is_the_same_computation_inverted() -> None:
    assert evaluate(Checkpoint(kind=CheckKind.TEXT_ABSENT, value="Session expired"), ACCOUNTS)
    assert not evaluate(Checkpoint(kind=CheckKind.TEXT_ABSENT, value="Primary Savings"), ACCOUNTS)


def test_a_scoped_checkpoint_does_not_quietly_widen_to_the_whole_page() -> None:
    # "$4,820.19 appears in the savings row" is false even though it appears on the page; a
    # scope that silently widened would pass.
    check = Checkpoint(
        kind=CheckKind.TEXT_PRESENT,
        value="4820.19",
        scope=Target(
            intent="the savings row",
            target_desc="the row for the savings account",
            anchor_text="Primary Savings",
        ),
        normalize=(Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS, Normalizer.STRIP_CURRENCY),
    )
    assert not evaluate(check, ACCOUNTS)


def test_url_matches_reads_the_surface_url_when_there_is_one() -> None:
    assert evaluate(Checkpoint(kind=CheckKind.URL_MATCHES, value="/members/12345"), ACCOUNTS)
    assert not evaluate(
        Checkpoint(kind=CheckKind.URL_MATCHES, value="/members/99999"), ACCOUNTS
    )


def test_verify_effect_reports_expected_next_to_observed() -> None:
    check = Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Transfer complete")
    result = verify_effect(check, ACCOUNTS)
    assert not result.ok
    assert result.kind is FailureKind.CHECKPOINT_FAILED
    assert "Transfer complete" in (result.expected or "")
    # The observed side is what the screen actually said; the two strings side by side are the
    # debugging story for a failed replay.
    assert "Everyday Checking" in (result.observed or "")


def test_checkpoint_parameters_are_substituted_before_matching() -> None:
    check = Checkpoint(kind=CheckKind.TEXT_PRESENT, value="{{account_id}}")
    assert evaluate(check, ACCOUNTS, {"account_id": "29455"})
    assert not evaluate(check, ACCOUNTS, {"account_id": "70001"})


def test_a_value_inside_a_longer_number_is_not_parameterized() -> None:
    """Synthesis rewrites recorded literals into placeholders by exact match. A bare string
    replace does that inside longer runs of digits too, so an account number containing the
    member id becomes a URL that exists for nobody — and navigates somewhere valid.
    """
    params = {"member_id": "12345"}

    assert unrender("Account 9912345 for member 12345", params) == (
        "Account 9912345 for member {{member_id}}"
    )
    # Still substituted where an application actually puts a value: against
    # punctuation, at a path segment, at the end of a sentence.
    assert unrender("http://app/members/12345", params) == "http://app/members/{{member_id}}"
    assert unrender("ID #12345.", params) == "ID #{{member_id}}."


def test_the_longest_input_is_substituted_first() -> None:
    """Two inputs where one value is a prefix of the other. Boundaries do not help
    here — both matches are properly bounded — so the ordering still has to."""
    params = {"branch": "123", "member_id": "12345"}

    assert unrender("member 12345 at branch 123", params) == (
        "member {{member_id}} at branch {{branch}}"
    )


# ---------------------------------------------------------------------------
# a value in a table is addressed by its column, not by counting cells
# ---------------------------------------------------------------------------

ACCOUNTS_TABLE = observation(
    el("h0", 0.02, 0.20, 0.08, 0.02, "Account"),
    el("h1", 0.13, 0.20, 0.05, 0.02, "Type"),
    el("h2", 0.24, 0.20, 0.09, 0.02, "Nickname"),
    el("h3", 0.40, 0.20, 0.07, 0.02, "Status"),
    el("h4", 0.49, 0.20, 0.14, 0.02, "Current Balance"),
    el("h5", 0.68, 0.20, 0.16, 0.02, "Available Balance"),
    el("r0", 0.02, 0.28, 0.06, 0.02, "30117"),
    el("r1", 0.13, 0.28, 0.08, 0.02, "Checking"),
    el("r2", 0.24, 0.28, 0.12, 0.02, "Free Checking"),
    el("r3", 0.40, 0.28, 0.06, 0.02, "Active"),
    el("r4", 0.64, 0.28, 0.06, 0.02, "$712.04"),
    el("r5", 0.84, 0.28, 0.06, 0.02, "$712.04"),
)


def _read(target: Target) -> str | None:
    found = Resolver(allow_vlm=False).resolve(target, ACCOUNTS_TABLE)
    hit = next(
        (e for e in ACCOUNTS_TABLE.elements if e.bbox.contains(found.point)),
        None,
    )
    return (hit.text or hit.name or "").strip() if hit else None


def test_counting_cells_reads_the_wrong_one_when_the_anchor_is_ambiguous() -> None:
    """`Checking` matches the Type cell and `Free Checking` alike, so a count starts from
    whichever one resolved and one column of drift lands on the status."""
    counted = Target(
        intent="read the balance",
        target_desc="the value to the right of 'Checking'",
        anchor_text="Checking",
        anchor_match=MatchMode.CONTAINS,
        relation=Relation.RIGHT_OF,
        relation_index=1,
        bbox=Bbox(x=0.64, y=0.28, w=0.06, h=0.02),
    )
    assert _read(counted) == "Active"


def test_naming_the_column_reads_the_balance() -> None:
    """Same ambiguous anchor, same row, right cell.

    The column is a band between two headers, so which cell is in it does not depend
    on how many cells to its left happen to be filled in.
    """
    addressed = Target(
        intent="read the balance",
        target_desc="the value to the right of 'Checking'",
        anchor_text="Checking",
        anchor_match=MatchMode.CONTAINS,
        relation=Relation.RIGHT_OF,
        relation_index=1,
        column="Current Balance",
        bbox=Bbox(x=0.64, y=0.28, w=0.06, h=0.02),
    )
    assert _read(addressed) == "$712.04"


def test_a_column_that_is_not_on_this_screen_falls_back_to_the_count() -> None:
    """Weaker than the recording had, but never worse than before columns existed."""
    absent = Target(
        intent="read the balance",
        target_desc="the value to the right of 'Nickname'",
        anchor_text="Free Checking",
        anchor_match=MatchMode.CONTAINS,
        relation=Relation.RIGHT_OF,
        relation_index=1,
        column="Ledger Balance",
        bbox=Bbox(x=0.64, y=0.28, w=0.06, h=0.02),
    )
    assert _read(absent) == "$712.04"


# ---------------------------------------------------------------------------
# a target that identifies a record, not a control
# ---------------------------------------------------------------------------


def test_an_anchor_that_renders_empty_stops_rather_than_degrades() -> None:
    """No needle is not the same as no anchor.

    The lower tiers answer a different question: `role_name` matches any text element on the
    frame, and the recorded box is where another record sat. Either returns a plausible number
    for a caller who named nothing.
    """
    target = Target(
        intent="read the balance",
        target_desc="the value to the right of the account",
        anchor_text="{{account_nickname}}",
        relation=Relation.RIGHT_OF,
        relation_index=1,
        role="text",
        bbox=Bbox(x=0.64, y=0.28, w=0.06, h=0.02),
    )
    with pytest.raises(Unresolvable):
        Resolver(allow_vlm=False).resolve(target, ACCOUNTS_TABLE, {"account_nickname": ""})


def test_a_record_that_is_absent_is_not_looked_for_by_position() -> None:
    """The anchor is a template, so it names a row rather than a control. Absent means absent:
    falling to the recorded coordinates returns whichever record moved into that position."""
    target = Target(
        intent="read the balance",
        target_desc="the value to the right of the account",
        anchor_text="{{account_nickname}}",
        relation=Relation.RIGHT_OF,
        relation_index=1,
        role="text",
        bbox=Bbox(x=0.64, y=0.28, w=0.06, h=0.02),
    )
    with pytest.raises(Unresolvable):
        Resolver(allow_vlm=False).resolve(
            target, ACCOUNTS_TABLE, {"account_nickname": "No Such Account"}
        )


def test_a_control_still_degrades_the_way_it_always_did() -> None:
    """The rule keys on the template, not on extraction: a button carries no placeholder and
    exists on every run, so a miss really is drift and the lower rungs are right."""
    target = Target(
        intent="click View",
        target_desc="View",
        anchor_text="View",
        role="text",
        bbox=Bbox(x=0.017, y=0.28, w=0.033, h=0.02),
    )
    found = Resolver(allow_vlm=False).resolve(target, ACCOUNTS_TABLE, {})
    # The claim is that it was allowed to fall past the anchor at all, not which rung caught it.
    assert found.tier is not ResolutionTier.ANCHOR_TEXT
