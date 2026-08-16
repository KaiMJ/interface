"""LLM client, via LiteLLM.

Thin on purpose. Two real jobs:

  1. Keep the surface the loop depends on down to two methods, so the model is a
     swappable component rather than an architectural commitment. LiteLLM gives
     one call signature across xAI, Anthropic, OpenAI and a local endpoint, which
     means "which model drives a UI best" stays an empirical question answered by
     changing `CUA_MODEL` — not something baked into the agent loop.

  2. Be the one place a model call can be counted. `LLMClient.calls` is what the
     replay tests assert is zero. "Deterministic replay" is a claim; this is how it
     becomes checkable rather than aspirational.

Requirements on whatever model is configured: vision (the loop's entire input is a
screenshot) and tool calling (its entire output is one structured action). A model
missing either fails on the first turn, which is why `preflight()` exists.

Credentials are never held on this object. LiteLLM reads the provider's env var
directly — an API key that lives in a Python attribute is one that can end up in a
traceback, a settings dump, or a serialized run record.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ToolCall:
    name: str
    input: dict[str, Any]
    text: str = ""          # the model's stated reasoning; recorded as step intent
    raw_id: str | None = None


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0


class LLMClient:
    def __init__(
        self,
        model: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        api_base: str | None = None,
        fallbacks: tuple[str, ...] = (),
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.api_base = api_base
        self.fallbacks = fallbacks
        self.calls = 0
        self.usage = Usage()

    def preflight(self) -> None:
        """Fail fast if the configured model cannot do what the loop needs.

        Checks vision and function-calling support via LiteLLM's capability
        metadata, and that the provider's credential is present. A discovery run
        that dies twenty seconds in because the model cannot see images is a
        confusing failure; this one is not.
        """
        raise NotImplementedError

    async def decide(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        image_path: Path | None = None,
    ) -> ToolCall:
        """One turn. Returns exactly one tool call.

        `tool_choice="required"` — a turn that returns prose is a wasted turn and
        the loop has nothing to do with it. Providers that do not honour it get a
        retry with an explicit instruction rather than a crash.
        """
        raise NotImplementedError

    async def structured(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """One structured completion. Used only by artifact synthesis."""
        raise NotImplementedError


def image_part(path: Path) -> dict[str, Any]:
    """Encode a screenshot as an OpenAI-style image content part.

    LiteLLM translates this to whatever the target provider wants, which is most
    of the reason it is here — the alternative is a per-provider branch in the one
    part of the loop that runs on every single turn.
    """
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


class NoLLM:
    """Raises on any call.

    Injected into the replay engine so a model call on the replay path is a loud
    crash rather than a slow, expensive, silently non-deterministic success.
    """

    calls = 0
    usage = Usage()

    def preflight(self) -> None:
        return None

    async def decide(self, *a: object, **k: object) -> ToolCall:
        raise RuntimeError("replay must not call the LLM")

    async def structured(self, *a: object, **k: object) -> dict[str, Any]:
        raise RuntimeError("replay must not call the LLM")
