"""Perception unit tests.

Everything here runs without torch, without an X server and without a model
download: the pieces under test are the deterministic ones — row grouping, the
merge rules, spatial queries and the settle loop — and those are exactly the
pieces every later layer trusts blindly.

The detector and text reader are stubbed with fakes rather than mocked, because
what is being asserted is the *composition* (capture -> detect -> read -> merge),
not that a particular call was made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from cua.perception.base import Perceiver, Unsettled
from cua.perception.index import ElementIndex
from cua.perception.merge import infer_role, merge
from cua.perception.ocr import group_rows
from cua.perception.screen import ImageFileScreen
from cua.schema import Bbox, Element, ElementSource, SettledBy, Viewport

VIEWPORT = Viewport(width=1440, height=900)


def text(id_: str, x: float, y: float, w: float, h: float, s: str) -> Element:
    return Element(
        id=id_,
        role="text",
        name=s,
        text=s,
        bbox=Bbox(x=x, y=y, w=w, h=h),
        source=ElementSource.OCR,
        conf=0.9,
    )


def control(id_: str, x: float, y: float, w: float, h: float, conf: float = 0.8) -> Element:
    return Element(
        id=id_,
        bbox=Bbox(x=x, y=y, w=w, h=h),
        source=ElementSource.OMNIPARSER,
        conf=conf,
    )


# ---------------------------------------------------------------------------
# row grouping
# ---------------------------------------------------------------------------


def test_group_rows_clusters_a_table_row_and_keeps_reading_order() -> None:
    cells = [
        text("t1", 0.60, 0.201, 0.08, 0.02, "$120.00"),
        text("t2", 0.10, 0.200, 0.10, 0.02, "2026-01-04"),
        text("t3", 0.30, 0.202, 0.20, 0.02, "ACME Corp"),
    ]
    rows = group_rows(cells)
    assert len(rows) == 1
    assert [e.text for e in rows[0]] == ["2026-01-04", "ACME Corp", "$120.00"]


def test_group_rows_does_not_merge_adjacent_table_rows() -> None:
    # Two rows 30px apart on a 900px display. Merging them would let
    # `row_contains_all` match terms a human reads as two separate records —
    # in a banking app, the wrong transaction.
    rows = group_rows(
        [
            text("a1", 0.1, 0.200, 0.1, 0.018, "row one"),
            text("a2", 0.4, 0.200, 0.1, 0.018, "111"),
            text("b1", 0.1, 0.233, 0.1, 0.018, "row two"),
            text("b2", 0.4, 0.233, 0.1, 0.018, "222"),
        ]
    )
    assert [[e.text for e in r] for r in rows] == [["row one", "111"], ["row two", "222"]]


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_merge_joins_text_into_its_control_and_drops_the_loose_line() -> None:
    out = merge(
        [control("d0", 0.30, 0.50, 0.12, 0.04)],
        [text("t0", 0.32, 0.51, 0.07, 0.02, "Transfer")],
    )
    assert len(out) == 1
    assert out[0].source is ElementSource.OMNIPARSER
    assert out[0].text == "Transfer"
    assert out[0].name == "Transfer"
    assert out[0].role == "button"


def test_merge_gives_text_to_the_smallest_containing_control() -> None:
    card = control("d0", 0.20, 0.40, 0.60, 0.30, conf=0.9)
    button = control("d1", 0.30, 0.50, 0.12, 0.04, conf=0.7)
    out = merge([card, button], [text("t0", 0.32, 0.51, 0.07, 0.02, "Transfer")])
    labelled = [e for e in out if e.text == "Transfer"]
    assert len(labelled) == 1
    # The button, not the card that also contains it.
    assert labelled[0].bbox.w == pytest.approx(0.12)


def test_merge_labels_a_control_whose_box_is_tighter_than_the_ocr_line() -> None:
    # Real geometry from the target app's "View" button: the detector boxes the
    # glyphs (29x16 px), OCR boxes the padded line (41x23 px). Only 49% of the
    # text is inside the control, so a containment-only rule would leave every
    # button on the page anonymous.
    button = control("d0", 474 / 1440, 264 / 900, 29 / 1440, 16 / 900)
    label = text("t0", 468 / 1440, 261 / 900, 41 / 1440, 23 / 900, "View")
    out = merge([button], [label])
    assert len(out) == 1
    assert out[0].text == "View"


def test_merge_does_not_let_an_icon_claim_the_row_of_text_it_sits_in() -> None:
    # The same reverse-containment rule, without its size guard, would name this
    # icon after the whole transaction line.
    icon = control("d0", 0.20, 0.300, 0.011, 0.018)
    row = text("t0", 0.10, 0.299, 0.30, 0.020, "07/14/2026 HARBORVIEW PROPERTY MGMT")
    out = merge([icon, control("d1", 0.60, 0.60, 0.05, 0.03)], [row])
    assert {e.text for e in out} == {"07/14/2026 HARBORVIEW PROPERTY MGMT", None}


def test_merge_collapses_duplicate_control_boxes_keeping_the_confident_one() -> None:
    out = merge(
        [
            control("d0", 0.30, 0.50, 0.12, 0.04, conf=0.6),
            control("d1", 0.305, 0.502, 0.12, 0.04, conf=0.9),
        ],
        [],
    )
    assert len(out) == 1
    assert out[0].conf == pytest.approx(0.9)


def test_merge_keeps_unclaimed_text_and_numbers_everything_in_reading_order() -> None:
    out = merge(
        [control("d0", 0.05, 0.30, 0.10, 0.03)],
        [
            text("t0", 0.50, 0.10, 0.20, 0.02, "header"),
            text("t1", 0.06, 0.301, 0.05, 0.02, "Go"),
            text("t2", 0.10, 0.60, 0.20, 0.02, "footer"),
        ],
    )
    assert [e.id for e in out] == ["e0", "e1", "e2"]
    assert [e.text for e in out] == ["header", "Go", "footer"]


def test_infer_role_separates_a_wide_row_from_a_button() -> None:
    row = control("d0", 0.05, 0.30, 0.90, 0.03).model_copy(
        update={"text": "2026-01-04 ACME Corp $120.00"}
    )
    button = control("d1", 0.30, 0.50, 0.12, 0.04).model_copy(update={"text": "Transfer"})
    blank_field = control("d2", 0.30, 0.20, 0.30, 0.03)
    assert infer_role(row) == "row"
    assert infer_role(button) == "button"
    assert infer_role(blank_field) == "textbox"


# ---------------------------------------------------------------------------
# spatial index
# ---------------------------------------------------------------------------


def _table() -> ElementIndex:
    return ElementIndex(
        (
            text("e0", 0.10, 0.10, 0.30, 0.02, "Member Services"),
            text("e1", 0.10, 0.20, 0.10, 0.02, "Savings"),
            text("e2", 0.30, 0.20, 0.10, 0.02, "$4,820.19"),
            text("e3", 0.10, 0.30, 0.10, 0.02, "Checking"),
            text("e4", 0.30, 0.30, 0.10, 0.02, "$108.22"),
        )
    )


def test_index_within_selects_only_boxes_mostly_inside_the_region() -> None:
    inside = _table().within(Bbox(x=0.05, y=0.15, w=0.40, h=0.20))
    assert [e.text for e in inside] == ["Savings", "$4,820.19", "Checking", "$108.22"]


def test_index_right_of_finds_the_value_beside_a_label() -> None:
    index = _table()
    savings = index.elements[1]
    assert [e.text for e in index.right_of(savings)] == ["$4,820.19"]


def test_index_right_of_does_not_cross_into_a_touching_row() -> None:
    # Real OCR geometry from the accounts table: the padded line boxes of
    # neighbouring rows touch, and one pixel of overlap used to be enough to
    # return the row above's nickname and the row below's status as if they sat
    # beside "Savings". A read capability would have returned the wrong account.
    index = ElementIndex(
        (
            text("e0", 161 / 1440, 259 / 900, 116 / 1440, 26 / 900, "Everyday Checking"),
            text("e1", 72 / 1440, 285 / 900, 55 / 1440, 25 / 900, "Savings"),
            text("e2", 161 / 1440, 284 / 900, 100 / 1440, 27 / 900, "Primary Savings"),
            text("e3", 323 / 1440, 286 / 900, 72 / 1440, 21 / 900, "$18,204.55"),
            text("e4", 277 / 1440, 309 / 900, 47 / 1440, 26 / 900, "Active"),
        )
    )
    assert [e.text for e in index.right_of(index.elements[1])] == [
        "Primary Savings",
        "$18,204.55",
    ]


def test_index_below_is_a_scope_constructor() -> None:
    index = _table()
    header = index.elements[0]
    assert [e.text for e in index.below(header)] == [
        "Savings",
        "$4,820.19",
        "Checking",
        "$108.22",
    ]


def test_index_overlapping_detects_something_stacked_on_the_target() -> None:
    target = Bbox(x=0.30, y=0.20, w=0.10, h=0.02)
    assert [e.text for e in _table().overlapping(target, min_iou=0.05)] == ["$4,820.19"]


def test_index_rows_reconstructs_table_rows() -> None:
    rows = _table().rows(Bbox(x=0.05, y=0.15, w=0.40, h=0.20))
    assert [[e.text for e in r] for r in rows] == [
        ["Savings", "$4,820.19"],
        ["Checking", "$108.22"],
    ]


# ---------------------------------------------------------------------------
# capture + the settle loop
# ---------------------------------------------------------------------------


def _png(path: Path, colour: tuple[int, int, int]) -> Path:
    Image.new("RGB", (VIEWPORT.width, VIEWPORT.height), colour).save(path)
    return path


class FakeReader:
    """Emits one text line, wherever it is asked to read."""

    def read(
        self, image_path: Path, viewport: Viewport, region: Bbox | None = None
    ) -> list[Element]:
        return [text("t0", 0.10, 0.10, 0.20, 0.02, "Savings")]


class NoDetector:
    def detect(self, image_path: Path, viewport: Viewport) -> list[Element]:
        return []


class ChangingScreen:
    """Pixels never repeat. A blinking caret or a spinner, in the abstract."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.n = 0

    def capture(self, out_path: Path) -> tuple[Viewport, str]:
        self.n += 1
        _png(out_path, (self.n % 255, 0, 0))
        return VIEWPORT, f"hash-{self.n}"


