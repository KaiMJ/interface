"""Composition root.

The only module that knows how the pieces fit. Everything else takes its collaborators as
constructor arguments.

    build_discovery()   a real LLM client; the model picks from enumerated marks
    build_replay()      no LLM client at all, and a resolver built with allow_vlm=False

Replay is not *asked* to avoid the model; there is nothing on it to call, which is a property a
test can assert — see `test_replay_resolver_cannot_reach_a_model_by_construction`. The VLM tier
is the same idea one level down, off on both paths (`resolve.resolver.Resolver._by_vlm`).
"""

from __future__ import annotations

from collections.abc import Callable
from functools import cache
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
from ..perception.ocr import RapidOcrTextReader
from ..perception.screen import ImageFileScreen, XDisplayScreen
from ..policy import Policy, Redactor
from ..replay.engine import ReplayEngine
from ..resolve import Resolver
from .session import Session, SessionPool


def build_perceiver(
    settings: Settings,
    url_provider: Callable[[], str | None] | None = None,
    volatile: tuple[str, ...] = (),
) -> Perceiver:
    """The one perceiver, shared by discovery and replay.

    If replay saw the screen through a different pipeline than the recording did, every
    checkpoint written at discovery time would be a guess about replay.

    `settings.detector` selects the backend: `omniparser` for the real thing, `ocr_only` for
    the no-torch path.
    """
    return Perceiver(
        screen=XDisplayScreen(settings.display, settings.viewport),
        detector=build_detector(
            settings.detector,
            settings.omniparser_repo,
            settings.omniparser_repo_file,
            settings.detect_conf_threshold,
        ),
        reader=RapidOcrTextReader(
            conf_threshold=settings.ocr_conf_threshold,
            det_side_len=settings.ocr_det_side_len,
            engine=settings.ocr_engine,
        ),
        merge_iou=settings.merge_iou_threshold,
        url_provider=url_provider,
        volatile=volatile,
    )


# One registry per process, because what it coordinates is a browser on this machine's X
# display: a control token that outlives the process holding the session is a token that lies.
# A durable store is the right answer for a real deployment.
REGISTRY = ControlRegistry()


@cache
def _load_policy(path: Path) -> Policy:
    return Policy.load(path)


def build_policy(settings: Settings, app: str | None = None) -> Policy:
    """The guardrails for one application, selected by name.

    Cached by path, so a mid-run edit cannot give the discovery loop and the driver different
    allowlists.
    """
    return _load_policy(settings.policy_path(app))


def entry_url(settings: Settings, policy: Policy) -> str:
    """Where a run against this app starts.

    Most specific first: this deployment's override, then what the app's policy declares. The
    override is how one policy file serves two institutions running the same vendor product at
    different addresses (REPORT §4).
    """
    url = settings.target_base_url or policy.entry_url
    if not url:
        raise ValueError(
            f"no entry url for app {policy.app!r}: set `entry_url` in its policy "
            "or CUA_TARGET_BASE_URL"
        )
    return url


def build_catalog(settings: Settings) -> Catalog:
    return Catalog(settings.artifacts_dir)


def build_redactor(settings: Settings, app: str | None = None) -> Redactor:
    """Declared-sensitive redaction, always on. Pattern masking off — a decision, not a gap.

    `redact_mapping` keeps a credential out of a serialized result and runs unconditionally.
    `enabled` governs only the pattern tier, off because the declared patterns are tuned for a
    real deployment: the PAN pattern admits spaces and hyphens between digits, so against a
    joined OCR string of a transaction table it masks runs of unrelated numbers and takes the
    evidence with them. Turning it on needs a policy author who has measured their patterns.
    """
    return Redactor(patterns=build_policy(settings, app).redact_patterns, enabled=False)


def build_session(settings: Settings, app: str | None = None) -> Session:
    """The long-lived browser + display + perceiver triple.

    A run borrows this; it does not own it. That split is what lets an escalation hand a live
    session to a human without tearing down the browser they are taking over.
    """
    policy = build_policy(settings, app)
    driver = BrowserDriver(settings.display, settings.viewport)
    return Session(
        sign_on=policy.sign_on,
        id=f"sess-{settings.display.lstrip(':')}",
        display=settings.display,
        driver=driver,
        perceiver=build_perceiver(
            settings,
            url_provider=driver.current_url,
            # Which lines on this application tick on their own. Read from policy here, so
            # perception stays a thing that knows only about pixels.
            volatile=policy.volatile_text,
        ),
        control=None,
        start_url=entry_url(settings, policy),
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
    )


