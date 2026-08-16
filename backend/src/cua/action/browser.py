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
coordinates. The offset between them (window position plus browser chrome height)
is measured once at session start and applied here, in one place. Kiosk mode keeps
the chrome height at zero, but the translation is done properly regardless — a
silent off-by-40px is the kind of bug that produces a plausible wrong click.
"""

from __future__ import annotations

from ..schema import Point, Viewport


class BrowserDriver:
    """Implements `action.base.Driver`."""

    def __init__(self, display: str, viewport: Viewport) -> None:
        self.display = display
        self.viewport = viewport
        self._page: object | None = None
        self._origin: tuple[int, int] = (0, 0)   # display -> page offset, measured at start

    async def start(self, start_url: str | None = None) -> None:
        """Launch headful Chromium on the X display and measure the origin offset."""
        raise NotImplementedError

    async def stop(self) -> None:
        """Close the browser.

        Never called while an intervention is open: the whole point of the handoff
        is that the session survives the transfer of control.
        """
        raise NotImplementedError

    def _to_page(self, p: Point) -> tuple[float, float]:
        """Normalized display coords -> absolute page coords."""
        raise NotImplementedError

    def navigate(self, url: str) -> None:
        raise NotImplementedError

    def click(self, p: Point, button: str = "left") -> None:
        raise NotImplementedError

    def type_text(self, text: str, secret: bool = False) -> None:
        raise NotImplementedError

    def key(self, keys: str) -> None:
        raise NotImplementedError

    def scroll(self, p: Point, dy: float) -> None:
        raise NotImplementedError

    def current_url(self) -> str | None:
        raise NotImplementedError
