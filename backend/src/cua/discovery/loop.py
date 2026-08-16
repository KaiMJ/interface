"""The discovery loop: observe -> decide -> act -> record.

Runs until the goal's success condition is met or a stopping condition fires
(max steps, timeout, policy denial, dead end, or the model asking to escalate).

The recording is a side effect of acting, not a separate pass. Every accepted
tool-call is appended as a typed step at the moment it is executed, together with
the observation it was resolved against. That means the transcript and the
artifact cannot disagree, and it means a run that fails at step 9 still leaves
eight verified steps behind for a human to inspect.

Termination is bounded on three axes independently — steps, wall clock, and LLM
calls — because they fail differently. A model stuck in a two-action cycle burns
steps; a model waiting on a hung page burns wall clock; a model retrying a
malformed tool call burns neither but costs money.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..schema import DiscoveryResult, Observation, StepResult


@dataclass
class DiscoveryState:
    """Everything the loop accumulates. Serialized to evidence on every step, so a
    crashed run is still inspectable."""

    run_id: str
    goal: str
    steps: list[Any] = field(default_factory=list)          # typed artifact steps
    results: list[StepResult] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    llm_calls: int = 0
    seen_frames: set[str] = field(default_factory=set)      # loop detection


class DiscoveryLoop:
    def __init__(self, perceiver: Any, driver: Any, policy: Any, evidence: Any, llm: Any) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.policy = policy
        self.evidence = evidence
        self.llm = llm

    async def run(self, goal: str, start_url: str, inputs: dict[str, Any]) -> DiscoveryResult:
        """Drive the surface until the goal is met.

        `inputs` are the concrete values used for this run (member id, amount).
        They matter beyond just being typed into fields: they are what the
        synthesis pass looks for when deciding which literals in the recording are
        parameters. See `synthesize.py`.
        """
        raise NotImplementedError

    async def _step(self, state: DiscoveryState, obs: Observation) -> bool:
        """One observe-decide-act cycle. Returns False when the run should stop."""
        raise NotImplementedError

    def _is_stuck(self, state: DiscoveryState) -> str | None:
        """Detect a dead end before the step budget runs out.

        Signals, cheapest first: the frame hash has repeated N times (the actions
        are having no effect); the same tool-call has been issued twice in a row
        with the same arguments; resolution has failed on consecutive steps.

        Detecting this early matters because the alternative is an escalation that
        arrives at max-steps with twenty near-identical screenshots attached — the
        operator has to work out what went wrong, instead of being told.
        """
        raise NotImplementedError
