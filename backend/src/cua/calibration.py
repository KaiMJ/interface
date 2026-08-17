"""Perceptual calibration — every tuned threshold, with what set it.

These are the numbers that decide what perception concludes: how much of a text
box must sit inside a control before it counts as that control's label, how much
vertical overlap makes two boxes the same table row, how large a detection has to
be before it reads as a container rather than a control.

They are empirical. Collecting them here does not make them principled; it makes
them **visible as a set**, which is the property that matters. A threshold living
beside its call site is a threshold nobody re-examines, and two call sites
answering the same question with different values — which is what this module was
extracted from — is how a threshold set turns into folklore.

Every field carries the measurement that set it. Where a value has no measurement
behind it, that is stated too.

Three rules keep this from becoming a junk drawer:

  1. Only *perceptual* values live here — things that could be re-measured against
     a different surface. Display truncation lengths, prompt budgets and
     structural constants stay where they are used.
  2. Anything a deployment might legitimately change lives in `Settings` and is
     passed in: the OCR detector's input size, confidence floors, the merge IoU.
     Calibration is what we measured. Settings are what an operator may override.
  3. One value per question. There is exactly one containment threshold for "is
     this element inside this region", not one per caller.

Re-measuring: `scripts/smoke_observe.py` and `scripts/smoke_drive.py` are the
harnesses these were set against. Change a value, run both, and read the element
counts and the resolution tiers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Calibration:
    """Thresholds over normalized (0..1) coordinates, unless stated otherwise."""

    # --- merge: joining OCR text to detected controls ------------------------

    label_containment: float = 0.70
    """How much of a text line must sit inside a control for it to be its label.

    Loose enough for OCR padding, tight enough that a line merely crossing a
    control's edge is not absorbed."""

    label_size_ratio: float = 0.25
    """Minimum area ratio when the *control* is inside the *text*, not the reverse.

    Measured on the target app's "View" buttons: the detector boxes the glyphs
    (29x16 px) where OCR boxes the padded line (41x23 px), so only 49% of the text
    sits inside the control and a containment-only rule leaves every button on the
    page anonymous. The guard stops an icon inside a table row's text line from
    claiming the whole row as its name: 464/943 px² passes, 256/6000 px² does
    not."""

    container_frame_area: float = 0.15
    """Above this share of the frame, a detection is a container, not a control.

    Measured on the sign-on screen, where a 21%-of-frame detection swallowed both
    field labels and left "User ID" unaddressable — which is the one thing that has
    to work to fill in a form without a DOM. The sign-on panel itself (4%) still
    takes its own heading."""

    # --- spatial queries -----------------------------------------------------

    region_containment: float = 0.60
    """How much of an element must sit inside a region to count as within it.

    One value for one question. This was previously 0.6 in `ElementIndex.within`
    and 0.5 in checkpoint scope reading, which is the duplication that motivated
    this module."""

    band_overlap: float = 0.50
    """Fraction of the shorter box's height two boxes must share to be one row.

    Measured on the accounts grid: OCR line boxes carry padding and adjacent rows
    touch, so "overlaps at all" put the row above and the row below in the same
    band — which let "the balance to the right of Savings" return the checking
    account's balance. Wrong row, right shape, no error."""

    row_tolerance: float = 0.008
    """Vertical slack when clustering text into rows, ~7px on a 900px display.

    Deliberately small: merging two adjacent table rows would let a predicate match
    terms a human reads as two separate records — in a banking app, the wrong
    transaction."""

    # --- verification --------------------------------------------------------

    overlay_min_frame_area: float = 0.08
    overlay_min_size_ratio: float = 4.0
    """When a target could not be confirmed from screen text, a control that covers
    it, occupies at least this share of the frame, and dwarfs it by this ratio is
    read as something stacked on top.

    Vision has no z-order, so this only runs when the recording's own words are
    *not* readable at the coordinates — see `resolve.verify.verify_target`. Not
    independently measured; the pair was chosen so a dialog qualifies and the
    sign-on panel enclosing its own button does not."""

    enclosure: float = 0.90
    """How much of a region must sit inside an element for that element to count as
    enclosing it — used to report what covers a target when nothing is inside it."""


CALIBRATION = Calibration()
