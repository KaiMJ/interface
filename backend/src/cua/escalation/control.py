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

Resume re-observes rather than trusting the frame it parked on, because the human
may have advanced the app several screens or gone back. In replay, a run parked
*before* acting (a risky step awaiting confirmation) performs the step; one parked
*after* a failure (`on_error: escalate`) treats the step as satisfied by the human
and continues. Neither searches forward for a step whose checkpoint already holds —
it does not have to, because the next step asserts its own screen and checkpoint
and so fails loudly rather than clicking blind. In discovery the model is told an
intervention happened and what the operator noted.
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
        """Called by the driver before every input event.

        This is the enforcement point. Putting the check in the driver rather than
        in the runner means an escalation path that forgot to yield still cannot
        inject input while a human holds the token.
        """
        if self.holder is not Controller.AUTOMATION:
            raise ControlError(
                f"run {self.run_id} tried to act while control is held by "
                f"{self.holder.value}"
            )

    def park(self, req: InterventionRequest) -> None:
        """Surrender control and publish the request. Does not wait.

        Split from `escalate` because it is the whole of the state transition —
        everything an operator can observe has already happened when this returns.
        The waiting is a separate concern, and keeping them separate means the
        queue can be exercised without a background task pretending to be a run.
        """
        # Control is surrendered *before* the request is published, so there is no
        # window in which an operator can see the intervention and start clicking
        # while the automation still believes it may act.
        self.holder = Controller.NOBODY
        self.intervention = req.model_copy(update={"state": InterventionState.PENDING})
        self.resolution = None
        self._resumed.clear()

    async def escalate(self, req: InterventionRequest) -> InterventionResolution:
        """Park the run and wait for a human. Returns how they resolved it.

        Awaits an event rather than polling, and never times out on its own — a
        run abandoned mid-intervention is a real state that an operator has to
        clean up, not something to paper over by silently resuming automation on a
        screen nobody looked at.
        """
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
    """All live runs, by id. Single-process, in-memory, deliberately.

    A durable store is the right answer for a real deployment and the wrong answer
    here: the thing being coordinated is a browser process on one machine's X
    display, so a control token that outlives that process would be a lie.
    """

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
        """Operators address interventions, not runs — the console shows a queue
        of things to do, and which run each belongs to is our bookkeeping."""
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
        """Every intervention this process has seen, open or closed.

        The operator queue is `pending()`; this is the record beside it. An
        operator who resumed a run five minutes ago and wants to check what they
        did should not have to go looking in an evidence directory for it.
        """
        return [c.intervention for c in self._runs.values() if c.intervention is not None]

    def forget(self, run_id: str) -> None:
        """Runs are dropped when they finish. The registry tracks live control,
        not history — history is what the evidence directory is for."""
        self._runs.pop(run_id, None)
