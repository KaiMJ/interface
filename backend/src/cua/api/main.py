"""Control plane.

Small on purpose, with three callers: an AI agent invoking a capability, a human
operator handling an escalation, and the console watching a run over SSE.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import Settings, settings
from . import routes


class RunInProgress(RuntimeError):
    """A run is already driving the session. Surfaces as 409, never as a queue."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run {run_id} is using the session; wait for it to finish or resolve "
            f"its intervention"
        )
        self.run_id = run_id


@dataclass
class Runtime:
    """Everything the routes share. One instance, held on `app.state`.

    A dataclass rather than module-level globals so a test can build one with
    fakes, and so the lifetime of the browser session is visible in one place.
    """

    settings: Settings
    catalog: Any
    registry: Any
    redactor: Any
    pool: Any
    runs: dict[str, Any] = field(default_factory=dict)
    watchers: dict[str, Any] = field(default_factory=dict)
    # Background runs the console started, so it can start one without holding an
    # HTTP request open for the length of a discovery run.
    tasks: dict[str, Any] = field(default_factory=dict)
    # What each of those runs is, from the moment it is accepted. A run spends its
    # first seconds starting a browser and signing in, and writes no evidence
    # until then — without this the console answers 404 for a run the operator
    # just started and watched appear in the header.
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Which run currently holds the display, if any. One browser on one X display
    # means two concurrent runs would fight over the same pixels — and an operator
    # console makes starting a second one a single mis-click. Refusing is the only
    # honest answer; queueing here would be scaling plumbing the brief explicitly
    # does not reward.
    active_run: str | None = None
    # Whether the thing holding the session is a *run*. Arming a fault claims the
    # display the same way, and for the same reason, but it is not something the
    # console can open and show steps for — reporting it as a run made the console
    # chase a run id that will never exist.
    active_is_run: bool = True
    # Which demo faults the console last armed. Their real state lives in a cookie
    # inside the automation's browser and is not readable from here, so this is
    # what we set rather than what is set — honest, and the only thing that can be
    # said without driving the session to go and look.
    armed_faults: list[str] = field(default_factory=list)

    def begin(self, run_id: str, is_run: bool = True) -> None:
        if self.active_run is not None:
            raise RunInProgress(self.active_run)
        self.active_run = run_id
        self.active_is_run = is_run

    def end(self, run_id: str) -> None:
        if self.active_run == run_id:
            self.active_run = None
            self.active_is_run = True

    async def session(self, app: str | None = None) -> Any:
        """The live session for one application, started and signed in on first use.

        Lazily, because the control plane must come up and answer /health whether
        or not a browser can start — a container whose health check fails because
        Xvfb was slow is a container that gets restarted forever.

        `app` selects the sign-on recipe and guardrails. The pool refuses to hand
        back a session opened for a different application (`WrongApp`), because
        one display is one coordinate space.
        """
        session = await self.pool.acquire(self.settings.display, app or self.settings.default_app)
        await session.authenticate(
            self.settings.target_username, self.settings.target_password
        )
        return session


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """The browser outlives every run but not the process.

    Closing it here rather than per-run is the same ownership rule the session
    itself encodes: a run borrows the session, and only the process that owns it
    tears it down.
    """
    yield
    pool = getattr(app.state.runtime, "pool", None)
    if pool is not None:
        await pool.shutdown()


def create_app() -> FastAPI:
    cfg = settings()
    app = FastAPI(
        lifespan=lifespan,
        title="Computer-Use Automation",
        version="0.1.0",
        description=(
            "An LLM discovers how to complete a goal by driving a UI; the run is "
            "recorded as a typed capability artifact; the artifact replays "
            "deterministically with no model in the loop."
        ),
    )
    app.include_router(routes.router)
    app.state.settings = cfg

    from ..runtime import REGISTRY, build_catalog, build_redactor, build_session_pool

    app.state.runtime = Runtime(
        settings=cfg,
        catalog=build_catalog(cfg),
        registry=REGISTRY,
        redactor=build_redactor(cfg),
        pool=build_session_pool(cfg),
    )

    # The console is a separate origin (:3000 to :8000) and is the only browser
    # client. Wide open is acceptable for a localhost demo and is exactly the kind
    # of thing that must not survive into a deployment.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


app = create_app()