def build_session_pool(settings: Settings) -> SessionPool:
    """Sessions built on demand, for whichever app the caller asks for."""
    return SessionPool(factory=lambda _display, app: build_session(settings, app))


def build_replay(
    settings: Settings,
    session: Any,
    run_id: str,
    app: str | None = None,
    require_approved: bool = False,
) -> ReplayEngine:
    """Replay, built so it *cannot* consult a model.

    The resolver is constructed without the VLM tier and no LLM client is passed in at all, so
    a test can assert determinism by construction. `require_approved` is a constructor argument
    for the same reason `allow_vlm` is: which engine you were handed decides what you may run.
    An operator at the console or CLI may replay a draft; the agent-facing invoke route may not.
    """
    control = REGISTRY.create(run_id)
    session.control = control
    session.driver.control = control
    policy = build_policy(settings, app)
    return ReplayEngine(
        perceiver=session.perceiver,
        driver=session.driver,
        resolver=Resolver(allow_vlm=False),
        policy=policy,
        evidence=EvidenceWriter(settings.evidence_dir, run_id, build_redactor(settings, app)),
        control=control,
        require_approved=require_approved,
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
        step_timeout_ms=settings.step_timeout_ms,
        vnc_url=session.vnc_url,
        # Which institution's install this run acts on; the recorded URLs name the one the
        # capability was recorded at.
        entry_url=entry_url(settings, policy),
        # A closure over the session's own sign-on, not the credentials, so the engine can
        # re-authenticate without holding a secret it could serialize.
        sign_on=lambda: session.authenticate(
            settings.target_username, settings.target_password
        ),
    )


def build_offline_replay(
    settings: Settings,
    frames: list[Path],
    run_id: str,
    url: str | None = None,
    app: str | None = None,
) -> ReplayEngine:
    """Replay against recorded frames, with no browser and no display.

    Same engine, resolver, policy and evidence writer, with two collaborators swapped; nothing
    above the perception and action seams knows the difference.
    """
    screen = ImageFileScreen(frames, settings.viewport)
    driver = OfflineDriver(screen, url=url)
    perceiver = Perceiver(
        screen=screen,
        detector=build_detector(
            settings.detector,
            settings.omniparser_repo,
            settings.omniparser_repo_file,
            settings.detect_conf_threshold,
        ),
        reader=RapidOcrTextReader(
            conf_threshold=settings.ocr_conf_threshold,
            det_side_len=settings.ocr_det_side_len,
            engine=settings.ocr_engine,
        ),
        merge_iou=settings.merge_iou_threshold,
        url_provider=driver.current_url,
        volatile=build_policy(settings, app).volatile_text,
    )
    return ReplayEngine(
        perceiver=perceiver,
        driver=driver,
        resolver=Resolver(allow_vlm=False),
        policy=build_policy(settings, app),
        evidence=EvidenceWriter(settings.evidence_dir, run_id, build_redactor(settings, app)),
        control=REGISTRY.create(run_id),
        settle_timeout_ms=settings.settle_timeout_ms,
        settle_poll_ms=settings.settle_poll_ms,
        step_timeout_ms=settings.step_timeout_ms,
    )


def build_discovery(
    settings: Settings, session: Any, run_id: str, app: str | None = None
) -> DiscoveryLoop:
    """Discovery, built with a real model client.

    The mirror image of `build_replay`: same perceiver, driver, policy object and evidence
    writer, plus an LLM. The two paths differ in exactly one collaborator, so a guardrail
    cannot bind only one of them.
    """
    control = REGISTRY.create(run_id)
    session.control = control
    session.driver.control = control
    return DiscoveryLoop(
        perceiver=session.perceiver,
        driver=session.driver,
        policy=build_policy(settings, app),
        # Discovery parks on the same token replay does. A risky action is risky
        # whoever is driving; see `DiscoveryLoop._intervene`.
        control=control,
        vnc_url=session.vnc_url,
        evidence=EvidenceWriter(settings.evidence_dir, run_id, build_redactor(settings, app)),
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
