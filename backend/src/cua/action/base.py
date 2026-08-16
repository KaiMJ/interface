"""The action seam.

Symmetric with perception: one narrow protocol, one set of primitives, no
knowledge of what is on the other side. Everything above this layer speaks in
normalized display coordinates.

The primitive list is identical to `schema.Primitive` — the discovery agent's
action space, the artifact's step vocabulary, and the driver's capabilities are
the same set by construction. Anything the agent can do, an artifact can record;
anything an artifact records, replay can execute.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema import Point


@runtime_checkable
class Driver(Protocol):
    """Injects input into a surface."""

    def navigate(self, url: str) -> None: ...

    def click(self, p: Point, button: str = "left") -> None: ...

    def type_text(self, text: str, secret: bool = False) -> None:
        """Type into whatever currently has focus.

        `secret=True` suppresses the value from every log line and evidence
        record. Typing is the only place a credential enters the system, and it
        enters *here*, below the point where anything is serialized — an artifact
        stores `{{password}}`, never the value.
        """
        ...

    def key(self, keys: str) -> None: ...

    def scroll(self, p: Point, dy: float) -> None: ...

    def current_url(self) -> str | None:
        """Best-effort. Convenience for checkpoints and evidence only.

        Never used for targeting, and a driver for a surface with no notion of a
        URL (a desktop app) returns None. Any checkpoint that depends on this is
        implicitly browser-only, which is why `url_matches` is not the default.
        """
        ...
