"""Run -> capability. The synthesis pass.

Open question in PLAN.md; resolved here as a three-stage pipeline, deterministic
first and model-assisted only where determinism cannot answer.

  1. PRUNE      (deterministic)
     Drop steps that did not advance the state: no-op scrolls, actions whose
     checkpoint failed and were retried, navigations to a page the run immediately
     left. Cheap and safe — these are identifiable from the recorded observations
     without asking anyone.

  2. PARAMETERIZE (deterministic, from known inputs)
     The run was given concrete inputs. Any recorded literal equal to one of them
     becomes `{{that_input}}` — in typed values, in anchor text, in predicates, and
     in checkpoint values. This is the answer to "who decided 12345 was a
     parameter?": nobody did, at synthesis time. The caller declared it when they
     started the run.

     This is deliberately *not* an LLM guessing which numbers look like ids. It is
     exact string matching against declared inputs, which cannot invent a parameter
     and cannot miss one that was actually supplied.

  3. DECLARE    (model-assisted, bounded)
     Outputs, the success checkpoint, and candidate business outcomes need
     judgment: which text on the final screen is "the balance", and what does
     "member not found" look like. The model is asked for these as a single
     structured completion over its own transcript, and every answer is validated
     against the recorded observations — a proposed checkpoint that does not
     actually match the final frame is rejected, not trusted.

Stage 3 output is `status: draft`. A human approves it before unattended replay.
That approval gate is the honest place to put a human, rather than pretending
stage 3 is reliable.
"""

from __future__ import annotations

from typing import Any

from ..schema import Capability


def prune(steps: list[Any], observations: list[Any]) -> list[Any]:
    raise NotImplementedError


def parameterize(steps: list[Any], inputs: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Substitute declared input values with placeholders.

    Returns the rewritten steps and the derived `InputSpec` list. Longest-match
    first, so an input of `123` does not corrupt a recorded `12345` belonging to a
    different input.
    """
    raise NotImplementedError


def declare(state: Any, llm: Any) -> dict[str, Any]:
    """Ask the model for outputs, success condition and business outcomes.

    Bounded: a fixed schema, one call, no tool use, and every proposal is checked
    against recorded frames before it is accepted.
    """
    raise NotImplementedError


def synthesize(state: Any, inputs: dict[str, Any], llm: Any) -> Capability:
    """Full pipeline. Validates the result parses as a `Capability` before returning
    — a synthesis that produces an unloadable artifact is a failed run, not a
    successful one with a bad file."""
    raise NotImplementedError
