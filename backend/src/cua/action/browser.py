"""Browser driver.

Playwright is used as an input engine and a process manager, not as a locator
library. There is no `page.locator()` anywhere in this file and there should never
be one: the moment element resolution happens inside Playwright, the design stops
generalizing to a surface that has no DOM, and the artifact stops being a
description of what a person does.

What Playwright gives us that a raw X-level driver would not:
  - a browser process we can start, stop and configure reproducibly
  - `page.mouse` / `page.keyboard`, which inject trusted events
  - `page.url()` for evidence

Chromium runs *headful on the Xvfb display*. Headless would be faster and would
also make the §3.6 handoff a lie — there would be no window for a human to take
over.

Coordinate spaces
-----------------
Perception photographs the whole display; Playwright's mouse works in page
coordinates. The window is sized to the display and put fullscreen at start, which
should make the two identical, and the offset is then *measured and checked*
anyway. A silent off-by-85px is the kind of bug that produces a plausible wrong
click, reports success on every step, and is invisible in a log of coordinates —
so `start()` refuses to hand back a session whose page and display disagree.
"""

from __future__ import annotations

import os
from typing import Any

from ..schema import Point, Viewport

_CHROMIUM_ARGS = (
    "--window-position=0,0",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-session-crashed-bubble",
    "--disable-infobars",
    # "Chrome is being controlled by automated test software" renders an infobar
    # that shifts the page down by ~35px. Harmless to a DOM-based tool; to a
    # coordinate-based one it moves every element on every page.
    "--disable-blink-features=AutomationControlled",
)

# Chromium's window manager rounds the fullscreen size down by a pixel. Anything
# beyond a couple of pixels is a real mismatch, not rounding.
_SIZE_TOLERANCE_PX = 4


