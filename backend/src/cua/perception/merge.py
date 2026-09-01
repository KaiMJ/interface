"""Fusing detector boxes with OCR text into one element list.

Detection knows where the controls are, OCR knows what the words are, and neither alone
is enough: a bare box cannot be targeted semantically, and a bare text line is not
necessarily clickable.
"""

from __future__ import annotations

from ..calibration import CALIBRATION
from ..schema import Bbox, Element, ElementSource
from .ocr import group_rows


def _area(b: Bbox) -> float:
    return b.w * b.h


def _labels(text: Bbox, control: Bbox, containment: float) -> bool:
    """Does this text line describe this control?

    Two cases, and the second is the common one: a pixel detector boxes the glyphs while OCR
    boxes the line including padding — 29x16 against 41x23 on the same button — so requiring
    text inside the control leaves it unlabelled. The reverse test needs a size guard, or an
    icon inside a table row's text line claims the whole row as its name.
    """
    if text.contained_by(control) >= containment:
        return True
    if control.contained_by(text) >= containment:
        a, b = _area(text), _area(control)
        return min(a, b) / max(a, b) >= CALIBRATION.label_size_ratio if max(a, b) > 0 else False
    return False


def _dedupe(controls: list[Element], iou_threshold: float) -> list[Element]:
    """Greedy non-maximum suppression, highest confidence first. OmniParser routinely emits a
    button and its label as two near-identical boxes, and keeping both gives the model two
    marks that mean one thing."""
    kept: list[Element] = []
    for el in sorted(controls, key=lambda e: (-e.conf, _area(e.bbox))):
        if all(el.bbox.iou(k.bbox) < iou_threshold for k in kept):
            kept.append(el)
    return kept


def merge(
    controls: list[Element],
    texts: list[Element],
    iou_threshold: float = 0.60,
    containment_threshold: float = CALIBRATION.label_containment,
) -> list[Element]:
    """Return one deduplicated, text-annotated element list.

    Ids are reassigned (`e0`, `e1`, ...) in reading order, top-to-bottom then left-to-right, so
    a set-of-marks overlay numbers things the way a person would scan them.
    """
    kept = _dedupe(controls, iou_threshold)

    # Each text goes to the *smallest* control containing it: a button inside a card is
    # contained by both, and the button is what was clicked.
    owned: dict[int, list[Element]] = {}
    orphans: list[Element] = []
    for text in texts:
        best: int | None = None
        for ci, control in enumerate(kept):
            if _area(control.bbox) > CALIBRATION.container_frame_area:
                continue
            if not _labels(text.bbox, control.bbox, containment_threshold):
                continue
            if best is None or _area(control.bbox) < _area(kept[best].bbox):
                best = ci
        if best is None:
            orphans.append(text)
        else:
            owned.setdefault(best, []).append(text)

    merged: list[Element] = []
    for ci, control in enumerate(kept):
        lines = sorted(owned.get(ci, []), key=lambda e: (e.bbox.center.y, e.bbox.x))
        joined = " ".join(line.text or "" for line in lines).strip()
        el = control.model_copy(
            update={"text": joined or None, "name": joined or control.name}
        )
        # Re-infer now the box has words: geometry alone cannot separate "a small wide box"
        # from "the Transfer button".
        merged.append(el.model_copy(update={"role": infer_role(el)}))

    # Text with no enclosing control is not clickable, but it is what checkpoints and anchors
    # match against, so it survives as its own element.
    merged.extend(orphans)

    ordered = [el for row in group_rows(merged) for el in row]
    return [el.model_copy(update={"id": f"e{i}"}) for i, el in enumerate(ordered)]


def infer_role(el: Element) -> str:
    """Guess a coarse role from geometry and text when the detector gives none.

    Openly heuristic, and wrong sometimes. The resolver treats `role` as a hint that narrows
    candidates and never as the sole matching key, so a bad guess costs precision, not
    correctness.

    Thresholds are normalized and application chrome is not — a button stays ~32px tall as the
    window grows — so these boundaries are sound only near the viewport they were measured at
    (REPORT §7).
    """
    if el.source is ElementSource.OCR:
        return "text"

    w, h = el.bbox.w, el.bbox.h
    ratio = w / h if h > 0 else 0.0
    text = (el.text or "").strip()

    if not text:
        # Input first, and icons kept near-square: a 96x26px PIN box is small enough for the
        # icon bound and too squat for the input ratio, and calling it an icon loses a field a
        # step types into. Between the two, `control`.
        if ratio >= 4.0 and h <= 0.06:
            return "textbox"
        if w <= 0.05 and h <= 0.06 and ratio <= 2.5:
            return "icon"
        return "control"
    if w >= 0.45 and ratio >= 10.0:
        return "row"
    if h <= 0.08 and len(text) <= 40:
        return "button"
    return "control"
