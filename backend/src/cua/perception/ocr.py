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

from ..schema import Bbox, Element, Viewport


class OnnxTextReader:
    """PP-OCR via ONNX Runtime. Implements `perception.base.TextReader`.

    The engine choice is deliberate and was made against measurements, not taste;
    the reasoning is recorded on the dependency in pyproject.toml. Behind the
    `TextReader` protocol, so swapping engines again touches this file only.
    """

    def __init__(self, models_dir: Path, lang: str = "en") -> None:
        self.models_dir = models_dir
        self.lang = lang
        self._ocr: object | None = None

    def _load(self) -> object:
        raise NotImplementedError

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
        raise NotImplementedError


def group_rows(elements: list[Element], y_tolerance: float = 0.008) -> list[list[Element]]:
    """Cluster text elements into visual rows by vertical overlap.

    Row grouping is what makes `row_contains_all` predicates possible without a
    DOM. A table row is not a thing that exists in pixels; it is a set of text
    boxes that share a horizontal band. Tolerance is in normalized units and
    deliberately small — merging two adjacent table rows would let a predicate
    match terms that a human would never read as one record.
    """
    raise NotImplementedError