class BrowserDriver:
    """Implements `action.base.Driver`."""

    def __init__(self, display: str, viewport: Viewport, control: Any | None = None) -> None:
        self.display = display
        self.viewport = viewport
        # The control token, when a run has one. Checked here rather than in the
        # runner: an escalation path that forgot to yield still cannot inject
        # input while an operator is holding the mouse.
        self.control = control
        self._page: Any | None = None
        self._browser: Any | None = None
        self._playwright: Any | None = None
        self._origin: tuple[int, int] = (0, 0)   # display -> page offset, measured at start

    # --- lifecycle -----------------------------------------------------------

    async def start(self, start_url: str | None = None) -> None:
        """Launch headful Chromium on the X display and measure the origin offset."""
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,
            args=[
                *_CHROMIUM_ARGS,
                f"--window-size={self.viewport.width},{self.viewport.height}",
            ],
            env={**os.environ, "DISPLAY": self.display},
        )
        # `no_viewport=True`, not `viewport=None`: the latter reads as "unspecified"
        # and Playwright then emulates its default 1280x720 render surface inside
        # whatever window it opened. The page would render at one size while we
        # photograph another, and every coordinate this driver translates would be
        # wrong by a scale factor. This is not a hypothetical — it is what the
        # first version of this file did.
        context = await self._browser.new_context(no_viewport=True)
        self._page = await context.new_page()
        await self._fill_display(context)
        if start_url:
            await self.navigate(start_url)
        await self._measure_origin()

    async def _fill_display(self, context: Any) -> None:
        """Size the window to the display and drop the browser chrome.

        Chromium ignores `--window-size` and `--kiosk` when Playwright launches it,
        so this is done over CDP: resize to the display, then go fullscreen, which
        is what removes the tab strip and the address bar. Two consequences worth
        the call — the model is not shown browser furniture it must learn to
        ignore, and an operator who takes over the live session gets the
        application rather than a browser they could navigate anywhere with.
        """
        cdp = await context.new_cdp_session(self._require_page())
        try:
            window = await cdp.send("Browser.getWindowForTarget")
            await cdp.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window["windowId"],
                    "bounds": {
                        "left": 0,
                        "top": 0,
                        "width": self.viewport.width,
                        "height": self.viewport.height,
                        "windowState": "normal",
                    },
                },
            )
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": window["windowId"], "bounds": {"windowState": "fullscreen"}},
            )
        finally:
            await cdp.detach()

    async def stop(self) -> None:
        """Close the browser.

        Never called while an intervention is open: the whole point of the handoff
        is that the session survives the transfer of control.
        """
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None
        self._page = None

    async def _measure_origin(self) -> None:
        """Measure display -> page translation once, from the page itself.

        Fullscreen should make this (0, 0). It is measured rather than assumed
        because a silent off-by-85px produces clicks that land one control above
        the intended one — plausible, wrong, and invisible in a log that records
        only coordinates. The first version of this driver had exactly that bug:
        it typed a password into empty space above the field and reported success
        on every step.

        The size check is the part that actually catches it. An offset can be
        compensated for; a page rendering at a different size than the display we
        photograph cannot, so it stops the session at start rather than producing
        a run of confidently wrong clicks.
        """
        g = await self._require_page().evaluate(
            "() => ({ sx: window.screenX, sy: window.screenY,"
            " ow: window.outerWidth, oh: window.outerHeight,"
            " iw: window.innerWidth, ih: window.innerHeight })"
        )
        self._origin = (
            int(g["sx"]),
            int(g["sy"]) + max(0, int(g["oh"]) - int(g["ih"])),
        )
        dw = self.viewport.width - int(g["iw"])
        dh = self.viewport.height - int(g["ih"]) - self._origin[1]
        if abs(dw) > _SIZE_TOLERANCE_PX or abs(dh) > _SIZE_TOLERANCE_PX:
            raise RuntimeError(
                f"page is {g['iw']}x{g['ih']} at origin {self._origin} on a "
                f"{self.viewport.width}x{self.viewport.height} display: the pixels we "
                f"photograph are not the pixels the page renders, so no coordinate "
                f"this driver produces can be trusted"
            )

    # --- input ---------------------------------------------------------------

    async def navigate(self, url: str) -> None:
        self._assert_control()
        # `domcontentloaded`, not `networkidle`: waiting is the perceiver's job and
        # it does it by watching the frame settle, which works on a page that
        # polls forever. Waiting for network silence on such a page does not.
        await self._require_page().goto(url, wait_until="domcontentloaded")

    async def click(self, p: Point, button: str = "left") -> None:
        self._assert_control()
        x, y = self._to_page(p)
        await self._require_page().mouse.click(x, y, button=button)

    async def type_text(self, text: str, secret: bool = False) -> None:
        # `secret` is honoured by not being used: the value is typed and never
        # returned, logged or stored here. The flag exists so callers above this
        # layer know which values must not reach evidence.
        self._assert_control()
        # A small per-key delay, because back-office forms routinely attach
        # keyup handlers that filter, mask or re-render as you type, and a
        # zero-delay burst outruns them.
        await self._require_page().keyboard.type(text, delay=20)

    async def key(self, keys: str) -> None:
        self._assert_control()
        await self._require_page().keyboard.press(keys)

    async def scroll(self, p: Point, dy: float) -> None:
        self._assert_control()
        x, y = self._to_page(p)
        page = self._require_page()
        await page.mouse.move(x, y)
        await page.mouse.wheel(0, dy * self.viewport.height)

    def current_url(self) -> str | None:
        page = self._page
        return None if page is None else str(page.url)

    # --- internals -----------------------------------------------------------

    def _to_page(self, p: Point) -> tuple[float, float]:
        """Normalized display coords -> absolute page coords."""
        return (
            p.x * self.viewport.width - self._origin[0],
            p.y * self.viewport.height - self._origin[1],
        )

    def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError("browser not started")
        return self._page

    def _assert_control(self) -> None:
        if self.control is not None:
            self.control.assert_automation()
