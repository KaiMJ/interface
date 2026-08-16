#!/usr/bin/env python3
"""Does the perception stack actually load and produce anything?

Run inside the desktop container:

    docker compose exec desktop python3 scripts/smoke_perception.py

This deliberately does NOT import `cua.perception` — those modules are stubs. It
exercises the two external dependencies directly, because they are the ones that
can fail in ways our code cannot fix: wrong weights format, a CUDA/driver
mismatch, an incompatible paddle pairing, a model that silently downloads to a
path that is not persisted.

Checks, in order of how expensive they are to discover later:

  1. torch imports, and reports whether the GPU is actually visible in here
  2. the OmniParser icon_detect checkpoint loads as a YOLO model
  3. the OCR engine constructs (where a broken onnxruntime install surfaces)
  4. both run against a real screenshot of the target app and return boxes

Exit non-zero on any failure, so it can be wired into CI later.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MODELS = Path(os.environ.get("CUA_MODELS_DIR", "/models"))
TARGET = os.environ.get("CUA_TARGET_BASE_URL", "http://targetapp:8080")
OUT = Path("/tmp/smoke")

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


# ---------------------------------------------------------------------------
step("torch / GPU")
try:
    import torch

    ok(f"torch {torch.__version__}")
    if torch.cuda.is_available():
        ok(f"cuda visible: {torch.cuda.get_device_name(0)}")
    else:
        # Not fatal. The detector falls back to CPU; it is just slower per
        # observation. But if the compose GPU reservation is present and this
        # still says no, the container toolkit is not wired up and it is worth
        # knowing now rather than wondering why every run is sluggish.
        print("  warn  no CUDA device visible — detector will run on CPU")
except Exception as e:  # noqa: BLE001
    bad(f"torch import failed: {e!r}")

# ---------------------------------------------------------------------------
step("OmniParser icon_detect weights")
# Fetched from HuggingFace into HF_HOME (=/models/hf, bind-mounted) on first use.
# NOT the local weights/icon_detect_v3/model.pt: that file is a TorchScript
# archive of the same network, which ultralytics refuses to load and which would
# require hand-written decode + NMS. The upstream release is a real ultralytics
# checkpoint, so the whole postprocessing stage comes for free.
detector = None
try:
    from huggingface_hub import hf_hub_download
    from ultralytics import YOLO

    t0 = time.time()
    path = hf_hub_download(
        os.environ.get("CUA_OMNIPARSER_REPO", "microsoft/OmniParser-v2.0"),
        os.environ.get("CUA_OMNIPARSER_REPO_FILE", "icon_detect/model.pt"),
    )
    ok(f"weights at {path}")
    detector = YOLO(path)
    ok(f"loaded in {time.time() - t0:.1f}s | task={detector.task} names={detector.names}")
except Exception as e:  # noqa: BLE001
    bad(f"OmniParser load failed: {e!r}")

# ---------------------------------------------------------------------------
step("PP-OCR via ONNX Runtime")
reader = None
try:
    from rapidocr import RapidOCR

    t0 = time.time()
    reader = RapidOCR()
    ok(f"constructed in {time.time() - t0:.1f}s")
except Exception as e:  # noqa: BLE001
    bad(f"RapidOCR construction failed: {e!r}")

# ---------------------------------------------------------------------------
step("screenshot of the target app")
shot = OUT / "member.png"
OUT.mkdir(parents=True, exist_ok=True)
try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.context.add_cookies(
            [{"name": "teller_sid", "value": "teller01", "url": TARGET}]
        )
        page.goto(f"{TARGET}/members/12345", wait_until="networkidle")
        page.screenshot(path=str(shot))
        browser.close()
    ok(f"captured {shot} ({shot.stat().st_size / 1e3:.0f} KB)")
except Exception as e:  # noqa: BLE001
    bad(f"screenshot failed: {e!r}")

# ---------------------------------------------------------------------------
step("detection + OCR on that screenshot")
if shot.exists() and detector is not None:
    try:
        t0 = time.time()
        res = detector.predict(str(shot), conf=0.30, verbose=False)
        boxes = res[0].boxes
        n = 0 if boxes is None else len(boxes)
        ok(f"OmniParser: {n} candidate controls in {time.time() - t0:.1f}s")
        if n == 0:
            bad("detector returned zero boxes on a dense form page — suspicious")
    except Exception as e:  # noqa: BLE001
        bad(f"detection failed: {e!r}")

if shot.exists() and reader is not None:
    try:
        t0 = time.time()
        out = reader(str(shot))
        texts = list(out.txts) if out is not None and out.txts is not None else []
        ok(f"PP-OCR: {len(texts)} text lines in {time.time() - t0:.1f}s")
        print("  sample:", ", ".join(repr(t) for t in texts[:8]))
        # The page renders these; if OCR cannot see them, anchor-text resolution
        # and every checkpoint built on it are unreliable.
        joined = " ".join(texts).lower()
        for expect in ("dolores", "12345", "savings", "balance"):
            if expect in joined:
                ok(f"read expected token {expect!r}")
            else:
                bad(f"expected token {expect!r} not found in OCR output")
    except Exception as e:  # noqa: BLE001
        bad(f"OCR failed: {e!r}")

# ---------------------------------------------------------------------------
print("\n" + "=" * 64)
if failures:
    print(f"{len(failures)} failure(s):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("perception stack OK")
