"""Control detection.

OmniParser v2's `icon_detect` checkpoint is a YOLO model trained to find
interactable regions in UI screenshots — buttons, inputs, icons, checkboxes,
rows. It finds *where things are*; it does not tell us what they say. Text comes
from OCR (`ocr.py`) and the two are merged in `merge.py`.

We deliberately do not run OmniParser's Florence-2 captioning stage. It exists to
describe icons in natural language, which is expensive on CPU and largely
redundant here: OCR already supplies the visible text, and for the small number of
text-free icon buttons a role guess from geometry is sufficient. Noted as a cut.

Weights are pulled from HuggingFace on first use into CUA_MODELS_DIR (a mounted
volume), not baked into the image.
"""

from __future__ import annotations

from pathlib import Path

from ..schema import Element, ElementSource, Viewport


class OmniParserDetector:
    """Implements `perception.base.Detector`."""

    def __init__(
        self,
        models_dir: Path,
        repo: str,
        weights: str,
        conf_threshold: float = 0.30,
    ) -> None:
        self.models_dir = models_dir
        self.repo = repo
        self.weights = weights
        self.conf_threshold = conf_threshold
        self._model: object | None = None

    def ensure_weights(self) -> Path:
        """Download the checkpoint if absent. Idempotent; safe to call per run."""
        raise NotImplementedError

    def _load(self) -> object:
        """Lazy-load the YOLO model.

        Lazy because import-time torch costs seconds and the replay path with an
        `ocr_only` detector must not pay it.
        """
        raise NotImplementedError

    def detect(self, image_path: Path, viewport: Viewport) -> list[Element]:
        """Run detection, emit normalized elements.

        Returns boxes with `source=OMNIPARSER`, a coarse `role` inferred from
        aspect ratio and size, and no text — text is joined in later.
        """
        raise NotImplementedError


class NullDetector:
    """Detects nothing; OCR text lines become the only candidates.

    Not a toy. It is the configuration used by tests and by the no-API-key demo
    path, and it is an honest floor for what the system can do on a surface where
    control detection fails entirely.
    """

    def detect(self, image_path: Path, viewport: Viewport) -> list[Element]:
        return []


def build_detector(name: str, models_dir: Path, repo: str, weights: str, conf: float) -> object:
    if name == "omniparser":
        return OmniParserDetector(models_dir, repo, weights, conf)
    if name == "ocr_only":
        return NullDetector()
    raise ValueError(f"unknown detector: {name!r}")


__all__ = ["ElementSource", "NullDetector", "OmniParserDetector", "build_detector"]
