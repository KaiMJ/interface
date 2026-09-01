"""Control transfer.

The human operates the *same live session*, and both parties are pointed at the same X
display — so if both can act at once they race, on a banking screen. Hence one token,
one holder, as explicit state rather than the implied consequence of nobody currently
calling `click()`:

    AUTOMATION ──escalate──► NOBODY ──take_control──► HUMAN
         ▲                                              │
         └──────────── resume ◄──── NOBODY ◄────release ┘

`NOBODY` is the interval between the automation stopping and the operator connecting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..schema import (
    Controller,
    InterventionRequest,
    InterventionResolution,
    InterventionState,
)


class ControlError(RuntimeError):
    """Raised when something acts without holding the token."""


@dataclass
class RunControl:
    """Per-run control state. One instance, shared by the runner and the API."""

    run_id: str
    holder: Controller = Controller.AUTOMATION
    intervention: InterventionRequest | None = None
    resolution: InterventionResolution | None = None
    operator: str = ""
    _resumed: asyncio.Event = field(default_factory=asyncio.Event)

    def assert_automation(self) -> None:
        """The enforcement point, called by the driver before every input event.

        In the driver rather than the runner, so an escalation path that forgot to
        yield still cannot inject input while a human holds the token.
        """
        if self.holder is not Controller.AUTOMATION:
            raise ControlError(
                f"run {self.run_id} tried to act while control is held by "
                f"{self.holder.value}"
            )

    def park(self, req: InterventionRequest) -> None:
        """Surrender control and publish the request. Does not wait.

        Everything an operator can observe has happened when this returns. Split from
        `escalate` so the queue can be exercised without a run behind it.
        """
        # Surrendered *before* publishing, so there is no window in which an
        # operator sees the intervention while the automation may still act.
        self.holder = Controller.NOBODY
        self.intervention = req.model_copy(update={"state": InterventionState.PENDING})
        self.resolution = None
        self._resumed.clear()

    async def escalate(self, req: InterventionRequest) -> InterventionResolution:
        """Park and wait for a human. Never times out: a run abandoned
        mid-intervention is a real state an operator has to clean up, not something
        to paper over by resuming on a screen nobody looked at."""
        self.park(req)
        await self._resumed.wait()

        resolution = self.resolution
        if resolution is None:  # pragma: no cover - release() always sets it
            raise ControlError(f"run {self.run_id} resumed without a resolution")
        return resolution

    def take_control(self, operator: str) -> None:
        """Operator claims the session. NOBODY -> HUMAN."""
        if self.intervention is None:
            raise ControlError(f"run {self.run_id} has no open intervention to take")
        if self.holder is Controller.HUMAN:
            raise ControlError(f"run {self.run_id} is already held by an operator")
        if self.holder is not Controller.NOBODY:
            raise ControlError(
                f"run {self.run_id} is still running; control cannot be taken from "
                f"a run that has not stopped"
            )
        self.holder = Controller.HUMAN
        self.intervention = self.intervention.model_copy(
            update={"state": InterventionState.HUMAN_CONTROL}
        )
        self.operator = operator

    def release(self, resolution: InterventionResolution) -> None:
        """Operator hands back. HUMAN -> NOBODY -> AUTOMATION, and the run wakes."""
        if self.intervention is None:
            raise ControlError(f"run {self.run_id} has no open intervention to release")
        aborted = resolution.outcome == "abort"
        self.resolution = resolution
        self.intervention = self.intervention.model_copy(
            update={
                "state": InterventionState.ABORTED if aborted else InterventionState.RESOLVED
            }
        )
        # NOBODY first, then AUTOMATION: the operator has let go before the run is
        # woken, so the two are never both live even for the length of one await.
        self.holder = Controller.NOBODY
        self.holder = Controller.AUTOMATION
        self._resumed.set()


class ControlRegistry:
    """All live runs, by id. In-memory deliberately: what it coordinates is a browser on this
    machine's display, so a token outliving that process would lie."""

    def __init__(self) -> None:
        self._runs: dict[str, RunControl] = {}

    def get(self, run_id: str) -> RunControl:
        if run_id not in self._runs:
            raise KeyError(f"no live run {run_id}")
        return self._runs[run_id]

    def create(self, run_id: str) -> RunControl:
        control = RunControl(run_id=run_id)
        self._runs[run_id] = control
        return control

    def by_intervention(self, intervention_id: str) -> RunControl:
        """Operators address interventions, not runs. Which run each belongs to is
        our bookkeeping."""
        for control in self._runs.values():
            if control.intervention is not None and control.intervention.id == intervention_id:
                return control
        raise KeyError(f"no open intervention {intervention_id}")

    def pending(self) -> list[InterventionRequest]:
        return [
            c.intervention
            for c in self._runs.values()
            if c.intervention is not None
            and c.intervention.state
            in (InterventionState.PENDING, InterventionState.HUMAN_CONTROL)
        ]

    def all(self) -> list[InterventionRequest]:
        """Every intervention seen, open or closed. `pending()` is the queue; this is the
        record beside it."""
        return [c.intervention for c in self._runs.values() if c.intervention is not None]

    def forget(self, run_id: str) -> None:
        """Dropped when a run finishes. This tracks live control; history is the
        evidence directory."""
        self._runs.pop(run_id, None)