class MovingTextReader:
    """The words themselves keep moving — a page still laying out."""

    def __init__(self) -> None:
        self.n = 0

    def read(
        self, image_path: Path, viewport: Viewport, region: Bbox | None = None
    ) -> list[Element]:
        self.n += 1
        return [text("t0", 0.10, 0.10 + self.n * 0.05, 0.20, 0.02, f"row {self.n}")]


def test_image_file_screen_moves_only_when_something_acts(tmp_path: Path) -> None:
    frames = [_png(tmp_path / "a.png", (10, 10, 10)), _png(tmp_path / "b.png", (20, 20, 20))]
    screen = ImageFileScreen(frames, VIEWPORT)
    out = tmp_path / "out.png"

    first = screen.capture(out)
    # Capture is not what changes a screen. If it were, `settle` would never see
    # two equal frames in a row and would never converge.
    assert screen.capture(out)[1] == first[1]

    screen.advance()
    second = screen.capture(out)
    assert second[1] != first[1]

    screen.advance()
    # Held at the last frame, so a replay against a recorded sequence terminates.
    assert screen.capture(out)[1] == second[1]
    assert first[0] == VIEWPORT


def test_observe_composes_capture_detect_read_and_merge(tmp_path: Path) -> None:
    screen = ImageFileScreen([_png(tmp_path / "a.png", (10, 10, 10))], VIEWPORT)
    obs = Perceiver(screen, NoDetector(), FakeReader()).observe(tmp_path / "frame.png")

    assert obs.viewport == VIEWPORT
    assert obs.frame_hash
    assert obs.taken_at
    assert [e.id for e in obs.elements] == ["e0"]
    assert obs.by_id("e0") is not None
    assert obs.by_id("e0").text == "Savings"  # type: ignore[union-attr]


