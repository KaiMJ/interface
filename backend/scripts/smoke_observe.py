#!/usr/bin/env python3
"""Does `cua.perception` see a given surface — and does it hold its assumptions?

This is the first thing to run against a page nobody has calibrated for. Run
inside the desktop container:

    docker compose exec desktop python3 scripts/smoke_observe.py
    docker compose exec desktop python3 scripts/smoke_observe.py --display
    docker compose exec desktop python3 scripts/smoke_observe.py --image /tmp/x.png
    docker compose exec desktop python3 scripts/smoke_observe.py \
        --url https://some-app.example/page --expect "Account" --expect "Balance"

Nothing here knows the target application. `--url` picks the page, `--expect`
names the strings a capability would need to anchor on, and both default to what
the shipped demo app offers so the common case stays one word long.

`scripts/smoke_perception.py` answers a different question — whether the two
external dependencies load and run at all. This one exercises the code we wrote:
capture -> detect -> read -> merge -> set-of-marks, through `Perceiver`, with the
same settings the real runs use.

Three findings matter more than the rest, because each one localizes a *different*
repair:

  - **zero labelled controls** means the merge thresholds do not fit this surface
    (`calibration.label_containment` / `label_size_ratio` / `container_frame_area`).
    An anonymous box is one the model cannot ask for by name and whose risk cannot
    be classified.
  - **rows spanning more than one visual line** means `calibration.row_tolerance`
    is too loose for this surface's line spacing, and a row predicate will match
    terms a human reads as two separate records.
  - **settling by text rather than pixels** means the surface animates. Not a
    fault — the fallback exists for it — but it doubles the settle budget and is
    worth knowing before blaming a slow run on the model.

Findings are *reported*, not asserted, except for the ones that mean nothing above
perception can run at all. A page that fails an `--expect` is information about
the page, not a broken system.

Writes the frame and its overlay to /tmp/smoke/ so a human can look at what the
model would have been shown — which answers most of these faster than any check.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from statistics import median

from cua.calibration import CALIBRATION
from cua.config import settings
from cua.perception import ElementIndex, Perceiver
from cua.perception.detect import build_detector
from cua.perception.ocr import OnnxTextReader
from cua.perception.screen import ImageFileScreen, XDisplayScreen
from cua.perception.som import annotate, candidate_digest, truncated
from cua.runtime import build_policy, entry_url
from cua.schema import Bbox, ElementSource, SettledBy

# Where this deployment's install of the app lives — from its policy, or the
# CUA_TARGET_BASE_URL override. One answer, the same one every command uses.
BASE_URL = entry_url(settings(), build_policy(settings()))

OUT = Path("/tmp/smoke")
# What the shipped demo app offers. Replaced wholesale by --expect; nothing in
# the checks below is specific to these strings.
DEFAULT_EXPECT = ("dolores", "12345", "savings", "available balance", "view")
DEFAULT_PATH = "/members/12345"

failures: list[str] = []
findings: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    """Nothing above perception can run. Fails the script."""
    print(f"  FAIL  {msg}")
    failures.append(msg)


def note(msg: str) -> None:
    """A fact about this surface that should change what you do next."""
    print(f"  NOTE  {msg}")
    findings.append(msg)


def capture_target(path: Path, url: str) -> None:
    """Screenshot a page headlessly, as a stand-in for the live display.

    The X display only shows something once the action layer launches a browser
    onto it; until then this keeps the perception check runnable. This is the one
    place a demo-app convenience survives: the teller cookie is set so the default
    URL renders signed in. Against any other host it is inert.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": settings().display_width, "height": settings().display_height}
        )
        if url.startswith(BASE_URL):
            page.context.add_cookies(
                [{"name": "teller_sid", "value": "teller01", "url": BASE_URL}]
            )
        page.goto(url, wait_until="networkidle")
        page.screenshot(path=str(path))
        browser.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--display", action="store_true", help="observe the live X display")
    ap.add_argument("--image", type=Path, help="observe a PNG instead of capturing one")
    ap.add_argument("--url", default=f"{BASE_URL}{DEFAULT_PATH}", help="page to capture")
    ap.add_argument(
        "--expect",
        action="append",
        default=None,
        help="text a capability would anchor on; repeatable",
    )
    args = ap.parse_args()
    expect = tuple(args.expect) if args.expect else (
        DEFAULT_EXPECT if args.url == f"{BASE_URL}{DEFAULT_PATH}" else ()
    )

    cfg = settings()
    OUT.mkdir(parents=True, exist_ok=True)

    step("perceiver")
    if args.display:
        screen: XDisplayScreen | ImageFileScreen = XDisplayScreen(cfg.display, cfg.viewport)
        ok(f"capturing X display {cfg.display}")
    else:
        frame = args.image or (OUT / "page.png")
        if args.image is None:
            capture_target(frame, args.url)
            ok(f"captured {args.url} -> {frame}")
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
    if not expect:
        print("  (nothing to check — pass --expect to test anchors on this page)")
    for anchor in expect:
        if anchor.lower() in joined:
            ok(f"readable: {anchor!r}")
        else:
            note(f"{anchor!r} is not readable here — a step anchored on it cannot resolve")

    # -----------------------------------------------------------------------
    step("labelled controls")
    # A control with no text is one the model can only refer to by position, and
    # one whose risk we cannot classify. Some are genuinely icon-only; a page
    # where *none* are labelled means the merge rule has stopped working.
    labelled = [e for e in controls if (e.text or "").strip()]
    for e in labelled[:10]:
        ok(f"{e.id} {e.role}: {e.text!r}")
    if controls and not labelled:
        note(
            f"{len(controls)} controls detected and none carry text: the merge "
            f"thresholds do not fit this surface (calibration.label_containment / "
            f"label_size_ratio / container_frame_area)"
        )
    elif controls:
        ok(f"{len(labelled)}/{len(controls)} controls carry text")

    # -----------------------------------------------------------------------
    step("rows")
    # Whether tabular data reconstructs at all. A row spanning more than one visual
    # line means row_tolerance is too loose here, and a predicate would match terms
    # a human reads as two separate records — in a banking app, the wrong one.
    index = ElementIndex(obs.elements)
    rows = index.rows(Bbox(x=0.0, y=0.0, w=1.0, h=1.0))
    widest = sorted(rows, key=len, reverse=True)[:4]
    for row in widest:
        cells = [(c.text or "").strip() for c in row if (c.text or "").strip()]
        # Multi-line if the row's vertical spread exceeds the height of the text
        # in it. Measured against the row's own glyphs rather than a fixed
        # constant, because line height is the thing that varies between surfaces
        # and is exactly what makes a fixed tolerance wrong somewhere else.
        spread = max(c.bbox.y for c in row) - min(c.bbox.y for c in row)
        line = median(c.bbox.h for c in row)
        multiline = spread > line
        ok(f"{len(cells)} cells: {cells[:6]}{'  <-- spans >1 line' if multiline else ''}")
        if multiline:
            note(
                f"a reconstructed row spans {spread:.4f} of the frame height on a "
                f"{line:.4f} line: {cells[:4]}. Predicates over this region would "
                f"match terms a human reads as separate records. Either it is a "
                f"key/value block rather than a table (harmless — nothing targets "
                f"it), or calibration.row_tolerance ({CALIBRATION.row_tolerance}) "
                f"and band_overlap ({CALIBRATION.band_overlap}) are too loose here."
            )
    if not widest:
        note("nothing clustered into a row — this page may have no tabular data")

    # -----------------------------------------------------------------------
    step("spatial queries")
    anchor = next(
        (e for e in obs.elements if len((e.text or "").strip()) > 3 and index.right_of(e)),
        None,
    )
    if anchor is None:
        note("no element has a right-hand neighbour: `right_of` targeting has nothing to bind to")
    else:
        neighbours = index.right_of(anchor)
        ok(f"right of {anchor.text!r}: {[n.text for n in neighbours[:3]]}")
        ok(f"{len(index.below(anchor))} elements below it")

    # -----------------------------------------------------------------------
    step("settling")
    settled = perceiver.settle(OUT / "settled.png", cfg.settle_timeout_ms, cfg.settle_poll_ms)
    if settled.settled_by is SettledBy.PIXELS:
        ok("settled on identical pixels — this surface is static")
    else:
        note(
            "pixels never converged; settled on stable text instead. Something on "
            "this page animates (caret, spinner, clock), so every step pays the "
            "settle budget twice"
        )

    # -----------------------------------------------------------------------
    step("set-of-marks")
    overlay = annotate(obs, OUT / "annotated.png")
    digest = candidate_digest(obs.elements)
    ok(f"overlay {overlay} ({overlay.stat().st_size / 1e3:.0f} KB)")
    ok(f"digest {len(digest)} candidates, {truncated(obs.elements)} truncated")

    print("\n" + "=" * 64)
    if findings:
        print(f"{len(findings)} finding(s) about this surface:")
        for f in findings:
            print(f"  - {f}")
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"perception ran; frame and overlay in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
