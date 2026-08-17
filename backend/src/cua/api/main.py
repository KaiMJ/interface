"""Control plane.

Small on purpose. It exists to serve three callers:

  - an AI agent invoking a capability          POST /capabilities/{id}/invoke
  - a human operator handling an escalation    /interventions/*
  - the console watching a run                 GET  /runs/{id}/events (SSE)

Routes are the seam where "this system" becomes "a thing an agent can call". The
invoke endpoint takes typed inputs and returns a `ReplayResult` — the same object
the engine produces — so the HTTP contract and the internal contract cannot drift.

Invocations are synchronous: the caller waits for the run. That is the right shape
for one browser on one display, where a queue would only be a place for the second
caller to wait less visibly. It is also the first thing that changes at scale, and
the seam for it is `SessionPool` rather than this file.
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

    async def session(self) -> Any:
        """The live session, started and signed in on first use.

        Lazily, because the control plane must come up and answer /health whether
        or not a browser can start — a container whose health check fails because
        Xvfb was slow is a container that gets restarted forever.
        """
        session = await self.pool.acquire(self.settings.display)
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
