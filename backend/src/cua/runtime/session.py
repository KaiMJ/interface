"""Session lifecycle — the object that outlives a handoff.

A `Session` owns the display, the browser process, and the perceiver/driver pair over
them; runs borrow it. If a run owned its browser, escalating would mean either tearing
down the session the human is supposed to take over, or keeping a finished run alive
purely to hold a process open.
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

    Terminal for the session, not for a run: every capability assumes it starts signed in, so
    continuing would fail every step for a reason unrelated to the capability.
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

        Deliberately not a recorded capability: it is the one flow where a secret is typed, so
        no artifact ever needs to reference one. The *recipe* — which fields, which button,
        what the signed-out screen says — is per-app configuration in the policy file.

        Idempotent: a session already signed in returns immediately, so this is safe to call
        before every run rather than tracking whether the cookie is still good.
        """
        if not username or self.sign_on is None:
            return
        detector = Checkpoint(
            kind=CheckKind(self.sign_on.detector_kind), value=self.sign_on.detector_value
        )

        obs = await self.observe()
        if not evaluate(detector, obs):
            return

        # The credential is substituted at the moment of typing. The recipe holds
        # `{{password}}` and nothing else ever holds the value.
        secrets = {"username": username, "password": password}
        for step in self.sign_on.steps:
            obs = await self.observe()
            await self._perform(step, obs, secrets)

        obs = await self.observe()
        if evaluate(detector, obs):
            # Deliberately vague: the screen may say "Sign-on failed", and repeating it into
            # an exception that reaches a log is how a bad credential becomes a logged one.
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


class WrongApp(RuntimeError):
    """Asked for one application on a display already holding another."""


class SessionPool:
    """One session per display. Single-display in v1, so: one session.

    A named concept rather than a bare global, so the concurrency constraint is visible in the
    type rather than discovered at runtime.

    A session is also bound to one *application*, because the sign-on recipe and the guardrails
    come from that app's policy. Two applications cannot share a display: the X display is the
    coordinate space, so a second browser on it would make every resolved coordinate ambiguous.
    Asking for the wrong one raises rather than driving app A under app B's allowlist.
    """

    def __init__(self, factory: Callable[[str, str | None], Session] | None = None) -> None:
        self._sessions: dict[str, Session] = {}
        self._apps: dict[str, str | None] = {}
        self._factory = factory
        # Serialized because a session is one browser on one display: two runs sharing it
        # would interleave clicks and look like a flaky application.
        self._lock = asyncio.Lock()

    async def acquire(self, display: str, app: str | None = None) -> Session:
        async with self._lock:
            session = self._sessions.get(display)
            if session is None:
                if self._factory is None:
                    raise RuntimeError("SessionPool was built without a factory")
                session = self._factory(display, app)
                await session.start()
                self._sessions[display] = session
                self._apps[display] = app
                return session
            held = self._apps.get(display)
            if app is not None and held is not None and app != held:
                raise WrongApp(
                    f"display {display} is holding a session for {held!r}; "
                    f"restart it to run {app!r}"
                )
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
