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
from typing import Any

from ..schema import Bbox, Element, ElementSource, Viewport
from .base import Detector
from .merge import infer_role


def _unit(v: float) -> float:
    """Clamp into 0..1.

    Detector boxes routinely run a pixel or two past the frame edge, and `Bbox`
    validates its fields as normalized units. Clamping here keeps a legitimate
    edge-of-screen control from raising instead of being detected.
    """
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


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
        self._model: Any | None = None

    def ensure_weights(self) -> Path:
        """Download the checkpoint if absent. Idempotent; safe to call per run."""
        from huggingface_hub import hf_hub_download

        # HF_HOME points inside models_dir (bind-mounted), so this is a no-op
        # after the first run and never touches the image.
        return Path(hf_hub_download(self.repo, self.weights))

    def _load(self) -> Any:
        """Lazy-load the YOLO model.

        Lazy because import-time torch costs seconds and the replay path with an
        `ocr_only` detector must not pay it.
        """
        if self._model is None:
            from ultralytics import YOLO  # type: ignore[attr-defined]

            self._model = YOLO(str(self.ensure_weights()))
        return self._model

    def detect(self, image_path: Path, viewport: Viewport) -> list[Element]:
        """Run detection, emit normalized elements.

        Returns boxes with `source=OMNIPARSER`, a coarse `role` inferred from
        aspect ratio and size, and no text — text is joined in later.
        """
        result = self._load().predict(
            str(image_path), conf=self.conf_threshold, verbose=False
        )[0]
        boxes = result.boxes
        if boxes is None:
            return []

        w, h = float(viewport.width), float(viewport.height)
        out: list[Element] = []
        for i, (xyxy, conf) in enumerate(
            zip(boxes.xyxy.tolist(), boxes.conf.tolist(), strict=True)
        ):
            x0, y0, x1, y1 = (v for v in xyxy)
            bbox = Bbox(
                x=_unit(x0 / w),
                y=_unit(y0 / h),
                w=_unit((x1 - x0) / w),
                h=_unit((y1 - y0) / h),
            )
            if bbox.w <= 0.0 or bbox.h <= 0.0:
                continue
            el = Element(
                id=f"d{i}",
                bbox=bbox,
                source=ElementSource.OMNIPARSER,
                conf=_unit(float(conf)),
            )
            # Geometry-only guess; `merge` re-infers once OCR text has been joined
            # in, at which point "a small wide box saying Transfer" is knowable.
            out.append(el.model_copy(update={"role": infer_role(el)}))
        return out


class NullDetector:
    """Detects nothing; OCR text lines become the only candidates.

    Not a toy. It is the configuration used by tests and by the no-API-key demo
    path, and it is an honest floor for what the system can do on a surface where
    control detection fails entirely.
    """

    def detect(self, image_path: Path, viewport: Viewport) -> list[Element]:
        return []


def build_detector(name: str, models_dir: Path, repo: str, weights: str, conf: float) -> Detector:
    if name == "omniparser":
        return OmniParserDetector(models_dir, repo, weights, conf)
    if name == "ocr_only":
        return NullDetector()
    raise ValueError(f"unknown detector: {name!r}")


__all__ = ["ElementSource", "NullDetector", "OmniParserDetector", "build_detector"]
