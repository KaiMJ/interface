"""Recording what the human did.

The part a headful-Playwright design cannot do: Playwright observes the events it
issues, and a manual click is not one of them. Capturing at the X layer means the same
code records a human operating a browser and a human operating a desktop app. Typed text
is counted, never stored — the operator may be entering a credential.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from ..clock import now_iso
from ..schema import HumanAction

# Sampled, not recorded wholesale: a mouse crossing the screen emits hundreds of motion
# events. In display pixels.
_MOVE_THRESHOLD = 60

_BUTTONS = {1: "click", 2: "click", 3: "click", 4: "scroll", 5: "scroll"}


class HumanActionWatcher:
    """Records operator input for the life of one intervention."""

    def __init__(self, display: str) -> None:
        self.display = display
        self._actions: list[HumanAction] = []
        self._running = False
        self._thread: threading.Thread | None = None
        self._display: Any | None = None
        self._context: Any | None = None
        self._keystrokes = 0
        self._last_move: tuple[int, int] | None = None
        self.unavailable: str | None = None

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        try:
            from Xlib import X
            from Xlib import display as xdisplay
            from Xlib.ext import record
        except ImportError as e:  # pragma: no cover - the dependency is declared
            self.unavailable = f"XRecord unavailable: {e}"
            return

        try:
            self._display = xdisplay.Display(self.display)
        except Exception as e:
            # No X on this host — a headless deployment, or an ssh session. The
            # handoff still happens; routes.py records this string in its place.
            self.unavailable = f"no display {self.display}: {e}"
            return
        if not self._display.has_extension("RECORD"):
            self.unavailable = "the X server has no RECORD extension"
            return

        self._context = self._display.record_create_context(
            0,
            [record.AllClients],
            [
                {
                    "core_requests": (0, 0),
                    "core_replies": (0, 0),
                    "ext_requests": (0, 0, 0, 0),
                    "ext_replies": (0, 0, 0, 0),
                    "delivered_events": (0, 0),
                    "device_events": (X.KeyPress, X.MotionNotify),
                    "errors": (0, 0),
                    "client_started": False,
                    "client_died": False,
                }
            ],
        )
        self._running = True
        self._thread = threading.Thread(target=self._pump, name="human-watch", daemon=True)
        self._thread.start()

    def _pump(self) -> None:
        # Blocks until another connection disables the context. Hence the thread.
        assert self._display is not None
        self._display.record_enable_context(self._context, self._handle)
        self._display.record_free_context(self._context)

    def stop(self) -> list[HumanAction]:
        if self._running and self._display is not None:
            from Xlib import display as xdisplay

            # A second connection, because the first one is blocked inside the tap.
            closer = xdisplay.Display(self.display)
            closer.record_disable_context(self._context)
            closer.flush()
            closer.close()
            self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._flush_keystrokes()
        return list(self._actions)

    # --- capture -------------------------------------------------------------

    def _handle(self, reply: Any) -> None:
        if getattr(reply, "category", None) is not None and reply.category != 0:
            return
        data = getattr(reply, "data", b"")
        if not data:
            return

        from Xlib import X
        from Xlib.protocol import rq

        display = self._display
        if display is None:  # pragma: no cover - stopped mid-callback
            return
        field = rq.EventField("")
        while data:
            event, data = field.parse_binary_value(data, display.display, None, None)
            if event.type in (X.KeyPress, X.KeyRelease):
                if event.type == X.KeyPress:
                    # Count, never content: the operator may be typing a credential.
                    self._keystrokes += 1
                continue

            self._flush_keystrokes()
            if event.type == X.ButtonPress:
                kind = _BUTTONS.get(event.detail, "click")
                self._record(kind, event.root_x, event.root_y, f"button {event.detail}")
            elif event.type == X.MotionNotify:
                self._maybe_move(event.root_x, event.root_y)

    def _maybe_move(self, x: int, y: int) -> None:
        if self._last_move is not None:
            dx, dy = x - self._last_move[0], y - self._last_move[1]
            if dx * dx + dy * dy < _MOVE_THRESHOLD * _MOVE_THRESHOLD:
                return
        self._last_move = (x, y)
        self._record("move", x, y, None)

    def _flush_keystrokes(self) -> None:
        if self._keystrokes:
            self._record("key", None, None, f"{self._keystrokes} keystrokes")
            self._keystrokes = 0

    def _record(self, kind: str, x: int | None, y: int | None, detail: str | None) -> None:
        self._actions.append(HumanAction(at=now_iso(), kind=kind, x=x, y=y, detail=detail))

    # --- evidence ------------------------------------------------------------

    def snapshot(self, out_path: Path, label: str) -> Path:
        """Evidence frame at handoff / handback.

        Captured here rather than through the perceiver: it must work while the automation is
        stopped and holds no control.
        """
        from ..perception.screen import XDisplayScreen
        from ..schema import Viewport

        out_path.parent.mkdir(parents=True, exist_ok=True)
        screen = XDisplayScreen(self.display, Viewport(width=1, height=1))
        try:
            screen.capture(out_path)
        finally:
            screen.close()
        return out_path
