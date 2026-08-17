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

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..action.browser import BrowserDriver
from ..action.offline import OfflineDriver
from ..catalog import Catalog
from ..config import Settings
from ..discovery import DiscoveryLoop, LLMClient
from ..escalation import ControlRegistry
from ..evidence import EvidenceWriter
from ..perception import Perceiver
from ..perception.detect import build_detector
from ..perception.ocr import OnnxTextReader
from ..perception.screen import ImageFileScreen, XDisplayScreen
from ..policy import Policy, Redactor
from ..replay.engine import ReplayEngine
from ..resolve import Resolver
from .session import Session, SessionPool


def build_perceiver(
    settings: Settings,
    url_provider: Callable[[], str | None] | None = None,
) -> Perceiver:
    """The one perceiver, shared by discovery and replay.

    Same perception on both paths on purpose. If replay saw the screen through a
    different pipeline than the recording did, an artifact would be a description
    of what *some other system* saw, and every checkpoint written at discovery
    time would be a guess about replay.

    `settings.detector` selects the backend: `omniparser` for the real thing,
    `ocr_only` for the no-torch path.
    """
    return Perceiver(
        screen=XDisplayScreen(settings.display, settings.viewport),
        detector=build_detector(
            settings.detector,
            settings.models_dir,
            settings.omniparser_repo,
            settings.omniparser_repo_file,
            settings.detect_conf_threshold,
        ),
        reader=OnnxTextReader(
            settings.models_dir,
            conf_threshold=settings.ocr_conf_threshold,
            det_side_len=settings.ocr_det_side_len,
        ),
        merge_iou=settings.merge_iou_threshold,
        url_provider=url_provider,
    )


# One registry per process, because the thing it coordinates is a browser on this
# machine's X display. A durable store would be the right answer for a real
# deployment and the wrong one here: a control token that outlives the process
# holding the session is a token that lies.
REGISTRY = ControlRegistry()


def build_policy(settings: Settings) -> Policy:
    return Policy.load(settings.policy_file)


def build_catalog(settings: Settings) -> Catalog:
    return Catalog(settings.artifacts_dir)


def build_redactor(settings: Settings) -> Redactor:
    return Redactor(patterns=build_policy(settings).redact_patterns, enabled=False)


def build_session(settings: Settings) -> Session:
    """The long-lived browser + display + perceiver triple.

    A run borrows this; it does not own it. That split is what lets an escalation
    hand a live session to a human without either tearing the browser down or
    keeping a finished run alive just to hold a process open.
    """
    driver = BrowserDriver(settings.display, settings.viewport)
    return Session(
        sign_on=build_policy(settings).sign_on,
        id=f"sess-{settings.display.lstrip(':')}",
        display=settings.display,
        driver=driver,
        perceiver=build_perceiver(settings, url_provider=driver.current_url),
        control=None,
        start_url=settings.target_base_url,
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
    )


def build_session_pool(settings: Settings) -> SessionPool:
    return SessionPool(factory=lambda _display: build_session(settings))


def build_replay(settings: Settings, session: Any, run_id: str) -> ReplayEngine:
    """Replay, built so it *cannot* consult a model.

    The resolver is constructed without the VLM tier and no LLM client is passed
    in at all. A test can assert determinism by construction rather than by
    reading the code for the absence of a call.
    """
    control = REGISTRY.create(run_id)
    session.control = control
    session.driver.control = control
    return ReplayEngine(
        perceiver=session.perceiver,
        driver=session.driver,
        resolver=Resolver(allow_vlm=False),
        policy=build_policy(settings),
        evidence=EvidenceWriter(settings.evidence_dir, run_id, build_redactor(settings)),
        control=control,
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
        step_timeout_ms=settings.step_timeout_ms,
        vnc_url=session.vnc_url,
    )


def build_offline_replay(
    settings: Settings, frames: list[Path], run_id: str, url: str | None = None
) -> ReplayEngine:
    """Replay against recorded frames, with no browser and no display.

    Same engine, same resolver, same policy, same evidence — two collaborators
    swapped. That the substitution is this small is the argument that the
    perception and action seams are real rather than described: nothing above them
    knows the difference.
    """
    screen = ImageFileScreen(frames, settings.viewport)
    driver = OfflineDriver(screen, url=url)
    perceiver = Perceiver(
        screen=screen,
        detector=build_detector(
            settings.detector,
            settings.models_dir,
            settings.omniparser_repo,
            settings.omniparser_repo_file,
            settings.detect_conf_threshold,
        ),
        reader=OnnxTextReader(
            settings.models_dir,
            conf_threshold=settings.ocr_conf_threshold,
            det_side_len=settings.ocr_det_side_len,
        ),
        merge_iou=settings.merge_iou_threshold,
        url_provider=driver.current_url,
    )
    return ReplayEngine(
        perceiver=perceiver,
        driver=driver,
        resolver=Resolver(allow_vlm=False),
        policy=build_policy(settings),
        evidence=EvidenceWriter(settings.evidence_dir, run_id, build_redactor(settings)),
        control=REGISTRY.create(run_id),
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
        step_timeout_ms=settings.step_timeout_ms,
    )


def build_discovery(settings: Settings, session: Any, run_id: str) -> DiscoveryLoop:
    """Discovery, built with a real model client.

    The mirror image of `build_replay`: same perceiver, same driver, same policy
    object, same evidence writer — and an LLM. The two paths differ in exactly one
    collaborator, which is the point. A guardrail that only guards one of them is
    not a guardrail.
    """
    control = REGISTRY.create(run_id)
    session.control = control
    session.driver.control = control
    return DiscoveryLoop(
        perceiver=session.perceiver,
        driver=session.driver,
        policy=build_policy(settings),
        evidence=EvidenceWriter(settings.evidence_dir, run_id, build_redactor(settings)),
        llm=LLMClient(
            model=settings.model,
            max_tokens=settings.max_tokens,
            temperature=settings.temperature,
            api_base=settings.api_base,
            fallbacks=settings.fallback_models,
        ),
        max_steps=settings.max_discovery_steps,
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
    )
