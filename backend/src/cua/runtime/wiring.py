"""Composition root.

The only module that knows how the pieces fit. Everything else takes its collaborators
as constructor arguments, which is what makes the seams testable rather than described.

    build_discovery()   real LLM client, resolver with allow_vlm=True
    build_replay()      NoLLM,           resolver with allow_vlm=False

Replay is not *asked* to avoid the model; it is handed collaborators that raise if it
tries. A test asserts `llm.calls == 0`.
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
from ..perception.ocr import OnnxTextReader
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
            engine=settings.ocr_engine,
        ),
        merge_iou=settings.merge_iou_threshold,
        url_provider=url_provider,
        volatile=volatile,
    )


# One registry per process, because the thing it coordinates is a browser on this
# machine's X display. A durable store would be the right answer for a real
# deployment and the wrong one here: a control token that outlives the process
# holding the session is a token that lies.
REGISTRY = ControlRegistry()


@cache
def _load_policy(path: Path) -> Policy:
    return Policy.load(path)


def build_policy(settings: Settings, app: str | None = None) -> Policy:
    """The guardrails for one application, selected by name.

    Cached by path: every builder below wants the same policy object for the same
    run, and re-parsing the file per collaborator would let a mid-run edit give
    the discovery loop and the driver different allowlists.
    """
    return _load_policy(settings.policy_path(app))


def entry_url(settings: Settings, policy: Policy) -> str:
    """Where a run against this app starts.

    Resolution order, most specific first: this deployment's override, then what
    the app's policy declares. The override is how one policy file serves two
    institutions running the same vendor product at different addresses — which is
    the whole of the multi-tenant story that is actually built (REPORT §4).
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
    return Redactor(patterns=build_policy(settings, app).redact_patterns, enabled=False)


def build_session(settings: Settings, app: str | None = None) -> Session:
    """The long-lived browser + display + perceiver triple.

    A run borrows this; it does not own it. That split is what lets an escalation
    hand a live session to a human without either tearing the browser down or
    keeping a finished run alive just to hold a process open.
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
            # Which lines on this application tick on their own. Read from policy
            # here rather than inside the perceiver, so perception stays a thing
            # that knows about pixels and the application stays a YAML file.
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
    a test can assert determinism by construction rather than by reading the code for the
    absence of a call. `require_approved` is a constructor argument for the same reason
    `allow_vlm` is: which engine you were handed decides what you may run. An operator at the
    console or CLI is a reviewer at work and may replay a draft; the agent-facing invoke route
    may not, because the caller on that path is a machine.
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
        # Which institution's install this run acts on. The recorded URLs name the
        # one the capability was recorded at, which is a fact about the past.
        entry_url=entry_url(settings, policy),
        # A closure over the session's own sign-on, not the credentials. The
        # engine can put the session back into an authenticated state without
        # holding a secret it could serialize — and a run that dies mid-flow on a
        # read-only capability re-runs itself instead of waking someone.
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
