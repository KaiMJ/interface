"""Desktop driver — the heterogeneity seam, deliberately not built out.

`xdotool` against the same X display; every primitive maps directly, and `navigate` has
no meaning here so it returns a policy error rather than silently succeeding. The file
exists to make one claim checkable rather than asserted: nothing above the action layer
changes when the surface does.
"""

from __future__ import annotations

from ..schema import Point, Viewport


class DesktopDriver:
    """Implements `action.base.Driver` over xdotool. Not exercised by the demo."""

    def __init__(self, display: str, viewport: Viewport) -> None:
        self.display = display
        self.viewport = viewport

    async def navigate(self, url: str) -> None:
        raise NotImplementedError("desktop surfaces have no navigate primitive")

    async def click(self, p: Point, button: str = "left") -> None:
        raise NotImplementedError

    async def type_text(self, text: str, secret: bool = False) -> None:
        raise NotImplementedError

    async def key(self, keys: str) -> None:
        raise NotImplementedError

    async def scroll(self, p: Point, dy: float) -> None:
        raise NotImplementedError

    def current_url(self) -> str | None:
        return None
