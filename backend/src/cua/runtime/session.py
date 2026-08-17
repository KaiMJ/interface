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

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..resolve import Resolver, Unresolvable, evaluate, render
from ..schema import CheckKind, Checkpoint, Observation, Primitive


class AuthenticationFailed(RuntimeError):
    """The session could not reach an authenticated state.

    Terminal for the session, not for a run: every capability assumes it starts
    signed in, so continuing would produce a series of steps that each fail for a
    reason that has nothing to do with the capability.
    """


@dataclass
class Session:
    id: str
    display: str
    driver: Any
    perceiver: Any
    control: Any
    # How this application is signed into, from its policy file. None means the
    # surface has no sign-on step.
    sign_on: Any = None
    start_url: str = ""
    vnc_base_url: str = "http://localhost:6080"
    settle_timeout_ms: int = 8000
    settle_poll_ms: int = 120
    _frames: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="cua-session-")))
    _resolver: Resolver = field(default_factory=lambda: Resolver(allow_vlm=False))

    async def start(self) -> None:
        await self.driver.start(self.start_url or None)

    async def authenticate(self, username: str, password: str) -> None:
        """Log in once, before any capability runs.

        Deliberately not a recorded capability: it is the one flow where a secret
        is typed, and keeping it out of the artifact system means no artifact ever
        needs to reference one. The *recipe* — which fields, which button, what
        the signed-out screen says — is per-app configuration in the policy file,
        so pointing this at a second application is an edit to YAML rather than to
        Python.

        Idempotent. A session that is already signed in returns immediately, which
        is what makes it safe to call before every run rather than tracking
        whether the cookie is still good.
        """
        if not username or self.sign_on is None:
            return
        detector = Checkpoint(
            kind=CheckKind(self.sign_on.detector_kind), value=self.sign_on.detector_value
        )

        obs = await self.observe()
        if not evaluate(detector, obs):
            return

        # The credential is substituted here, at the moment of typing, from values
        # this object was handed. The recipe holds `{{password}}` and nothing else
        # ever holds the value.
        secrets = {"username": username, "password": password}
        for step in self.sign_on.steps:
            obs = await self.observe()
            await self._perform(step, obs, secrets)

        obs = await self.observe()
        if evaluate(detector, obs):
            # Deliberately vague. The screen may say "Sign-on failed"; repeating
            # it into an exception message that ends up in a log is how a bad
            # credential becomes a logged credential.
            raise AuthenticationFailed(
                f"still on the sign-on screen after submitting as {username!r}"
            )

    async def _perform(self, step: Any, obs: Observation, secrets: dict[str, str]) -> None:
        point = None
        if step.target is not None:
            try:
                point = self._resolver.resolve(step.target, obs).point
            except Unresolvable as e:
                raise AuthenticationFailed(f"{step.target.target_desc} not found: {e}") from e
            await self.driver.click(point)
        if step.action is Primitive.TYPE:
            await self.driver.type_text(render(step.value, secrets) or "", secret=True)
        elif step.action is Primitive.KEY:
            await self.driver.key(step.value or "Enter")

    async def observe(self) -> Observation:
        """Session-level perception. Frames go to a scratch directory, not to
        evidence: signing in is not part of any capability's audit trail."""
        return await asyncio.to_thread(
            self.perceiver.settle,
            self._frames / "frame.png",
            self.settle_timeout_ms,
            self.settle_poll_ms,
        )

    async def stop(self) -> None:
        await self.driver.stop()

    @property
    def vnc_url(self) -> str:
        """Where an operator connects to take over this exact session."""
        return f"{self.vnc_base_url}/vnc.html?autoconnect=1&resize=scale"


class SessionPool:
    """One session per display. Single-display in v1, so: one session.

    Present as a named concept rather than a bare global because concurrency is
    the first thing that breaks this design, and the constraint should be visible
    in the type rather than discovered at runtime.
    """

    def __init__(self, factory: Callable[[str], Session] | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._factory = factory
        # Serialized because a session is one browser on one display: two runs
        # sharing it would interleave clicks, and the failure would look like a
        # flaky application rather than like a concurrency bug.
        self._lock = asyncio.Lock()

    async def acquire(self, display: str) -> Session:
        async with self._lock:
            session = self._sessions.get(display)
            if session is None:
                if self._factory is None:
                    raise RuntimeError("SessionPool was built without a factory")
                session = self._factory(display)
                await session.start()
                self._sessions[display] = session
            return session

    def get(self, session_id: str) -> Session:
        for session in self._sessions.values():
            if session.id == session_id:
                return session
        raise KeyError(f"no session {session_id}")

    async def shutdown(self) -> None:
        for session in list(self._sessions.values()):
            await session.stop()
        self._sessions.clear()
