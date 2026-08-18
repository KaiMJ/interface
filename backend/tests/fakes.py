"""Fakes for the two surfaces the system touches: the screen and the model.

Both are faked at the seam the design already has, which is the point of having
one. A test that needed a browser and an API key would not run in CI, and none of
what these tests assert — that a "no such member" screen is an outcome, that a
discarded step is not recorded — has anything to do with pixels or with tokens.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from cua.discovery.llm import ToolCall, Usage
from cua.schema import Bbox, Element, ElementSource, Observation, Point, Viewport

VIEWPORT = Viewport(width=1440, height=900)
_BLANK: Path | None = None


def blank_png() -> Path:
    """A real image file, because the set-of-marks overlay actually opens it."""
    global _BLANK
    if _BLANK is None:
        path = Path(tempfile.mkdtemp(prefix="cua-fakes-")) / "blank.png"
        Image.new("RGB", (VIEWPORT.width, VIEWPORT.height), (250, 250, 250)).save(path)
        _BLANK = path
    return _BLANK


def el(id_: str, x: float, y: float, w: float, h: float, text: str) -> Element:
    return Element(
        id=id_,
        role="text",
        name=text,
        text=text,
        bbox=Bbox(x=x, y=y, w=w, h=h),
        source=ElementSource.OCR,
        conf=0.95,
    )


def frame(*texts: str, url: str = "http://targetapp:8080/members/12345") -> Observation:
    """A screen, described by the lines on it, laid out top to bottom."""
    return Observation(
        screenshot_path=str(blank_png()),
        viewport=VIEWPORT,
        elements=tuple(
            el(f"e{i}", 0.1, 0.1 + i * 0.05, 0.3, 0.02, t) for i, t in enumerate(texts)
        ),
        url=url,
        frame_hash=f"h{hash(texts) & 0xFFFF:04x}",
        taken_at="2026-08-16T00:00:00+00:00",
    )


def row_frame(*cells: str, url: str = "http://targetapp:8080/members/12345") -> Observation:
    """One horizontal row of cells, for the relational targeting cases."""
    return Observation(
        screenshot_path=str(blank_png()),
        viewport=VIEWPORT,
        elements=tuple(
            el(f"e{i}", 0.05 + i * 0.15, 0.20, 0.12, 0.02, c) for i, c in enumerate(cells)
        ),
        url=url,
        frame_hash=f"h{hash(cells) & 0xFFFF:04x}",
        taken_at="2026-08-16T00:00:00+00:00",
    )


class FakePerceiver:
    """Returns the current scripted frame. Only the driver advances the script."""

    def __init__(self, frames: list[Observation]) -> None:
        self.frames = frames
        self.index = 0
        self.observations = 0

    def settle(self, out_path: Path, timeout_ms: int, poll_ms: int) -> Observation:
        self.observations += 1
        return self._current()

    def _current(self) -> Observation:
        return self.frames[min(self.index, len(self.frames) - 1)]

    def peek(self, out_path: Path) -> str:
        """The cheap "has anything changed" probe — no observation is spent."""
        return self._current().frame_hash or ""


class FakeDriver:
    """Records what it was told to do and advances the scripted screen."""

    def __init__(self, perceiver: FakePerceiver) -> None:
        self.perceiver = perceiver
        self.calls: list[tuple[str, Any]] = []

    def _advance(self) -> None:
        self.perceiver.index += 1

    async def navigate(self, url: str) -> None:
        self.calls.append(("navigate", url))
        self._advance()

    async def reload(self) -> None:
        self.calls.append(("reload", None))
        self._advance()

    async def click(self, p: Point, button: str = "left") -> None:
        self.calls.append(("click", (round(p.x, 3), round(p.y, 3))))
        self._advance()

    async def type_text(self, text: str, secret: bool = False) -> None:
        self.calls.append(("type", "***" if secret else text))

    async def key(self, keys: str) -> None:
        self.calls.append(("key", keys))
        self._advance()

    async def scroll(self, p: Point, dy: float) -> None:
        self.calls.append(("scroll", dy))
        self._advance()

    def current_url(self) -> str | None:
        frames = self.perceiver.frames
        return frames[min(self.perceiver.index, len(frames) - 1)].url


class ScriptedLLM:
    """Plays back a fixed list of tool calls.

    Faked at `LLMClient`'s two-method surface rather than at the HTTP layer,
    because what these tests are about is what the loop does with an answer — not
    how the answer was transported.
    """

    model = "scripted/test"

    def __init__(self, script: list[ToolCall], declaration: dict[str, Any] | None = None) -> None:
        self.script = list(script)
        self.declaration = declaration or {
            "description": "a scripted capability",
            "success_text": "",
            "business_outcomes": [],
        }
        self.calls = 0
        self.usage = Usage()
        self.prompts: list[str] = []

    def preflight(self) -> None:
        return None

    async def decide(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        image_path: Path | None = None,
    ) -> ToolCall:
        self.calls += 1
        self.prompts.append(str(messages[-1].get("content")))
        if not self.script:
            return ToolCall(name="escalate", input={"reason": "script exhausted"})
        return self.script.pop(0)

    async def structured(
        self, system: str, prompt: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls += 1
        self.prompts.append(prompt)
        return self.declaration
