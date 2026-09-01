#!/usr/bin/env python3
"""What perception costs, measured rather than assumed.

    docker compose exec desktop python3 scripts/bench_perception.py
    docker compose exec desktop python3 scripts/bench_perception.py \
        --frames /data/evidence/<run_id>/frames

Perception is where a run's time goes — on a dense back-office screen an observation is
~2.4s, of which text recognition is ~98%. Every step pays for at least one.

Three questions, one command:

  where     the split across detect / recognise / merge
  engine    onnxruntime (CPU) against torch (the GPU the detector already uses), which is
            the switch `CUA_OCR_ENGINE` selects
  side_len  the detector's input scale, measured against *anchor correctness* rather than
            line count

A smaller `side_len` reads faster and misreads intermittently — one sample reads the "View"
link correctly and the next reads "Yew" — so changing it needs repeated trials, which is what
`--trials` is for.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any

for _name in list(logging.root.manager.loggerDict) + ["RapidOCR"]:
    logging.getLogger(_name).setLevel(logging.ERROR)

from cua.config import settings  # noqa: E402
from cua.perception.detect import build_detector  # noqa: E402
from cua.perception.merge import merge  # noqa: E402
from cua.perception.ocr import RapidOcrTextReader  # noqa: E402
from cua.schema import Viewport  # noqa: E402

CFG = settings()

# Text the shipped capability resolves against. A side length that cannot read these is
# broken however fast it is, and a line count does not show it.
ANCHORS = ("View", "Primary Savings", "Member Profile")


def timed(fn: Any, trials: int) -> tuple[list[float], Any]:
    fn()                                              # warm: weights, kernels, autotune
    times, out = [], None
    for _ in range(trials):
        began = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - began) * 1000)
    return times, out


def show(label: str, times: list[float], extra: str = "") -> None:
    p95 = sorted(times)[min(len(times) - 1, int(len(times) * 0.95))]
    print(
        f"  {label:<30} {min(times):7.0f} ms min "
        f"{statistics.median(times):7.0f} med {p95:7.0f} p95   {extra}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--frames", default="", help="directory of PNGs to measure against")
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--sides", default="960,1280,1600", help="det_side_len values to sweep")
    ap.add_argument("--engines", default="onnxruntime", help="comma-separated OCR backends")
    args = ap.parse_args()

    frames = sorted(Path(args.frames).glob("*.png")) if args.frames else []
    if not frames:
        print(
            "no frames given. Point --frames at a run's evidence:\n"
            "  --frames /data/evidence/<run_id>/frames\n"
            "Any PNG of the surface at the recording viewport will do."
        )
        return 2

    vp = Viewport(width=CFG.display_width, height=CFG.display_height)
    detector = build_detector(
        CFG.detector, CFG.omniparser_repo,
        CFG.omniparser_repo_file, CFG.detect_conf_threshold,
    )

    print(f"viewport {vp.width}x{vp.height} · {args.trials} trials · {len(frames)} frames")
    for frame in frames:
        print(f"\n=== {frame.name} " + "=" * max(0, 50 - len(frame.name)))

        times, boxes = timed(lambda f=frame: detector.detect(f, vp), args.trials)
        show("detector", times, f"{len(boxes)} boxes")

        for engine in args.engines.split(","):
            for side in (int(s) for s in args.sides.split(",")):
                reader = RapidOcrTextReader(
                    conf_threshold=CFG.ocr_conf_threshold,
                    det_side_len=side,
                    engine=engine.strip(),
                )
                try:
                    times, lines = timed(lambda r=reader, f=frame: r.read(f, vp), args.trials)
                except Exception as e:  # noqa: BLE001 - a backend that will not load is data
                    print(f"  {engine}/{side:<20} unavailable: {type(e).__name__}: {e}")
                    continue
                exact = {(e.text or "").strip() for e in lines}
                missing = [a for a in ANCHORS if a not in exact]
                verdict = f"{len(lines)} lines" + (
                    "" if not missing else f"  MISREAD: {missing}"
                )
                marker = " <-- configured" if (
                    side == CFG.ocr_det_side_len and engine.strip() == CFG.ocr_engine
                ) else ""
                show(f"ocr {engine.strip()} side={side}{marker}", times, verdict)

        reader = RapidOcrTextReader(
            conf_threshold=CFG.ocr_conf_threshold,
            det_side_len=CFG.ocr_det_side_len, engine=CFG.ocr_engine,
        )
        lines = reader.read(frame, vp)
        times, merged = timed(
            lambda b=boxes, ln=lines: merge(list(b), list(ln), CFG.merge_iou_threshold),
            args.trials,
        )
        show("merge", times, f"{len(merged)} elements")

    print(
        "\nOne observation is detector + ocr + merge. Every step pays for at least\n"
        "one; before the reuse in `_clear_the_way` it paid for two."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
