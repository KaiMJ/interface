#!/usr/bin/env python3
"""Can the system actually drive the application? The spine, end to end.

Run inside the desktop container, with the target app up:

    docker compose exec desktop python3 scripts/smoke_drive.py

No model, no artifact, no engine — this is the layer underneath all three, wired
by hand so that a failure here is unambiguous:

    XDisplayScreen  ->  Perceiver  ->  Resolver  ->  verify_target
                                                          |
                                            BrowserDriver (page.mouse)
                                                          |
                                                     verify_effect

What it proves, in order of how expensive each would be to discover later:

  1. Chromium comes up headful on the Xvfb display and the display we photograph
     is the page the browser is rendering — one coordinate space, no offset.
  2. A click resolved from *text on screen* lands on the control that text names.
     This is the single assumption every capability rests on.
  3. `relation=right_of` finds the input beside a label, which is how a form is
     filled without a DOM.
  4. Checkpoints see the state the actions produced.

It signs in (credentials from config, never from an artifact), opens a member,
and reads a balance the same way a recorded capability would.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from cua.action.browser import BrowserDriver
from cua.config import settings
from cua.perception import ElementIndex, Perceiver
from cua.perception.detect import build_detector
from cua.perception.ocr import OnnxTextReader
from cua.perception.screen import XDisplayScreen
from cua.resolve import Resolver, Unresolvable, evaluate, verify_target
from cua.schema import (
    Bbox,
    CheckKind,
    Checkpoint,
    MatchMode,
    Normalizer,
    Observation,
    Relation,
    Target,
)

OUT = Path("/tmp/smoke/drive")
MONEY = (Normalizer.CASEFOLD, Normalizer.COLLAPSE_WS, Normalizer.STRIP_CURRENCY)

failures: list[str] = []


def step(name: str) -> None:
    print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))


def ok(msg: str) -> None:
    print(f"  ok    {msg}")


def bad(msg: str) -> None:
    print(f"  FAIL  {msg}")
    failures.append(msg)


async def main() -> int:
    cfg = settings()
    OUT.mkdir(parents=True, exist_ok=True)

    driver = BrowserDriver(cfg.display, cfg.viewport)
    perceiver = Perceiver(
        screen=XDisplayScreen(cfg.display, cfg.viewport),
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
        url_provider=driver.current_url,
    )
    resolver = Resolver(allow_vlm=False)
    frame = 0

    async def settle(label: str) -> Observation:
        """Perception is CPU-bound, so it runs in a thread — as it does in the
        engine. The event loop stays free for the control plane."""
        nonlocal frame
        frame += 1
        obs = await asyncio.to_thread(
            perceiver.settle,
            OUT / f"{frame:02d}-{label}.png",
            cfg.settle_timeout_ms,
            cfg.settle_poll_ms,
        )
        print(f"        [{label}] {len(obs.elements)} elements, url={obs.url}")
        return obs

    async def act_on(target: Target, obs: Observation, then: str, value: str = "") -> bool:
        """Resolve -> verify target -> act. The engine's per-step lifecycle, by hand."""
        try:
            resolution = resolver.resolve(target, obs)
        except Unresolvable as e:
            bad(f"{target.intent}: {e}")
            return False
        check = verify_target(target, resolution, obs)
        if not check.ok:
            bad(f"{target.intent}: {check.kind} expected={check.expected!r} "
                f"observed={check.observed!r}")
            return False
        ok(f"{target.intent} -> {resolution.tier.value} at "
           f"({resolution.point.x:.3f}, {resolution.point.y:.3f}) "
           f"matched={resolution.matched_text!r}")
        if then == "click":
            await driver.click(resolution.point)
        elif then == "type":
            await driver.click(resolution.point)
            await driver.type_text(value, secret=True)
        return True

    try:
        # -------------------------------------------------------------------
        step("browser on the X display")
        await driver.start(f"{cfg.target_base_url}/login")
        ok(f"chromium up on {cfg.display}, url={driver.current_url()}")
        # `start()` refuses to continue if the page and the display disagree about
        # size, so reaching this line already means the coordinate spaces match.
        ok(f"display->page origin offset {driver._origin}")

        obs = await settle("login")
        if not evaluate(Checkpoint(kind=CheckKind.TEXT_PRESENT, value="Staff Sign-On"), obs):
            bad("the sign-on screen is not what we are looking at")

        # -------------------------------------------------------------------
        step("fill a form with no DOM")
        # The field has no text of its own. It is found as "the thing to the right
        # of the words User ID" — which is also how it stays findable when the
        # form is restyled.
        user_field = Target(
            intent="type the teller id",
            target_desc="the User ID input, right of its label",
            anchor_text="User ID",
            anchor_match=MatchMode.CONTAINS,
            relation=Relation.RIGHT_OF,
        )
        pw_field = Target(
            intent="type the password",
            target_desc="the Password input, right of its label",
            anchor_text="Password",
            relation=Relation.RIGHT_OF,
        )
        if await act_on(user_field, obs, "type", cfg.target_username):
            obs = await settle("typed-user")
            await act_on(pw_field, obs, "type", cfg.target_password)

        obs = await settle("typed-both")
        await act_on(
            Target(intent="click Sign On", target_desc="the sign-on button", anchor_text="Sign On"),
            obs,
            "click",
        )

        obs = await settle("signed-in")
        if evaluate(Checkpoint(kind=CheckKind.URL_MATCHES, value="/members"), obs):
            ok("signed in — a click resolved from screen text landed on the button")
        else:
            bad(f"still at {obs.url} after clicking Sign On")

        # -------------------------------------------------------------------
        step("read a value the way a capability would")
        await driver.navigate(f"{cfg.target_base_url}/members/12345")
        obs = await settle("member")

        for name, check in {
            "member name is on screen": Checkpoint(
                kind=CheckKind.TEXT_PRESENT, value="Dolores"
            ),
            "the savings balance is present": Checkpoint(
                kind=CheckKind.TEXT_PRESENT, value="18204.55", normalize=MONEY
            ),
            "no session banner": Checkpoint(
                kind=CheckKind.TEXT_ABSENT, value="session has expired"
            ),
        }.items():
            if evaluate(check, obs):
                ok(name)
            else:
                bad(name)

        # The output an artifact would declare: the value beside the account's
        # nickname, read positionally rather than by guessing at a number.
        anchor = next(
            (e for e in obs.elements if "primary savings" in (e.text or "").lower()), None
        )
        if anchor is None:
            bad("could not find the savings account row")
        else:
            row = [e.text for e in ElementIndex(obs.elements).right_of(anchor)]
            ok(f"savings row reads {row}")
            if not any("18,204.55" in (t or "") for t in row):
                bad("the balance is not in the savings row — wrong row or wrong read")

        # -------------------------------------------------------------------
        step("clicking is verified, not assumed")
        # A target that resolves (the box is there) but is not what the recording
        # described. Nothing should be clicked.
        wrong = Target(
            intent="click a control that is no longer where it was",
            target_desc="a stale recorded coordinate",
            anchor_text="Wire Transfer",
            anchor_match=MatchMode.EXACT,
            bbox=Bbox(x=0.05, y=0.20, w=0.10, h=0.02),
        )
        resolution = resolver.resolve(wrong, obs)
        result = verify_target(wrong, resolution, obs)
        if result.ok:
            bad("a stale coordinate passed pre-click verification")
        else:
            ok(f"refused: {result.kind.value if result.kind else '?'} "
               f"expected={result.expected!r} observed={result.observed!r}")

    finally:
        await driver.stop()

    print("\n" + "=" * 64)
    if failures:
        print(f"{len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"spine OK — frames in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
