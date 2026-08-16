"""Control transfer.

The mechanism §3.6 asks for. Its shape follows from one requirement — the human
must operate *the same live session*, not a fresh one — and one hazard: the
automation and the operator are pointed at the same X display, so if both can act
at once they race, on a banking screen.

Hence control is a token with exactly one holder, and it is explicit state rather
than an implied consequence of nobody currently calling `click()`.

    AUTOMATION ──escalate──► NOBODY ──take_control──► HUMAN
         ▲                                              │
         └──────────── resume ◄──── NOBODY ◄────release ┘

`NOBODY` is not ceremony. It is the interval in which the automation has stopped
but the operator has not yet connected, and it is the state that makes "the agent
clicked while I was typing" impossible rather than unlikely.

What survives the transfer: the browser process, the X display, cookies and
session, the current page and any half-filled form, and the run's evidence
directory. Nothing is torn down. The runner is parked on an asyncio event, not
unwound.

Resume re-observes rather than trusting a step counter. The human may have
advanced the app several screens, gone back, or fixed something three steps
earlier. In replay the engine advances to the first step whose checkpoint already
holds — skip-forward, not blind resume. In discovery the model is told an
intervention happened and what the operator noted.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..schema import Controller, InterventionRequest, InterventionResolution


class ControlError(RuntimeError):
    """Raised when something acts without holding the token."""


@dataclass
class RunControl:
    """Per-run control state. One instance, shared by the runner and the API."""

    run_id: str
    holder: Controller = Controller.AUTOMATION
    intervention: InterventionRequest | None = None
    resolution: InterventionResolution | None = None
    _resumed: asyncio.Event = field(default_factory=asyncio.Event)

    def assert_automation(self) -> None:
        """Called by the driver before every input event.

        This is the enforcement point. Putting the check in the driver rather than
        in the runner means an escalation path that forgot to yield still cannot
        inject input while a human holds the token.
        """
        raise NotImplementedError

    async def escalate(self, req: InterventionRequest) -> InterventionResolution:
        """Park the run and wait for a human. Returns how they resolved it.

        Awaits an event rather than polling, and never times out on its own — a
        run abandoned mid-intervention is a real state that an operator has to
        clean up, not something to paper over by silently resuming automation on a
        screen nobody looked at.
        """
        raise NotImplementedError

    def take_control(self, operator: str) -> None:
        """Operator claims the session. NOBODY -> HUMAN."""
        raise NotImplementedError

    def release(self, resolution: InterventionResolution) -> None:
        """Operator hands back. HUMAN -> NOBODY -> AUTOMATION, and the run wakes."""
        raise NotImplementedError


class ControlRegistry:
    """All live runs, by id. Single-process, in-memory, deliberately.

    A durable store is the right answer for a real deployment and the wrong answer
    here: the thing being coordinated is a browser process on one machine's X
    display, so a control token that outlives that process would be a lie.
    """

    def __init__(self) -> None:
        self._runs: dict[str, RunControl] = {}

    def get(self, run_id: str) -> RunControl:
        raise NotImplementedError

    def create(self, run_id: str) -> RunControl:
        raise NotImplementedError

    def pending(self) -> list[InterventionRequest]:
        raise NotImplementedError
