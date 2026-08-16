"""Control plane.

Small on purpose. It exists to serve three callers:

  - an AI agent invoking a capability          POST /capabilities/{id}/invoke
  - a human operator handling an escalation    /interventions/*
  - the console watching a run                 GET  /runs/{id}/events (SSE)

Routes are the seam where "this system" becomes "a thing an agent can call". The
invoke endpoint takes typed inputs and returns a `ReplayResult` — the same object
the engine produces — so the HTTP contract and the internal contract cannot drift.
"""

from __future__ import annotations

from fastapi import FastAPI

from ..config import settings
from . import routes


def create_app() -> FastAPI:
    cfg = settings()
    app = FastAPI(
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
    return app


app = create_app()
