"""Composition root.

The only module that knows how the pieces fit together. Everything else takes its
collaborators as constructor arguments, which is what makes the seams testable
rather than merely described.

Two builders, and the difference between them is the point:

    build_discovery()   real LLM client, resolver with allow_vlm=True
    build_replay()      NoLLM,           resolver with allow_vlm=False

The replay engine is not *asked* to avoid the model. It is handed collaborators
that raise if it tries. Determinism is a construction-time property, and a test
can assert it by checking `llm.calls == 0` after a replay.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings


def build_perceiver(settings: Settings) -> Any:
    raise NotImplementedError


def build_session(settings: Settings) -> Any:
    raise NotImplementedError


def build_discovery(settings: Settings, session: Any, run_id: str) -> Any:
    raise NotImplementedError


def build_replay(settings: Settings, session: Any, run_id: str) -> Any:
    raise NotImplementedError