def test_settle_returns_once_two_frames_agree(tmp_path: Path) -> None:
    frames = [_png(tmp_path / "a.png", (10, 10, 10))]
    perceiver = Perceiver(ImageFileScreen(frames, VIEWPORT), NoDetector(), FakeReader())
    obs = perceiver.settle(tmp_path / "frame.png", timeout_ms=2000, poll_ms=1)
    assert obs.elements
    # The cheap path fired; nothing had to be read twice.
    assert obs.settled_by is SettledBy.PIXELS


def test_a_page_whose_pixels_never_repeat_settles_on_what_it_says(tmp_path: Path) -> None:
    """A caret, a spinner or a clock must not make every step unsettleable.

    Pixels never converge here. The words do, and the words are the property the
    resolver actually depends on.
    """
    perceiver = Perceiver(ChangingScreen(tmp_path), NoDetector(), FakeReader())
    obs = perceiver.settle(tmp_path / "frame.png", timeout_ms=60, poll_ms=1)
    assert obs.settled_by is SettledBy.TEXT
    assert [e.text for e in obs.elements] == ["Savings"]


def test_settle_raises_rather_than_returning_a_mid_reflow_frame(tmp_path: Path) -> None:
    """Neither test converges: the page is genuinely still laying out."""
    perceiver = Perceiver(ChangingScreen(tmp_path), NoDetector(), MovingTextReader())
    with pytest.raises(Unsettled):
        perceiver.settle(tmp_path / "frame.png", timeout_ms=60, poll_ms=1)


