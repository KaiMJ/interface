"""Prompts for the discovery agent.

Kept in one file and out of the loop so they are reviewable as text, and so a
prompt change is a visible diff rather than a line buried in control flow.

Shape of each turn:
  system  : role, the surface contract, the action space, the rules
  user    : the goal, the declared inputs, and the current annotated screenshot
            plus its candidate list
  assistant: exactly one tool call

Design notes that belong with the prompt rather than in a doc:

  - The model is told it is choosing from an enumerated candidate list, never
    producing coordinates. Coordinates from a model are the failure mode this
    whole design avoids.
  - It is told which values are parameters up front, so it types `{{member_id}}`'s
    value knowing it is a parameter. Synthesis still does the substitution
    deterministically; telling the model improves the intent text it writes.
  - It is instructed to prefer `find_and_act` over manual scrolling, with the
    reason given. Models follow rules better when the rule has a stated cause.
  - It is told that `escalate` is a legitimate, non-penalized answer. Without
    that, a stuck model does something plausible and wrong.
  - Page text is untrusted input. The system prompt says so explicitly. This is a
    mitigation, not a defense — see the prompt-injection limit in REPORT §6.
"""

from __future__ import annotations

SYSTEM = """\
TODO
"""

GOAL_TURN = """\
TODO
"""

SYNTHESIS = """\
TODO
"""


def system_prompt(allowed_actions: frozenset[str], surface: str) -> str:
    raise NotImplementedError


def goal_turn(goal: str, inputs: dict[str, object], candidates: list[dict[str, object]]) -> str:
    raise NotImplementedError
