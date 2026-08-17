"""Text reading.

PP-OCR detection + recognition, executed by ONNX Runtime via RapidOCR. Gives text
lines with boxes and per-line confidence. Used in three places, and it is worth
being explicit that the third is the weak one:

  1. Joining text to detected controls, so a box becomes "the Transfer button".
  2. Evaluating checkpoints — asserting the screen says what we expected.
  3. Scanning lists in `find_and_act`.

(3) is the least deterministic part of an otherwise model-free replay path.
Truncation ("ACME Corporat..."), currency formatting and column bleed are all
real, which is why `Normalizer` exists and why the normalizer list is recorded in
the artifact rather than being a property of the engine. Stated plainly in
REPORT §3 as the known weakest link.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..calibration import CALIBRATION
from ..schema import Bbox, Element, ElementSource, Viewport


class OnnxTextReader:
    """PP-OCR via ONNX Runtime. Implements `perception.base.TextReader`.

    The engine choice is deliberate and was made against measurements, not taste;
    the reasoning is recorded on the dependency in pyproject.toml. Behind the
    `TextReader` protocol, so swapping engines again touches this file only.
    """

    def __init__(
        self,
        models_dir: Path,
        lang: str = "en",
        conf_threshold: float = 0.5,
        det_side_len: int = 1600,
    ) -> None:
        self.models_dir = models_dir
        self.lang = lang
        self.det_side_len = det_side_len
        # Below this a line is dropped rather than kept as text. Anchors and
        # checkpoints both compare against this output, and a confidently wrong
        # string is worse than a missing one: a missing anchor fails the step
        # loudly, a wrong one resolves to the wrong control.
        self.conf_threshold = conf_threshold
        self._ocr: Any | None = None

    def _load(self) -> Any:
        if self._ocr is None:
            from rapidocr import RapidOCR

            # Models ship inside the wheel; nothing is downloaded at run time.
            self._ocr = RapidOCR(params={"Det.limit_side_len": self.det_side_len})
        return self._ocr

    def read(
        self,
        image_path: Path,
        viewport: Viewport,
        region: Bbox | None = None,
    ) -> list[Element]:
        """Return one element per detected text line.

        When `region` is given, crop before reading rather than reading the frame
        and filtering. Cropping is both faster and more accurate — PP-OCR's
        detection stage behaves better on a tight region than on a full page — and
        it is what makes a 10-screen scan affordable.

        Emitted elements carry `source=OCR`, `role="text"`, and the raw string in
        both `text` and `name`.
        """
        w, h = float(viewport.width), float(viewport.height)
        img = Image.open(image_path).convert("RGB")

        ox, oy = 0.0, 0.0
        if region is not None:
            left, top = region.x * w, region.y * h
            right, bottom = left + region.w * w, top + region.h * h
            # A degenerate crop makes PP-OCR's detector raise rather than return
            # nothing, so treat "no region to read" as "no text".
            if right - left < 2 or bottom - top < 2:
                return []
            img = img.crop((int(left), int(top), int(right), int(bottom)))
            ox, oy = float(int(left)), float(int(top))

        result = self._load()(np.array(img))
        if result is None or result.boxes is None or result.txts is None:
            return []

        out: list[Element] = []
        for i, (poly, txt, score) in enumerate(
            zip(result.boxes, result.txts, result.scores, strict=True)
        ):
            conf = float(score)
            text = str(txt).strip()
            if not text or conf < self.conf_threshold:
                continue
            xs = [float(p[0]) + ox for p in poly]
            ys = [float(p[1]) + oy for p in poly]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            out.append(
                Element(
                    id=f"t{i}",
                    role="text",
                    name=text,
                    text=text,
                    bbox=Bbox(
                        x=max(0.0, min(1.0, x0 / w)),
                        y=max(0.0, min(1.0, y0 / h)),
                        w=max(0.0, min(1.0, (x1 - x0) / w)),
                        h=max(0.0, min(1.0, (y1 - y0) / h)),
                    ),
                    source=ElementSource.OCR,
                    conf=max(0.0, min(1.0, conf)),
                )
            )
        return out


def group_rows(
    elements: list[Element], y_tolerance: float = CALIBRATION.row_tolerance
) -> list[list[Element]]:
    """Cluster text elements into visual rows by vertical overlap.

    Row grouping is what makes `row_contains_all` predicates possible without a
    DOM. A table row is not a thing that exists in pixels; it is a set of text
    boxes that share a horizontal band. Tolerance is in normalized units and
    deliberately small — merging two adjacent table rows would let a predicate
    match terms that a human would never read as one record.
    """
    rows: list[list[Element]] = []
    # Sorted by vertical centre, so a new element can only ever belong to the row
    # being built. Comparing against every open row instead would let a tall
    # element chain two table rows into one, which is exactly the failure this
    # tolerance is small to avoid.
    for el in sorted(elements, key=lambda e: (e.bbox.center.y, e.bbox.x)):
        centre = el.bbox.center.y
        if rows:
            band = rows[-1]
            top = min(e.bbox.y for e in band)
            bottom = max(e.bbox.y + e.bbox.h for e in band)
            if top - y_tolerance <= centre <= bottom + y_tolerance:
                band.append(el)
                continue
        rows.append([el])
    return [sorted(row, key=lambda e: e.bbox.x) for row in rows]
