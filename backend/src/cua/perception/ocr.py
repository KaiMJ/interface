"""Text reading.

PP-OCR detection and recognition: text lines with boxes and per-line confidence. Three uses —
naming detected controls, evaluating checkpoints, and scanning lists in `find_and_act`. The
last is the weak one, because a predicate is only as good as the characters that came back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from ..calibration import CALIBRATION
from ..schema import Bbox, Element, ElementSource, Viewport


class RapidOcrTextReader:
    """PP-OCR via RapidOCR. Implements `perception.base.TextReader`.

    `engine` selects the backend at construction: `onnxruntime` on the CPU, or `torch` on the
    GPU the detector is already using. Behind the `TextReader` protocol, so swapping either
    touches this file only.
    """

    def __init__(
        self,
        conf_threshold: float = 0.5,
        det_side_len: int = 1600,
        engine: str = "onnxruntime",
    ) -> None:
        self.det_side_len = det_side_len
        # "onnxruntime" (CPU) or "torch" (the GPU the detector already uses).
        self.engine = engine
        # Below this a line is dropped. A confidently wrong string is worse than a missing
        # one: a missing anchor fails the step loudly, a wrong one resolves to the wrong
        # control.
        self.conf_threshold = conf_threshold
        self._ocr: Any | None = None

    def _load(self) -> Any:
        """Construct the recogniser once, on the configured backend.

        The torch backend is the GPU path and its weights are not in the wheel, so the first
        construction downloads them into RapidOCR's own cache — which is why the image makes
        that directory writable for a non-root user. A backend that cannot be constructed falls
        back to CPU rather than taking the run down.
        """
        if self._ocr is not None:
            return self._ocr

        from rapidocr import RapidOCR

        params: dict[str, Any] = {"Det.limit_side_len": self.det_side_len}
        if self.engine == "torch":
            from rapidocr.utils.typings import EngineType

            params.update(
                {
                    "Det.engine_type": EngineType.TORCH,
                    "Rec.engine_type": EngineType.TORCH,
                    "EngineConfig.torch.use_cuda": True,
                    "EngineConfig.torch.gpu_id": 0,
                }
            )
            try:
                self._ocr = RapidOCR(params=params)
                return self._ocr
            except Exception as e:  # noqa: BLE001 - see the docstring
                import logging

                logging.getLogger(__name__).warning(
                    "OCR backend 'torch' unavailable (%s: %s); using onnxruntime on the CPU",
                    type(e).__name__,
                    e,
                )
                params = {"Det.limit_side_len": self.det_side_len}

        self._ocr = RapidOCR(params=params)
        return self._ocr

    def read(
        self,
        image_path: Path,
        viewport: Viewport,
        region: Bbox | None = None,
    ) -> list[Element]:
        """Return one element per detected text line, carrying `source=OCR`, `role="text"` and
        the raw string in both `text` and `name`.

        A `region` crops before reading rather than reading the frame and filtering: cropping
        is faster and more accurate, since PP-OCR's detection stage behaves better on a tight
        region than a full page.
        """
        w, h = float(viewport.width), float(viewport.height)
        img = Image.open(image_path).convert("RGB")

        ox, oy = 0.0, 0.0
        if region is not None:
            left, top = region.x * w, region.y * h
            right, bottom = left + region.w * w, top + region.h * h
            # A degenerate crop makes PP-OCR's detector raise rather than return nothing, so
            # treat "no region to read" as "no text".
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

    What makes `row_contains_all` predicates possible without a DOM: a table row is a set of
    text boxes sharing a horizontal band. The tolerance is deliberately small, since merging
    two adjacent rows would let a predicate match terms no human would read as one record.
    """
    rows: list[list[Element]] = []
    # Sorted by vertical centre, so a new element can only belong to the row being built; a
    # tall element must not chain two rows into one.
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
