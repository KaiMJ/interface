#!/usr/bin/env python3
"""Pre-fetch every model weight into CUA_MODELS_DIR.

    docker compose run --rm desktop python3 /app/scripts/fetch_models.py

Weights are deliberately not baked into the image — they are ~300MB+ that would
have to be re-pulled on every layer invalidation, and they do not belong in git.
Instead they live in ./models, bind-mounted at /models, so they survive rebuilds
and a developer downloads them once.

The failure this prevents: each library has its own idea of where a cache goes,
and the defaults all point inside the container ( ~/.cache/huggingface,
~/.paddlex, ~/.config/Ultralytics ). Left alone, every `docker compose up
--build` silently re-downloads them, and the first run of a demo stalls for
minutes on what looks like a hang. Every one of those paths is redirected under
/models by the Dockerfile; this script proves it by actually pulling them.

Idempotent. Safe to re-run; already-present files are left alone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

MODELS = Path(os.environ.get("CUA_MODELS_DIR", "/models"))

# Redirect every library's cache under /models before importing anything that
# reads these at import time.
os.environ.setdefault("HF_HOME", str(MODELS / "hf"))
os.environ.setdefault("YOLO_CONFIG_DIR", str(MODELS / "ultralytics"))

failures: list[str] = []


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def du(path: Path) -> str:
    if not path.exists():
        return "0 B"
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return f"{total / 1e6:.0f} MB"


print(f"models dir: {MODELS}")
MODELS.mkdir(parents=True, exist_ok=True)
# ultralytics checks that its config dir is writable *before* creating it, and
# falls back to /tmp with a warning if the probe fails. On a fresh clone the
# bind mount is empty, so create it here.
(MODELS / "ultralytics" / "Ultralytics").mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
print("\nOmniParser icon_detect (control detection)")
try:
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        os.environ.get("CUA_OMNIPARSER_REPO", "microsoft/OmniParser-v2.0"),
        os.environ.get("CUA_OMNIPARSER_REPO_FILE", "icon_detect/model.pt"),
    )
    ok(f"{path}")
    # Load once here rather than on the first observation of the first run: this
    # is also where a corrupt or truncated download surfaces.
    from ultralytics import YOLO

    m = YOLO(path)
    ok(f"loads as ultralytics {m.task}, classes={m.names}")
except Exception as e:  # noqa: BLE001
    bad(f"OmniParser: {e!r}")

# ---------------------------------------------------------------------------
print("\nPP-OCR detection + recognition (ONNX)")
try:
    from rapidocr import RapidOCR

    # RapidOCR ships its ONNX models inside the wheel, so there is nothing to
    # cache — but construct it here anyway: this is where a broken onnxruntime
    # install surfaces, rather than on the first observation of the first run.
    RapidOCR()
    ok("rapidocr constructed (models ship in the wheel; no download)")
except Exception as e:  # noqa: BLE001
    bad(f"PP-OCR: {e!r}")

# ---------------------------------------------------------------------------
print("\nPP-OCR on the GPU (torch weights)")
try:
    import torch

    if not torch.cuda.is_available():
        ok("no GPU visible — skipped; CUA_OCR_ENGINE=onnxruntime is the right setting here")
    else:
        from rapidocr import RapidOCR
        from rapidocr.utils.typings import EngineType

        # The same PP-OCR models in torch format, which is the only route to this
        # machine's GPU: onnxruntime-gpu ships CUDA 12 wheels and the torch here is
        # CUDA 13, so its CUDA provider loads and then registers no device.
        #
        # Measured on a dense back-office screen, 1440x900: 2707ms on the CPU
        # against 807ms here, for identical output. Perception is ~95% of a
        # replay's wall clock, so that is the largest single lever in the system.
        #
        # Fetched here rather than on first use so a run never pays for a download
        # mid-step. ~31MB into RapidOCR's own cache, which the image makes
        # writable for exactly this.
        RapidOCR(
            params={
                "Det.engine_type": EngineType.TORCH,
                "Rec.engine_type": EngineType.TORCH,
                "EngineConfig.torch.use_cuda": True,
                "EngineConfig.torch.gpu_id": 0,
            }
        )
        ok("torch OCR weights cached — set CUA_OCR_ENGINE=torch to use them")
except Exception as e:  # noqa: BLE001
    bad(f"PP-OCR (torch): {e!r}")

# ---------------------------------------------------------------------------
print(f"\ntotal in {MODELS}: {du(MODELS)}")
if failures:
    print(f"\n{len(failures)} failure(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all weights present")
