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
import json
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
        import litellm

        missing = litellm.validate_environment(self.model).get("missing_keys") or []
        if missing:
            raise ModelUnusable(f"{self.model} needs {', '.join(missing)} in the environment")

        # Capability metadata is best-effort: LiteLLM does not know every model,
        # and a model it has not heard of is not necessarily unusable. An unknown
        # model is allowed through and fails on the first turn with the provider's
        # own error, which is more informative than ours would be.
        for name, supported in (
            ("vision", litellm.supports_vision),
            ("tool calling", litellm.supports_function_calling),
        ):
            try:
                if supported(model=self.model) is False:
                    raise ModelUnusable(
                        f"{self.model} does not support {name}; the loop is screenshots "
                        f"in and one tool call out, so it cannot drive a UI"
                    )
            except ModelUnusable:
                raise
            except Exception:  # noqa: BLE001 - unknown model, not a broken one
                continue

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
        turn = list(messages)
        if image_path is not None:
            turn = _with_image(turn, image_path)

        for attempt in range(2):
            response = await self._complete(
                [{"role": "system", "content": system}, *turn],
                tools=tools,
                tool_choice="required",
            )
            choice = response.choices[0].message
            calls = getattr(choice, "tool_calls", None) or []
            if calls:
                call = calls[0]
                return ToolCall(
                    name=call.function.name,
                    input=_json(call.function.arguments),
                    text=(choice.content or "").strip(),
                    raw_id=getattr(call, "id", None),
                )
            # Prose instead of a tool call. Say so and try once more rather than
            # crashing a run that has already done nine steps of real work.
            turn = [
                *turn,
                {"role": "assistant", "content": choice.content or ""},
                {
                    "role": "user",
                    "content": (
                        "That reply contained no tool call. Reply with exactly one "
                        "tool call and no prose."
                    ),
                },
            ]
            if attempt == 1:
                raise ModelUnusable(f"{self.model} returned no tool call twice in a row")
        raise ModelUnusable("unreachable")

    async def structured(self, system: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        """One structured completion. Used only by artifact synthesis.

        Expressed as a single forced tool call rather than as a JSON response
        format, because tool calling is already a hard requirement of this design
        while structured-output support varies by provider. One capability
        requirement, not two.
        """
        tool = {
            "type": "function",
            "function": {"name": "declare", "description": "Return the requested fields.",
                         "parameters": schema},
        }
        response = await self._complete(
            [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            tools=[tool],
            tool_choice={"type": "function", "function": {"name": "declare"}},
        )
        calls = getattr(response.choices[0].message, "tool_calls", None) or []
        if not calls:
            raise ModelUnusable("synthesis returned no structured answer")
        return _json(calls[0].function.arguments)

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: Any,
    ) -> Any:
        import litellm

        self.calls += 1
        response = await litellm.acompletion(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            api_base=self.api_base,
            fallbacks=list(self.fallbacks) or None,
            drop_params=True,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.usage.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.usage.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)
        cost = getattr(response, "_hidden_params", {}).get("response_cost")
        self.usage.cost_usd += float(cost or 0.0)
        return response


class ModelUnusable(RuntimeError):
    """The configured model cannot do what discovery requires."""


def _json(arguments: Any) -> dict[str, Any]:
    """Tool arguments, however the provider chose to send them.

    Some return a JSON string, some a dict, and a model under load occasionally
    returns a string that is nearly JSON. A malformed argument set is a bad turn,
    not a crashed run, so it comes back empty and the loop reports it.
    """
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _with_image(messages: list[dict[str, Any]], image_path: Path) -> list[dict[str, Any]]:
    """Attach the frame to the last user message.

    Only the current frame is ever sent. Earlier screenshots are represented by
    the text history instead: a ten-step run would otherwise carry ten megabytes
    of base64 into every subsequent turn, and models attend worse, not better,
    with a pile of near-identical images to compare.
    """
    head, last = messages[:-1], dict(messages[-1])
    content = last.get("content")
    parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    last["content"] = [*parts, image_part(image_path)]
    return [*head, last]


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
