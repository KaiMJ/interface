"""Session lifecycle — the object that outlives a handoff.

A `Session` owns the X display, the browser process, and the perceiver/driver
pair built over them. Runs borrow it; they do not own it.

That ownership split is the whole reason this file exists separately from the
runners. If a run owned its browser, escalating would mean either tearing the
browser down (losing the session the human is supposed to take over) or leaving a
run object alive purely to hold a process open. Instead the session is
long-lived, and a run is a bounded activity that borrows it, yields it to a human,
and takes it back.

Login is a precondition, not a step. Credentials are supplied from configuration
at session start and never appear in an artifact, a prompt, or a log. Capabilities
therefore start from an authenticated state, which is also how these systems work
in production — an automation account, not a stolen operator session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Session:
    id: str
    display: str
    driver: Any
    perceiver: Any
    control: Any

    async def start(self) -> None:
        raise NotImplementedError

    async def authenticate(self, username: str, password: str) -> None:
        """Log in once, before any capability runs.

        Deliberately not a recorded capability: it is the one flow where a secret
        is typed, and keeping it out of the artifact system means no artifact ever
        needs to reference one.
        """
        raise NotImplementedError

    async def stop(self) -> None:
        raise NotImplementedError

    @property
    def vnc_url(self) -> str:
        """Where an operator connects to take over this exact session."""
        raise NotImplementedError


class SessionPool:
    """One session per display. Single-display in v1, so: one session.

    Present as a named concept rather than a bare global because concurrency is
    the first thing that breaks this design, and the constraint should be visible
    in the type rather than discovered at runtime.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    async def acquire(self, display: str) -> Session:
        raise NotImplementedError

    def get(self, session_id: str) -> Session:
        raise NotImplementedError
