"""Desktop driver — the heterogeneity seam, deliberately not built out.

`xdotool` against the same X display, where every primitive maps directly and `navigate` has
no meaning. The file exists to make one claim checkable rather than asserted: nothing above
the action layer changes when the surface does. The check is the `_satisfies_driver` binding
below, which mypy fails if this class ever drifts from the protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..schema import Point, Viewport


class DesktopDriver:
    """Satisfies `action.base.Driver` over xdotool. Every body raises: the shape is the
    deliverable here, not the implementation, and a half-written click is worse than an
    honest `NotImplementedError`."""

    def __init__(self, display: str, viewport: Viewport) -> None:
        self.display = display
        self.viewport = viewport

    async def navigate(self, url: str) -> None:
        raise NotImplementedError("desktop surfaces have no navigate primitive")

    async def reload(self) -> None:
        raise NotImplementedError

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


if TYPE_CHECKING:
    from .base import Driver

    # The claim in the module docstring, type-checked. Nothing constructs a
    # `DesktopDriver`, so without this the protocol could drift away from it silently.
    _satisfies_driver: type[Driver] = DesktopDriver
