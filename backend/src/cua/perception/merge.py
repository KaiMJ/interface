"""Fusing detector boxes with OCR text into one element list.

Detection knows where the controls are. OCR knows what the words are. Neither
alone is enough: a bare box cannot be targeted semantically, and a bare text line
is not necessarily clickable.

Merge rules, in order:

  1. A text element substantially inside a control box becomes that control's
     `name`/`text`; the text element is absorbed.
  2. Control boxes overlapping each other above `iou_threshold` collapse to the
     highest-confidence one. OmniParser routinely emits a button and its label as
     two nearly-identical boxes.
  3. Text with no enclosing control survives as its own `role="text"` element.
     It cannot be clicked meaningfully but it is exactly what checkpoints and
     anchors match against.
"""

from __future__ import annotations

from ..schema import Element


def merge(
    controls: list[Element],
    texts: list[Element],
    iou_threshold: float = 0.60,
    containment_threshold: float = 0.70,
) -> list[Element]:
    """Return one deduplicated, text-annotated element list.

    Ids are reassigned here (`e0`, `e1`, ...) in reading order — top-to-bottom then
    left-to-right — so that a set-of-marks overlay numbers things the way a person
    would scan them, which measurably helps the model refer to them correctly.
    """
    raise NotImplementedError


def infer_role(el: Element) -> str:
    """Guess a coarse role from geometry and text when the detector gives none.

    Heuristic and openly so: short text in a small wide box near other such boxes
    is a button; a wide short box with no text is an input. Wrong sometimes. The
    resolver treats `role` as a hint that narrows candidates, never as the sole
    matching key, so a bad guess costs a little precision and never correctness.
    """
    raise NotImplementedError
