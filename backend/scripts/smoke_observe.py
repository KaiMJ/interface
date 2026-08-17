#!/usr/bin/env python3
"""Does `cua.perception` actually see the target application?

Run inside the desktop container:

    docker compose exec desktop python3 scripts/smoke_observe.py
    docker compose exec desktop python3 scripts/smoke_observe.py --display
    docker compose exec desktop python3 scripts/smoke_observe.py --image /tmp/x.png

`scripts/smoke_perception.py` answers a different question — whether the two
external dependencies load and run at all. This one exercises the code we wrote:
capture -> detect -> read -> merge -> set-of-marks, through `Perceiver`, with the
same settings the real runs use.

What it checks is what everything above perception depends on and cannot verify
for itself:

  1. an observation comes back with elements, ids in reading order
  2. the anchors a capability would target are actually readable
  3. controls carry their labels — an anonymous box is a box the model cannot ask
     for by name, and the merge rule that joins them is the fragile part
  4. spatial queries answer the questions the resolver will ask ("what is to the
     right of 'Savings'")

Writes the frame and its overlay to /tmp/smoke/ so a human can look at what the
model would have been shown. Exit non-zero on failure, for CI later.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from cua.config import settings
from cua.perception import ElementIndex, Perceiver
from cua.perception.detect import build_detector
from cua.perception.ocr import OnnxTextReader
from cua.perception.screen import ImageFileScreen, XDisplayScreen
from cua.perception.som import annotate, candidate_digest, truncated
from cua.schema import ElementSource

OUT = Path("/tmp/smoke")
ANCHORS = ("dolores", "12345", "savings", "available balance", "view")

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


def capture_target(path: Path, url: str) -> None:
    """Screenshot the target app headlessly, as a stand-in for the live display.

    The X display only shows something once the action layer launches a browser
    onto it; until then this keeps the perception check runnable.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": settings().display_width, "height": settings().display_height}
        )
        page.context.add_cookies([{"name": "teller_sid", "value": "teller01", "url": url}])
        page.goto(f"{url}/members/12345", wait_until="networkidle")
        page.screenshot(path=str(path))
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", action="store_true", help="observe the live X display")
    ap.add_argument("--image", type=Path, help="observe a PNG instead of capturing one")
    args = ap.parse_args()

    cfg = settings()
    OUT.mkdir(parents=True, exist_ok=True)

    step("perceiver")
    if args.display:
        screen: XDisplayScreen | ImageFileScreen = XDisplayScreen(cfg.display, cfg.viewport)
        ok(f"capturing X display {cfg.display}")
    else:
        frame = args.image or (OUT / "member.png")
        if args.image is None:
            capture_target(frame, cfg.target_base_url)
            ok(f"captured {cfg.target_base_url}/members/12345 -> {frame}")
        screen = ImageFileScreen([frame], cfg.viewport)
    perceiver = Perceiver(
        screen=screen,
        detector=build_detector(
            cfg.detector,
            cfg.models_dir,
            cfg.omniparser_repo,
            cfg.omniparser_repo_file,
            cfg.detect_conf_threshold,
        ),
        reader=OnnxTextReader(
            cfg.models_dir,
            conf_threshold=cfg.ocr_conf_threshold,
            det_side_len=cfg.ocr_det_side_len,
        ),
        merge_iou=cfg.merge_iou_threshold,
    )
    ok(f"detector={cfg.detector} viewport={cfg.viewport.width}x{cfg.viewport.height}")

    # -----------------------------------------------------------------------
    step("observe")
    t0 = time.time()
    obs = perceiver.observe(OUT / "frame.png")
    cold = time.time() - t0
    t0 = time.time()
    obs = perceiver.observe(OUT / "frame.png")
    warm = time.time() - t0

    controls = [e for e in obs.elements if e.source is not ElementSource.OCR]
    texts = [e for e in obs.elements if e.source is ElementSource.OCR]
    ok(f"{len(obs.elements)} elements ({len(controls)} controls, {len(texts)} text) "
       f"cold {cold:.1f}s warm {warm:.1f}s")
    if not obs.elements:
        bad("no elements at all — nothing above perception can run")
    if [e.id for e in obs.elements] != [f"e{i}" for i in range(len(obs.elements))]:
        bad("element ids are not a dense reading-order sequence")

    # -----------------------------------------------------------------------
    step("anchors a capability would target")
    joined = " ".join((e.text or "") for e in obs.elements).lower()
    for anchor in ANCHORS:
        if anchor in joined:
            ok(f"readable: {anchor!r}")
        else:
            bad(f"anchor {anchor!r} not readable — steps targeting it cannot resolve")

    # -----------------------------------------------------------------------
    step("labelled controls")
    # A control with no text is one the model can only refer to by position, and
    # one whose risk we cannot classify. Some are genuinely icon-only; a page
    # where *none* are labelled means the merge rule has stopped working.
    labelled = [e for e in controls if (e.text or "").strip()]
    for e in labelled[:10]:
        ok(f"{e.id} {e.role}: {e.text!r}")
    if controls and not labelled:
        bad(f"{len(controls)} controls detected, none carry text — merge is broken")

    # -----------------------------------------------------------------------
    step("spatial queries")
    index = ElementIndex(obs.elements)
    label = next((e for e in obs.elements if (e.text or "").strip().lower() == "savings"), None)
    if label is None:
        bad("no element reads exactly 'savings' — cannot test right_of()")
    else:
        neighbours = index.right_of(label)
        if neighbours:
            ok(f"right of 'Savings': {[n.text for n in neighbours[:3]]}")
        else:
            bad("nothing found to the right of 'Savings'")
        ok(f"{len(index.below(label))} elements below it")

    # -----------------------------------------------------------------------
    step("set-of-marks")
    overlay = annotate(obs, OUT / "annotated.png")
    digest = candidate_digest(obs.elements)
    ok(f"overlay {overlay} ({overlay.stat().st_size / 1e3:.0f} KB)")
    ok(f"digest {len(digest)} candidates, {truncated(obs.elements)} truncated")

    print("\n" + "=" * 64)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("perception OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