class CountdownReader:
    """A screen with a session countdown on it and nothing else happening.

    The case that defeats both settle tests at once, and not an exotic one: an
    application that declares session expiry as a condition is very likely to
    render the countdown for it. The pixels differ because a digit changed; the
    text differs for the same reason.
    """

    def __init__(self) -> None:
        self.n = 0

    def read(
        self, image_path: Path, viewport: Viewport, region: Bbox | None = None
    ) -> list[Element]:
        self.n += 1
        return [
            text("t0", 0.10, 0.10, 0.20, 0.02, "Available Balance"),
            text("t1", 0.60, 0.02, 0.15, 0.02, f"Session expires in 14:{60 - self.n:02d}"),
        ]


def test_a_ticking_clock_does_not_make_a_ready_screen_unsettleable(tmp_path: Path) -> None:
    """Declared-volatile lines are excluded from the comparison and nothing else.

    Without this the screen above never settles by pixels *or* by text, so every
    step on the application burns two full timeouts and then fails on a page that
    was ready throughout.
    """
    perceiver = Perceiver(
        ChangingScreen(tmp_path),
        NoDetector(),
        CountdownReader(),
        volatile=(r"\b\d{1,2}:\d{2}\b",),
    )

    obs = perceiver.settle(tmp_path / "frame.png", timeout_ms=60, poll_ms=1)

    assert obs.settled_by is SettledBy.TEXT
    # Excluded from the settling decision, not from the observation: a countdown
    # is still read, still perceivable, and still available to a checkpoint.
    assert any("Session expires" in (e.text or "") for e in obs.elements)


def test_without_the_declaration_the_same_screen_never_settles(tmp_path: Path) -> None:
    """The other half of the pair. This is what the application is opting out of,
    and it is worth seeing that the mechanism is what makes the difference rather
    than something else about the fixture."""
    perceiver = Perceiver(ChangingScreen(tmp_path), NoDetector(), CountdownReader())
    with pytest.raises(Unsettled):
        perceiver.settle(tmp_path / "frame.png", timeout_ms=60, poll_ms=1)
