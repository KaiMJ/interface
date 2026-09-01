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

    A dataclass rather than module-level globals, so a test can build one with fakes and the
    browser session's lifetime is visible in one place.
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
    # What each of those runs is, from the moment it is accepted — a run writes no evidence
    # while it is starting a browser and signing in.
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Which run currently holds the display, if any. One browser on one X display means two
    # concurrent runs would fight over the same pixels, so a second is refused rather than
    # queued.
    active_run: str | None = None
    # Whether the thing holding the session is a *run*. Arming a fault claims the display the
    # same way but has no steps to show, so it must not be reported as a run.
    active_is_run: bool = True
    # Which demo faults the console last armed. Their real state lives in a cookie inside the
    # automation's browser, so this is what we set rather than what is set.
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

        Lazily, so the control plane comes up and answers /health whether or not a browser can
        start.

        `app` selects the sign-on recipe and guardrails. The pool refuses to hand back a
        session opened for a different application (`WrongApp`): one display is one coordinate
        space.
        """
        session = await self.pool.acquire(self.settings.display, app or self.settings.default_app)
        await session.authenticate(
            self.settings.target_username, self.settings.target_password
        )
        return session


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """The browser outlives every run but not the process.

    A run borrows the session; only the process that owns it tears it down.
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

    # The console is a separate origin (:3000 to :8000) and the only browser client. Wide open
    # is acceptable for a localhost demo and must not survive into a deployment.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


app = create_app()
