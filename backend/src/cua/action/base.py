"""The action seam.

One narrow protocol, symmetric with perception, speaking normalized display
coordinates. The primitive list is identical to `schema.Primitive`: the agent's action
space, the artifact's step vocabulary and the driver's capabilities are one set by
construction. Async because escalation parks a run on an event, and the control plane
must keep answering while it sits parked.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema import Point


@runtime_checkable
class Driver(Protocol):
    """Injects input into a surface."""

    async def navigate(self, url: str) -> None: ...

    async def reload(self) -> None:
        """Re-request the current page.

        A distinct primitive rather than `navigate(current_url())` because on a
        surface with no URL — a desktop app — re-requesting is still meaningful
        and an address is not. It is a *recovery* verb, not a step verb: no
        artifact records it, because a flow that needs a reload to work is a flow
        that does not work.
        """
        ...

    async def click(self, p: Point, button: str = "left") -> None: ...

    async def type_text(self, text: str, secret: bool = False) -> None:
        """Type into whatever currently has focus.

        `secret=True` suppresses the value from every log line and evidence
        record. Typing is the only place a credential enters the system, and it
        enters *here*, below the point where anything is serialized — an artifact
        stores `{{password}}`, never the value.
        """
        ...

    async def key(self, keys: str) -> None: ...

    async def scroll(self, p: Point, dy: float) -> None:
        """Scroll by `dy` display heights at point `p`. Positive is downward.

        Normalized like every other quantity that crosses this seam, so a recorded
        scroll means the same thing on a differently-sized display.
        """
        ...

    def current_url(self) -> str | None:
        """Best-effort. Convenience for checkpoints and evidence only.

        Never used for targeting, and a driver for a surface with no notion of a
        URL (a desktop app) returns None. Any checkpoint that depends on this is
        implicitly browser-only, which is why `url_matches` is not the default.
        """
        ...
