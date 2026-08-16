"""Verification — the three checks that wrap every action.

A resolved coordinate being *stable* does not make it *right*. An unexpected modal
moves nothing; it lands on top. The recorded coordinate is still correct in the
sense that nothing shifted, and the click hits the dialog. In a banking
application that is a correctness and safety failure, not a robustness one, and no
amount of better targeting detects it. Verification does.

So each step is: resolve, verify target, execute, verify effect.

  verify_target   before acting: does the region I am about to click actually say
                  what the recording said it said, and is anything on top of it?
                  Nearly free — the observation already exists.
  verify_effect   after acting: did the state actually change the way the step's
                  checkpoint declares? Per-step, not just at the end, so a wrong
                  click at step 3 fails at step 3 with a legible diff rather than
                  producing a confident wrong output at step 9.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..schema import Checkpoint, FailureKind, Observation, Target
from .resolver import Resolution


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    kind: FailureKind | None = None
    expected: str | None = None
    observed: str | None = None
    detail: str = ""


def verify_target(
    target: Target,
    resolution: Resolution,
    obs: Observation,
    params: dict[str, object] | None = None,
) -> VerifyResult:
    """Pre-action assertion.

    Two distinct checks with two distinct failure kinds, because they call for
    different operator responses:

      TARGET_MISMATCH     the region resolved, but its text does not match the
                          recorded label. Either we resolved the wrong thing or the
                          app changed. Re-record.
      UNEXPECTED_OVERLAY  something is stacked on top of the target. If the policy
                          declares a dismissal handler this is recoverable; if not,
                          it is a hard stop. Never click through one.
    """
    raise NotImplementedError


def verify_effect(
    checkpoint: Checkpoint,
    obs: Observation,
    params: dict[str, object] | None = None,
) -> VerifyResult:
    """Post-action assertion against the step's declared checkpoint."""
    raise NotImplementedError


def evaluate(
    checkpoint: Checkpoint,
    obs: Observation,
    params: dict[str, object] | None = None,
) -> bool:
    """Evaluate one checkpoint against one observation. No waiting, no retries.

    Kept separate from `verify_effect` because business-outcome detectors and
    recoverable-condition detectors are the same shape and are evaluated against
    the same frame — the difference between them is what the caller does with a
    True, not how it is computed.
    """
    raise NotImplementedError
