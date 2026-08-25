"""Offline driver — replay the decision path against recorded frames.

Point it at a previous run's frames and the engine re-derives every decision from the
same pixels, with no GPU, no credentials and no browser. It proves the decision path is
reproducible from pixels alone; it does not prove the application responds the way it
did, because nothing is being clicked.
"""

from __future__ import annotations

from typing import Any

from ..schema import Point


class OfflineDriver:
    """Implements `action.base.Driver` over a recorded frame sequence.

    Acting advances the sequence, because in a recorded run each frame is the
    state that *followed* an action. Reads (extraction) do not advance it, for
    the same reason.
    """

    def __init__(self, screen: Any, url: str | None = None) -> None:
        self.screen = screen
        self.calls: list[tuple[str, Any]] = []
        self._url = url

    async def navigate(self, url: str) -> None:
        self.calls.append(("navigate", url))
        self._url = url
        self.screen.advance()

    async def reload(self) -> None:
        self.calls.append(("reload", self._url))
        self.screen.advance()

    async def click(self, p: Point, button: str = "left") -> None:
        self.calls.append(("click", (round(p.x, 4), round(p.y, 4))))
        self.screen.advance()

    async def type_text(self, text: str, secret: bool = False) -> None:
        self.calls.append(("type", "***" if secret else text))

    async def key(self, keys: str) -> None:
        self.calls.append(("key", keys))
        self.screen.advance()

    async def scroll(self, p: Point, dy: float) -> None:
        self.calls.append(("scroll", dy))
        self.screen.advance()

    def current_url(self) -> str | None:
        """The URL the frames were recorded at.

        Supplied by the caller rather than invented: a recorded PNG does not carry
        one, and a `url_matches` checkpoint that silently passed against a made-up
        value would be worse than one that cannot be evaluated.
        """
        return self._url
