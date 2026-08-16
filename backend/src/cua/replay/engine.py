"""Deterministic replay. The production execution path.

This is what an AI agent actually invokes. No model is in the decision loop — the
engine is constructed with `NoLLM` and a resolver that cannot use the VLM tier, so
determinism is a structural property rather than a promise.

Per-step lifecycle, in order:

    ┌─ verify permission ─┐   policy: is this capability allowed to do this, to
    │                     │   this URL, at this risk level?
    ├─ resolve ───────────┤   Target -> coordinate, via the resolver ladder
    ├─ verify target ─────┤   does the region say what the recording said?
    │                     │   is anything stacked on top of it?
    ├─ execute ───────────┤   the primitive
    └─ verify effect ─────┘   did the declared checkpoint become true?

Between resolve and verify-target the engine settles the frame — two consecutive
hash-equal observations — so nothing is resolved against a page that is still
laying out.

Detector evaluation order at each step is deliberate and is where the error
taxonomy is actually enforced:

    1. business outcomes   a declared, legitimate answer ends the run cleanly with
                           BUSINESS_OUTCOME. Checked first, because "no such
                           member" is an answer and must not be reported as a
                           checkpoint failure.
    2. recoverable         a declared app-level condition; apply the handler,
                           re-observe, retry the step, count against max_per_run.
    3. step checkpoint     the expected path.
    4. anything else       hard failure. Unknown states are not guessed at.
"""

from __future__ import annotations

from typing import Any

from ..schema import Capability, ReplayResult


class ReplayEngine:
    def __init__(
        self,
        perceiver: Any,
        driver: Any,
        resolver: Any,
        policy: Any,
        evidence: Any,
        control: Any,
    ) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.resolver = resolver          # constructed with allow_vlm=False
        self.policy = policy
        self.evidence = evidence
        self.control = control            # for escalation / handoff

    async def replay(self, cap: Capability, inputs: dict[str, Any]) -> ReplayResult:
        """Execute a capability with caller-supplied inputs.

        Inputs are validated against the declared `InputSpec` list before anything
        is touched. A type error should be a rejected call, not a run that gets
        four steps in and types "None" into an amount field.
        """
        raise NotImplementedError

    async def _run_step(self, cap: Capability, step: Any, ctx: Any) -> Any:
        raise NotImplementedError

    def _validate_inputs(self, cap: Capability, inputs: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _extract_outputs(self, cap: Capability, ctx: Any) -> dict[str, Any]:
        """Read declared outputs from the recorded extract steps.

        A missing required output is EXTRACTION_FAILED, not a partial success. The
        caller's contract says what it gets back; returning three of four fields
        with no signal is how a downstream agent ends up acting on a null balance.
        """
        raise NotImplementedError
