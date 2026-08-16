"""Desktop driver — the §3.7 seam, deliberately not built out.

`xdotool` against the same X display the browser is on. Every primitive maps
directly: click is `xdotool mousemove/click`, type is `xdotool type`, key is
`xdotool key`, scroll is button 4/5. `navigate` has no meaning and returns a
policy error rather than silently succeeding.

This file exists to make one claim concrete and checkable rather than asserted in
prose: *nothing above the action layer changes when the surface does*. A desktop
capability would use the same artifact schema, the same resolver ladder, the same
checkpoint kinds (minus `url_matches`) and the same replay engine. Perception
already reads pixels rather than a DOM, so it needs no change at all.

Stubbed on purpose, documented as a cut in REPORT §7.
"""

from __future__ import annotations

from ..schema import Point, Viewport


class DesktopDriver:
    """Implements `action.base.Driver` over xdotool. Not exercised by the demo."""

    def __init__(self, display: str, viewport: Viewport) -> None:
        self.display = display
        self.viewport = viewport

    def navigate(self, url: str) -> None:
        raise NotImplementedError("desktop surfaces have no navigate primitive")

    def click(self, p: Point, button: str = "left") -> None:
        raise NotImplementedError

    def type_text(self, text: str, secret: bool = False) -> None:
        raise NotImplementedError

    def key(self, keys: str) -> None:
        raise NotImplementedError

    def scroll(self, p: Point, dy: float) -> None:
        raise NotImplementedError

    def current_url(self) -> str | None:
        return None
