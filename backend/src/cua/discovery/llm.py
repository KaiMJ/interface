"""LLM client, via LiteLLM.

Thin on purpose: two methods, so the model stays swappable by changing `CUA_MODEL`.
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
    # The chain of thought, when the model emits one. Distinct from `text`: under
    # `tool_choice="required"` a reasoning model leaves `content` empty and puts everything
    # here.
    reasoning: str = ""
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

        Checks vision and function-calling support via LiteLLM's capability metadata, and that
        the provider's credential is present.
        """
        import litellm

        missing = litellm.validate_environment(self.model).get("missing_keys") or []
        if missing:
            raise ModelUnusable(f"{self.model} needs {', '.join(missing)} in the environment")

        # Best-effort: LiteLLM does not know every model. An unknown one is let through and
        # fails on the first turn with the provider's own error.
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

        `tool_choice="required"`, since the loop has nothing to do with prose. Providers that
        do not honour it get a retry with an explicit instruction rather than a crash.
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
                    reasoning=_reasoning(choice),
                    raw_id=getattr(call, "id", None),
                )
            # Prose instead of a tool call. Say so and try once more rather than crashing a
            # run that has already done real work.
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

        A single forced tool call rather than a JSON response format: tool calling is already a
        hard requirement, while structured-output support varies by provider.
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


def _reasoning(choice: Any) -> str:
    """The model's chain of thought, from wherever the provider put it.

    Two shapes in the wild: a flat `reasoning_content` string, or `thinking_blocks` carrying
    the same thing in parts. Both are read and the first that yields text wins, rather than
    branching per model. Redacted blocks carry no readable text and are skipped.
    """
    flat = str(getattr(choice, "reasoning_content", "") or "").strip()
    if flat:
        return flat
    blocks = getattr(choice, "thinking_blocks", None) or ()
    parts = [
        str(b.get("thinking", "")).strip()
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "thinking"
    ]
    return "\n\n".join(p for p in parts if p)


def _json(arguments: Any) -> dict[str, Any]:
    """Tool arguments, however the provider chose to send them.

    Some return a JSON string, some a dict, and a model under load occasionally returns a
    string that is nearly JSON. A malformed argument set is a bad turn, not a crashed run.
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

    Only the current frame is ever sent; earlier screenshots are represented by the text
    history, so a ten-step run does not carry ten megabytes of base64 into every later turn.
    """
    head, last = messages[:-1], dict(messages[-1])
    content = last.get("content")
    parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
    last["content"] = [*parts, image_part(image_path)]
    return [*head, last]


def image_part(path: Path) -> dict[str, Any]:
    """Encode a screenshot as an OpenAI-style image content part.

    LiteLLM translates this to whatever the target provider wants, so the per-turn path has no
    per-provider branch.
    """
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


class NoLLM:
    """Raises on any call.

    Injected into the replay engine, so a model call on the replay path is a loud crash rather
    than a silently non-deterministic success.
    """

    calls = 0
    usage = Usage()

    def preflight(self) -> None:
        return None

    async def decide(self, *a: object, **k: object) -> ToolCall:
        raise RuntimeError("replay must not call the LLM")

    async def structured(self, *a: object, **k: object) -> dict[str, Any]:
        raise RuntimeError("replay must not call the LLM")
