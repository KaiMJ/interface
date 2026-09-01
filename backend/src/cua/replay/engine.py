"""Deterministic capability replay."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..clock import monotonic_ms, now_iso
from ..perception import Unsettled, cell_in_column, column_span
from ..policy import PolicyDenied
from ..resolve import (
    Resolution,
    Unresolvable,
    evaluate,
    placeholders,
    region_text,
    render,
    verify_effect,
    verify_target,
)
from ..schema import (
    ActStep,
    Bbox,
    Capability,
    Element,
    Evidence,
    FailureDetail,
    FailureKind,
    FindAndActStep,
    InterventionReason,
    InterventionRequest,
    MultiplePolicy,
    Observation,
    OnError,
    OutcomeDetail,
    Phases,
    Point,
    PolicyDecision,
    Primitive,
    ReplayResult,
    ResolutionTier,
    ResolutionTrace,
    Risk,
    RunStatus,
    StepResult,
    StepStatus,
    Target,
)
from .contract import ContractError, extract_outputs, validate_inputs
from .outcomes import (
    Classification,
    Classified,
    UndeclaredOutcome,
    classify,
    conditions,
    effective_outcomes,
)
from .scan import Scanner, Untestable
from .tenant import rebase

# A recovery may justify one safe re-execution if its checkpoint still does not hold.
_RECOVERY_RETRIES = 1


class _Terminal(Exception):
    """Base class for non-successful terminal run states."""


class _Business(_Terminal):
    def __init__(self, outcome: OutcomeDetail) -> None:
        super().__init__(outcome.name)
        self.outcome = outcome


class _Failed(_Terminal):
    def __init__(self, failure: FailureDetail) -> None:
        super().__init__(failure.message)
        self.failure = failure


class _Restart(_Terminal):
    """Restart a read-only flow after re-authentication."""

    def __init__(self, condition: str) -> None:
        super().__init__(condition)
        self.condition = condition


class _Escalated(_Terminal):
    def __init__(self, intervention_id: str, note: str = "") -> None:
        super().__init__(intervention_id)
        self.intervention_id = intervention_id
        self.note = note


@dataclass
class RunContext:
    params: dict[str, Any]
    sensitive: frozenset[str]
    obs: Observation | None = None
    evidence: Evidence = field(default_factory=Evidence)
    extracted: dict[int, Any] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    recovery_counts: dict[str, int] = field(default_factory=dict)
    frames: int = 0
    # Step-local state survives terminal exceptions for evidence recording.
    policy_decision: PolicyDecision | None = None
    resolution_trace: ResolutionTrace | None = None
    recovery_applied: str | None = None
    # Recovery grants at most one additional attempt.
    attempt: int = 1
    recovered_this_attempt: bool = False
    escalations: int = 0
    notes: list[str] = field(default_factory=list)
    # Risky work prevents automatic restart after session expiry.
    mutated: bool = False
    restarts: int = 0
    # Geometry is checked once, on the first frame — the earliest it is knowable.
    viewport_checked: bool = False
    # Timing accumulated into the step record.
    observe_ms: float = 0.0
    observations: int = 0
    resolve_ms: float = 0.0
    act_ms: float = 0.0
    verify_ms: float = 0.0
    # Reuse the prior settled observation when no action changed the screen.
    fresh: Observation | None = None


class ReplayEngine:
    def __init__(
        self,
        perceiver: Any,
        driver: Any,
        resolver: Any,
        policy: Any,
        evidence: Any,
        control: Any,
        settle_timeout_ms: int = 8000,
        settle_poll_ms: int = 120,
        step_timeout_ms: int = 15000,
        vnc_url: str = "",
        require_approved: bool = False,
        entry_url: str = "",
        sign_on: Any = None,
    ) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.resolver = resolver
        self.policy = policy
        self.evidence = evidence
        self.control = control
        self.settle_timeout_ms = settle_timeout_ms
        self.settle_poll_ms = settle_poll_ms
        self.step_timeout_ms = step_timeout_ms
        self.vnc_url = vnc_url
        # Rebase recorded navigations onto this deployment's entry URL.
        self.entry_url = entry_url
        # A sign-on closure keeps credentials outside this serializable engine.
        self.sign_on = sign_on
        # Interactive replay may review a draft; unattended invoke cannot.
        self.require_approved = require_approved
        self.scanner = Scanner(perceiver, driver, resolver)
        # Settle polling uses scratch frames, not reviewer-facing evidence.
        self._scratch = Path(tempfile.mkdtemp(prefix="cua-frames-"))

    # -----------------------------------------------------------------------
    # entry point
    # -----------------------------------------------------------------------

    async def replay(self, cap: Capability, inputs: dict[str, Any]) -> ReplayResult:
        """Execute a capability with caller-supplied inputs, validated against the declared
        `InputSpec` list before anything is touched."""
        run_id = self.evidence.run_id
        started = now_iso()
        clock = monotonic_ms()
        sensitive = frozenset(i.name for i in cap.inputs if i.sensitive)

        result = ReplayResult(
            run_id=run_id,
            capability=cap.ref,
            app=cap.app.name,
            # Persisted before each step; terminal status is set below.
            status=RunStatus.RUNNING,
            inputs={},
            started_at=started,
            evidence_dir=str(self.evidence.open()),
        )

        try:
            params = validate_inputs(cap, inputs, self.require_approved)
            # Validate inherited outcome detectors before touching the app.
            effective_outcomes(cap, self.policy)
        except UndeclaredOutcome as e:
            result.status = RunStatus.FAILURE
            result.failure = FailureDetail(kind=FailureKind.INTERNAL, message=str(e))
            return self._finish(result, started, clock)
        except ContractError as e:
            result.status = RunStatus.FAILURE
            result.failure = e.failure
            return self._finish(result, started, clock)

        ctx = RunContext(params=params, sensitive=sensitive)
        # What the caller sent, minus what they declared sensitive. Redaction runs before the
        # first write, not the last.
        result.inputs = dict(self.evidence.redactor.redact_mapping(params, sensitive))
        self.evidence.result(result)

        try:
            while True:
                try:
                    for step in cap.steps:
                        await self._run_step(cap, step, ctx)
                        result.steps = ctx.steps
                        self.evidence.result(result)
                    break
                except _Restart as e:
                    # The session is good but the flow lost its place, so the capability starts
                    # over. Steps already taken stay in the log.
                    if ctx.restarts >= self.policy.max_restarts:
                        raise _Failed(
                            FailureDetail(
                                kind=FailureKind.APP_ERROR,
                                message=(
                                    f"{e.condition} happened again after re-authenticating; "
                                    f"the session is not staying up"
                                ),
                            )
                        ) from e
                    ctx.restarts += 1
                    ctx.notes = []
                    result.steps = ctx.steps
                    self.evidence.result(result)

            await self._verify_success(cap, ctx)
            result.outputs = extract_outputs(cap, ctx.extracted)
            result.status = RunStatus.SUCCESS

        except ContractError as e:
            result.status = RunStatus.FAILURE
            result.failure = e.failure
        except _Business as e:
            # Not a failure. The caller asked a question and this is the answer.
            result.status = RunStatus.BUSINESS_OUTCOME
            result.outcome = e.outcome
        except _Escalated as e:
            result.status = RunStatus.ESCALATED
            result.intervention_id = e.intervention_id
        except _Failed as e:
            result.status = RunStatus.FAILURE
            result.failure = e.failure
        except Exception as e:  # noqa: BLE001 - last resort; the run must still report
            result.status = RunStatus.FAILURE
            result.failure = FailureDetail(
                kind=FailureKind.INTERNAL,
                message=f"{type(e).__name__}: {e}",
            )

        result.steps = ctx.steps
        return self._finish(result, started, clock)

    def _finish(self, result: ReplayResult, started: str, clock: float) -> ReplayResult:
        result.started_at = started
        result.finished_at = now_iso()
        result.duration_ms = int(monotonic_ms() - clock)
        self.evidence.result(result)
        return result

    # -----------------------------------------------------------------------
    # steps
    # -----------------------------------------------------------------------

    async def _run_step(self, cap: Capability, step: Any, ctx: RunContext) -> StepResult:
        """Run one step and record it, however it ends.

        Recorded here rather than in the caller so a step that terminates the run still appears
        in the step log.
        """
        began = monotonic_ms()
        intent = _intent(step, ctx.params)
        ctx.policy_decision = None
        ctx.resolution_trace = None
        ctx.recovery_applied = None
        ctx.attempt = 1
        ctx.recovered_this_attempt = False
        ctx.escalations = 0
        ctx.notes = []
        ctx.observe_ms = ctx.resolve_ms = ctx.act_ms = ctx.verify_ms = 0.0
        ctx.observations = 0
        # A step's frames are its own; carried over, the previous step's after-frame would
        # attach here.
        ctx.evidence = Evidence()
        try:
            result = await self._attempt(cap, step, ctx, began, intent)
        except _Terminal as e:
            ctx.steps.append(_terminal_step(step, intent, began, ctx, e))
            self.evidence.step(ctx.steps[-1])
            raise
        ctx.steps.append(result)
        self.evidence.step(result)
        return result

    async def _attempt(
        self, cap: Capability, step: Any, ctx: RunContext, began: float, intent: str
    ) -> StepResult:
        """Execute the step, re-executing only when that is both allowed and safe.

        Everything deciding whether there is another attempt lives here rather than in the loop
        body: the failure mode of that rule is a duplicate transfer.
        """
        while True:
            resolution, result_obs, verdict = await self._once(cap, step, ctx, intent)

            if verdict.kind is Classification.APP_ERROR:
                # Two distinct grants: a recovery is the app's operator declaring the
                # condition transient, `on_error: retry` is the recording declaring the step
                # repeatable. Either justifies another attempt; neither implies the other.
                again = self._retry_reason(step, ctx)
                if again is None:
                    raise _Failed(
                        FailureDetail(
                            kind=FailureKind.APP_ERROR,
                            step_id=getattr(step, "id", None),
                            message=f"the application reported an error: {verdict.name}",
                            observed=verdict.observed,
                        )
                    )
                ctx.attempt += 1
                ctx.recovered_this_attempt = False
                ctx.notes.append(f"attempt {ctx.attempt} after {verdict.name}: {again}")
                continue

            if verdict.kind is not Classification.FAILURE:
                break

            again = self._retry_reason(step, ctx)
            if again is None:
                await self._fail_or_escalate(
                    cap,
                    step,
                    ctx,
                    kind=FailureKind.CHECKPOINT_FAILED,
                    message=f"checkpoint did not hold after {intent}",
                    expected=verdict.expected,
                    observed=verdict.observed,
                )
                # Reached only when `on_error: escalate` and a human resolved it, which
                # counts the step as satisfied.
                break

            ctx.attempt += 1
            ctx.recovered_this_attempt = False
            ctx.notes.append(f"attempt {ctx.attempt}: {again}")

        return StepResult(
            step_id=step.id,
            # A recovered step succeeded, but not the same way; the status is a drift signal.
            status=StepStatus.RECOVERED if ctx.recovery_applied else StepStatus.OK,
            intent=intent,
            resolution=resolution.tier if resolution else ResolutionTier.NONE,
            settled_by=result_obs.settled_by,
            duration_ms=int(monotonic_ms() - began),
            attempts=ctx.attempt,
            note="; ".join(ctx.notes) or None,
            expected=_expected_of(step, ctx.params),
            observed=(str(ctx.extracted.get(step.id))[:200] if step.id in ctx.extracted else None),
            recovery_applied=ctx.recovery_applied,
            policy=ctx.policy_decision,
            resolution_trace=ctx.resolution_trace,
            phases=_phases(ctx),
            evidence=ctx.evidence,
        )

    async def _once(
        self, cap: Capability, step: Any, ctx: RunContext, intent: str
    ) -> tuple[Resolution | None, Observation, Classified]:
        """One execution of the step: clear, check, resolve, act, verify."""

        # The frame this step acts on; an interstitial is caught before it swallows the click.
        obs = await self._clear_the_way(cap, step, ctx)
        self._check_viewport(cap, ctx, obs)

        # Policy classifies the *declared intent*, never the step's value: a navigate to
        # `/transfer/review` is not a transfer. Checked before any waiting, so a denied step
        # does not spend a timeout finding out.
        disposition = self._check_policy(cap, step, _declared_intent(step, ctx.params), ctx)
        if disposition == "confirm":
            await self._escalate(
                cap,
                step,
                ctx,
                reason=InterventionReason.RISKY_ACTION_CONFIRMATION,
                message=f"risky action needs confirmation: {intent}",
            )

        obs = await self._wait_until_ready(cap, step, ctx, obs)

        if isinstance(step, FindAndActStep):
            resolution = await self._run_find_and_act(cap, step, ctx)
        else:
            resolution = await self._run_act(cap, step, ctx, obs)

        # From here the run has done something it cannot take back, which is what stops a
        # later expiry being recoverable. Policy's verdict, so a promoted step counts too.
        decided = ctx.policy_decision
        if decided is not None and decided.effective_risk != Risk.SAFE.value:
            ctx.mutated = True

        # Polled to the step's own timeout: "not true yet" and "not true" look the same.
        result_obs, verdict = await self._await_effect(cap, step, ctx)
        return resolution, result_obs, verdict

    async def _wait_until_ready(
        self, cap: Capability, step: Any, ctx: RunContext, obs: Observation
    ) -> Observation:
        """Poll until the screen this step needs is in front of us.

        No risk gate: everything polled here is a read. Retrying *after* an action is the
        direction that doubles a transfer, and is gated in `_retry_reason`.
        """
        deadline = monotonic_ms() + _timeout_of(step, self.step_timeout_ms)
        began = monotonic_ms()
        polls = 0

        while True:
            try:
                # A flow that went elsewhere fails naming where it actually is, rather than
                # "the target was not found".
                self._check_screen(cap, step, obs)
                if isinstance(step, ActStep) and step.target is not None:
                    # Resolved only to find out whether it *can* be; no side effects.
                    self._resolve(cap, step, ctx, obs)
            except _Failed as e:
                # A screen positively recognised as a *different* declared one is not
                # transient; a page we cannot name may still be mid-navigation.
                if e.failure.kind is FailureKind.WRONG_SCREEN and e.failure.observed in {
                    s.name for s in cap.screens
                }:
                    raise
                if monotonic_ms() >= deadline:
                    raise
                polls += 1
                # Not a bare observe: an interstitial arriving *while* we wait would hold the
                # target for the rest of the deadline.
                obs = await self._clear_the_way(cap, step, ctx)
                continue

            if polls:
                waited = int(monotonic_ms() - began)
                ctx.notes.append(f"waited {waited}ms for this step's screen to arrive")
            return obs

    def _retry_reason(self, step: Any, ctx: RunContext) -> str | None:
        """Why this step gets another execution, or None if it does not.

        Two grants, one gate. The gate is `risk`: a safe step may run twice, a risky one may
        not, at any budget. Policy's verdict rather than the artifact's field, so a step
        promoted from its intent is excluded too.
        """
        decision = ctx.policy_decision
        effective = decision.effective_risk if decision is not None else step.risk.value
        if effective != Risk.SAFE.value or step.risk is not Risk.SAFE:
            return None

        # Granted by the engine: a recovery fired and the checkpoint still did not hold, the
        # signature of an action the interstitial ate.
        if ctx.recovered_this_attempt and ctx.attempt <= _RECOVERY_RETRIES:
            return f"re-running after the {ctx.recovery_applied!r} recovery cleared"

        # Granted by the artifact.
        if step.on_error is OnError.RETRY and ctx.attempt <= step.retries:
            return f"on_error: retry, attempt {ctx.attempt + 1} of {step.retries + 1}"

        return None

    async def _clear_the_way(
        self, cap: Capability, step: Any, ctx: RunContext
    ) -> Observation:
        """Observe, and handle any declared condition already on the screen.

        Before the step acts, not after: an interstitial up when the click is issued absorbs it,
        while the recorded coordinate still resolves to the right place underneath.

        **Only recoverable conditions** — obstruction, not state. A session expiry or an app
        error *is* a different page, already named by the screen assertion and the previous
        step's classification, and a business outcome is an answer a step produces.

        The first frame is usually free: the previous step settled on one and nothing has acted
        since.
        """
        reused = ctx.fresh
        ctx.fresh = None
        if reused is not None:
            # Its after-frame becomes this step's acted-on frame: the same pixels under both
            # names.
            obs = self._adopt(reused, step, ctx)
        else:
            obs = await self._observe(cap, step, ctx)
        # Bounded by `max_per_run`, which `conditions` enforces; this is the backstop for a
        # policy file declaring an absurd cap.
        for _ in range(8):
            verdict = conditions(obs, self.policy, ctx.recovery_counts)

            if verdict is not None and verdict.kind is Classification.RECOVERABLE and verdict.name:
                await self._recover(cap, step, verdict.name, ctx)
                ctx.notes.append(f"cleared {verdict.name!r} before acting")
                obs = await self._observe(cap, step, ctx)
                continue

            if verdict is not None and verdict.kind is Classification.FAILURE:
                # Past its cap with the condition still up: the handler is what stopped
                # working, not the checkpoint, and the condition was declared rather than
                # unexpected.
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.RECOVERY_EXHAUSTED,
                        step_id=getattr(step, "id", None),
                        message=f"{verdict.name} did not clear",
                        expected=verdict.expected,
                        observed=verdict.observed,
                    )
                )

            return obs

        raise _Failed(
            FailureDetail(
                kind=FailureKind.UNEXPECTED_OVERLAY,
                step_id=getattr(step, "id", None),
                message="the screen never cleared to a state this step could act on",
            )
        )

    async def _run_act(
        self, cap: Capability, step: ActStep, ctx: RunContext, obs: Observation
    ) -> Resolution | None:
        """One primitive against at most one resolved target."""
        value = render(step.value, ctx.params)

        if step.action is Primitive.NAVIGATE:
            if not value:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.INTERNAL,
                        step_id=step.id,
                        message="navigate step has no url",
                    )
                )
            # The artifact supplies the path, the deployment the origin. Checked against the
            # allowlist *after* rebasing, so what is permitted is what is navigated to.
            value, note = rebase(value, self.entry_url)
            if note:
                ctx.notes.append(note)
            self._check_url(step, value)
            await self._act_on_surface(ctx, lambda: self.driver.navigate(value))
            return None

        if step.action in (Primitive.WAIT, Primitive.ASSERT):
            # Pure checkpoint steps: the effect verification that follows does the waiting,
            # polling to the declared timeout.
            return None

        if step.action is Primitive.KEY:
            await self._act_on_surface(ctx, lambda: self.driver.key(value or "Enter"))
            return None

        resolution = self._resolve(cap, step, ctx, obs)
        await self._check_ambiguity(cap, step, ctx, resolution)

        if step.action is Primitive.EXTRACT:
            text = region_text(obs, resolution.bbox)
            if not text:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.EXTRACTION_FAILED,
                        step_id=step.id,
                        message=f"nothing readable at the target for {step.extract_as!r}",
                        expected=render(step.target.target_desc, ctx.params)
                        if step.target
                        else None,
                    )
                )
            ctx.extracted[step.id] = text
            return resolution

        if step.action is Primitive.SCROLL:
            amount = float(value or "0.8")
            await self._act_on_surface(
                ctx, lambda: self.driver.scroll(resolution.point, amount)
            )
            return resolution

        if step.action is Primitive.CLICK:
            await self._act_on_surface(ctx, lambda: self.driver.click(resolution.point))
            await self._check_url_after(step)
            return resolution

        if step.action is Primitive.TYPE:
            # Focus first: typing into whatever had focus is how a password ends up in a
            # search box.
            await self._act_on_surface(ctx, lambda: self.driver.click(resolution.point))
            secret = bool(placeholders(step.value) & ctx.sensitive)
            await self._act_on_surface(
                ctx, lambda: self.driver.type_text(value or "", secret=secret)
            )
            return resolution

        raise _Failed(
            FailureDetail(
                kind=FailureKind.INTERNAL,
                step_id=step.id,
                message=f"unsupported primitive {step.action}",
            )
        )

    async def _run_find_and_act(
        self, cap: Capability, step: FindAndActStep, ctx: RunContext
    ) -> Resolution | None:
        """Data-dependent targeting: find the row, then act on it."""

        async def observe() -> Observation:
            return await self._observe(cap, step, ctx)

        try:
            found = await self.scanner.scan(step, ctx.params, observe)
        except Untestable as e:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.SCAN_INCONCLUSIVE,
                    step_id=step.id,
                    message=str(e),
                )
            ) from e
        except Unresolvable as e:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.RESOLUTION_EXHAUSTED,
                    step_id=step.id,
                    message=f"scan scope not found: {e}",
                )
            ) from e

        if found.inconclusive:
            # Out of budget while the list was still moving, so "not found" is a guess.
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.SCAN_INCONCLUSIVE,
                    step_id=step.id,
                    message=(
                        f"stopped after {found.advances} advances with the region still "
                        f"changing; cannot distinguish absent from not-yet-seen"
                    ),
                    expected=", ".join(_terms(step, ctx.params)),
                )
            )

        if not found.matches:
            if step.on_not_found_outcome:
                raise _Business(
                    OutcomeDetail(
                        name=step.on_not_found_outcome,
                        step_id=step.id,
                        fields=dict(ctx.params),
                    )
                )
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.RESOLUTION_EXHAUSTED,
                    step_id=step.id,
                    message="scanned the whole list and found no matching record",
                    expected=", ".join(_terms(step, ctx.params)),
                )
            )

        if len(found.matches) > 1 and not step.collect_all:
            if step.on_multiple is MultiplePolicy.ESCALATE:
                await self._escalate(
                    cap,
                    step,
                    ctx,
                    reason=InterventionReason.AMBIGUOUS_MATCH,
                    message=(
                        f"{len(found.matches)} records match "
                        f"{', '.join(_terms(step, ctx.params))}"
                    ),
                )
            elif step.on_multiple is MultiplePolicy.FAIL:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.AMBIGUOUS_MATCH,
                        step_id=step.id,
                        message=f"{len(found.matches)} records match; policy is fail",
                        expected=", ".join(_terms(step, ctx.params)),
                    )
                )

        if step.collect_all or step.on_found_action is Primitive.EXTRACT:
            column = render(step.on_found_extract_column, ctx.params)
            values = [_read_row(row, column, found.observation, step) for row in found.matches]
            ctx.extracted[step.id] = values if step.collect_all else values[0]
            return None

        row = _row_bbox(found.matches[0])
        point = Point(
            x=min(1.0, row.x + step.on_found_offset[0] * row.w),
            y=min(1.0, row.y + step.on_found_offset[1] * row.h),
        )
        await self._act_on_surface(ctx, lambda: self.driver.click(point))
        await self._check_url_after(step)
        return Resolution(point=point, bbox=row, tier=ResolutionTier.ANCHOR_TEXT)

    # -----------------------------------------------------------------------
    # perception, resolution, verification
    # -----------------------------------------------------------------------

    async def _observe(
        self, cap: Capability, step: Any, ctx: RunContext, after: bool = False
    ) -> Observation:
        """Settle and record the frame as evidence.

        Perception only. Classification belongs to the caller — `_clear_the_way` before a step,
        `_await_effect` after it — because the two ask different questions of the same picture,
        and folding either in here is how the taxonomy ends up decided in two places.

        CPU-bound (OCR, a detector forward pass), so it runs in a thread: the control plane has
        to keep answering an operator while a run is in flight, and a parked run has to wake.
        """
        ctx.frames += 1
        live = self._scratch / f"frame-{ctx.frames:04d}.png"
        began = monotonic_ms()
        try:
            obs: Observation = await asyncio.to_thread(
                self.perceiver.settle, live, self.settle_timeout_ms, self.settle_poll_ms
            )
        except Unsettled as e:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.TIMEOUT,
                    step_id=getattr(step, "id", None),
                    message=str(e),
                )
            ) from e
        ctx.observe_ms += monotonic_ms() - began
        ctx.observations += 1
        return self._adopt(obs, step, ctx, after=after)

    async def _act_on_surface(self, ctx: RunContext, action: Any) -> Any:
        """Run one driver primitive: timed, and it drops the reusable frame.

        Every path to the surface goes through here, which is what makes the frame reuse in
        `_clear_the_way` sound.
        """
        ctx.fresh = None
        began = monotonic_ms()
        try:
            return await action()
        finally:
            ctx.act_ms += monotonic_ms() - began

    def _adopt(
        self, obs: Observation, step: Any, ctx: RunContext, after: bool = False
    ) -> Observation:
        """Make an observation the current one and write it to evidence.

        The frame a step acted on is the one its target resolved against; the frame it produced
        is the one its checkpoint was judged on. They are separate slots.
        """
        ctx.obs = obs
        ctx.evidence = self.evidence.frame(
            obs, getattr(step, "id", 0), after=after, prior=ctx.evidence
        )
        return obs

    async def _unchanged(self, ctx: RunContext) -> bool:
        """Is the screen byte-identical to the observation we already have?

        A screen grab and a hash. When the answer is yes the verdict on that frame is the one
        already computed, so there is nothing to re-interpret.
        """
        if ctx.obs is None or ctx.obs.frame_hash is None:
            return False
        peek = self._scratch / "peek.png"
        current: str = await asyncio.to_thread(self.perceiver.peek, peek)
        return current == ctx.obs.frame_hash

    async def _await_effect(
        self, cap: Capability, step: Any, ctx: RunContext
    ) -> tuple[Observation, Classified]:
        """Poll the step's checkpoint until it holds or its timeout expires.

        A checkpoint that has not become true yet looks exactly like one that never will, so
        the only honest answer is to keep looking to the declared deadline. An unchanged screen
        is not re-interpreted.
        """
        deadline = monotonic_ms() + _timeout_of(step, self.step_timeout_ms)
        entered = monotonic_ms()
        observing = ctx.observe_ms
        polled = False

        while True:
            if polled and await self._unchanged(ctx):
                if monotonic_ms() >= deadline:
                    # Out of time on a screen that never moved. One last real read, so the
                    # verdict comes from an interpreted frame rather than a hash comparison.
                    pass
                else:
                    await asyncio.sleep(self.settle_poll_ms / 1000.0)
                    continue
            polled = True
            obs = await self._observe(cap, step, ctx, after=True)
            verdict = classify(
                obs, cap, step, self.policy, ctx.params, ctx.recovery_counts
            )

            if verdict.kind is Classification.BUSINESS_OUTCOME:
                raise _Business(
                    OutcomeDetail(
                        name=verdict.name or "unnamed",
                        step_id=getattr(step, "id", None),
                        fields=verdict.fields or {},
                    )
                )

            if verdict.kind is Classification.RECOVERABLE and verdict.name:
                await self._recover(cap, step, verdict.name, ctx)
                continue

            if verdict.kind is Classification.APP_ERROR:
                # Returned, not raised: whether the step is worth running again is
                # `_attempt`'s question. Polling stops either way — an app error is not
                # "not yet".
                ctx.verify_ms += monotonic_ms() - entered - (ctx.observe_ms - observing)
                return obs, verdict

            if verdict.kind is Classification.ESCALATE:
                if ctx.escalations >= self.policy.max_escalations_per_step:
                    # Handed back with the condition still on screen; asking again would park
                    # the run on the same intervention forever.
                    raise _Failed(
                        FailureDetail(
                            kind=FailureKind.APP_ERROR,
                            step_id=getattr(step, "id", None),
                            message=(
                                f"{verdict.name} was still present after "
                                f"{ctx.escalations} interventions"
                            ),
                            observed=verdict.observed,
                        )
                    )
                await self._escalate(
                    cap,
                    step,
                    ctx,
                    reason=InterventionReason.APP_CONDITION,
                    message=f"declared app condition: {verdict.name}",
                    observed=verdict.observed,
                )
                continue

            if verdict.kind is Classification.OK or monotonic_ms() >= deadline:
                ctx.verify_ms += monotonic_ms() - entered - (ctx.observe_ms - observing)
                # The frame this step ended on; nothing acts before the next step's first
                # look. See `_clear_the_way`.
                ctx.fresh = obs
                return obs, verdict

    def _resolve(
        self, cap: Capability, step: ActStep, ctx: RunContext, obs: Observation
    ) -> Resolution:
        if step.target is None:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.INTERNAL,
                    step_id=step.id,
                    message=f"{step.action.value} step has no target",
                )
            )
        began = monotonic_ms()
        try:
            found: tuple[Resolution, ResolutionTrace] = self.resolver.resolve_traced(
                step.target, obs, ctx.params
            )
            resolution, ctx.resolution_trace = found
        except Unresolvable as e:
            ctx.resolve_ms += monotonic_ms() - began
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.RESOLUTION_EXHAUSTED,
                    step_id=step.id,
                    message=str(e),
                    expected=render(step.target.target_desc, ctx.params),
                )
            ) from e

        check = verify_target(step.target, resolution, obs, ctx.params)
        if not check.ok:
            raise _Failed(
                FailureDetail(
                    kind=check.kind or FailureKind.TARGET_MISMATCH,
                    step_id=step.id,
                    message=check.detail,
                    expected=check.expected,
                    observed=check.observed,
                    # Where it is, not just what it said: the console draws this over the
                    # step's frame.
                    region=check.region,
                )
            )
        # Booked either way; a step that spent its time failing to resolve still reports it.
        ctx.resolve_ms += monotonic_ms() - began
        return resolution

    def _check_viewport(self, cap: Capability, ctx: RunContext, obs: Observation) -> None:
        """Is this deployment's display the shape the capability was recorded on?

        Coordinates are normalized, so a proportional scale change is only a note. A different
        aspect ratio reflows the app and makes the recorded-bbox tier precise and wrong, so it
        stops the run. Checked once: the answer cannot change mid-run.
        """
        if ctx.viewport_checked:
            return
        ctx.viewport_checked = True
        if cap.recording is None:
            # A hand-written capability makes no claim about its geometry.
            return
        recorded, current = cap.recording.viewport, obs.viewport
        if (recorded.width, recorded.height) == (current.width, current.height):
            return
        was = f"{recorded.width}x{recorded.height}"
        now = f"{current.width}x{current.height}"
        if abs(recorded.width / recorded.height - current.width / current.height) > 0.01:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.INTERNAL,
                    message=(
                        f"{cap.ref} was recorded at {was} and this deployment's "
                        f"display is {now}; the application lays out differently at "
                        f"a different shape, so its recorded positions no longer "
                        f"mean what they meant"
                    ),
                    expected=was,
                    observed=now,
                )
            )
        ctx.notes.append(f"recorded at {was}, replaying at {now} — same shape, scaled")

    async def _check_ambiguity(
        self, cap: Capability, step: ActStep, ctx: RunContext, resolution: Resolution
    ) -> None:
        """An anchor that matched more than one element, on a step that mutates.

        `_pick` takes the match nearest the recorded position, which is a guess. On a read that
        is a note, since a wrong answer fails its own checkpoint; on a write three rows reading
        "View" are three different members. Gated on policy's *effective* risk, so a promoted
        step is covered too.
        """
        if not resolution.ambiguous:
            return
        decision = ctx.policy_decision
        effective = decision.effective_risk if decision is not None else step.risk.value
        if effective == Risk.SAFE.value:
            ctx.notes.append(
                f"{resolution.candidates} elements matched the anchor and more than "
                f"one was a real candidate; took the one nearest the recorded position"
            )
            return
        await self._escalate(
            cap,
            step,
            ctx,
            reason=InterventionReason.AMBIGUOUS_MATCH,
            message=(
                f"more than one element matches the anchor for a {effective} step; "
                f"which one is a person's call"
            ),
            expected=step.target.anchor_text if step.target else None,
            observed=resolution.matched_text,
        )

    async def _verify_success(self, cap: Capability, ctx: RunContext) -> None:
        """The capability's own final assertion. Separate from the last step's checkpoint,
        because a flow can execute every step correctly and still not achieve what it was
        recorded to achieve."""
        obs = ctx.obs
        if obs is None:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.INTERNAL, message="no observation to verify success against"
                )
            )
        verdict = verify_effect(cap.success, obs, ctx.params)
        if not verdict.ok:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.CHECKPOINT_FAILED,
                    message="the capability's success condition does not hold",
                    expected=verdict.expected,
                    observed=verdict.observed,
                )
            )

    # -----------------------------------------------------------------------
    # policy, recovery, escalation
    # -----------------------------------------------------------------------

    def _check_screen(self, cap: Capability, step: Any, obs: Observation) -> None:
        """Assert the step's declared screen, and name the one we are on if not. Silent when a
        capability declares no screens: an artifact making no claim about where it is gets no
        assertion, rather than a guess."""
        expected = getattr(step, "screen", None)
        if not expected or not cap.screens:
            return
        if getattr(step, "action", None) is Primitive.NAVIGATE:
            # A screen claim is about where an action lands. Navigation leaves a screen and is
            # legal from anywhere, including a cold browser.
            return
        declared = {s.name: s for s in cap.screens}
        wanted = declared.get(expected)
        # A screen signature is a property of the application, never parameterized.
        if wanted is None or evaluate(wanted.signature, obs):
            return

        here = next(
            (s.name for s in cap.screens if evaluate(s.signature, obs)),
            None,
        )
        raise _Failed(
            FailureDetail(
                kind=FailureKind.WRONG_SCREEN,
                step_id=getattr(step, "id", None),
                message=(
                    f"this step acts on the {expected!r} screen and the application "
                    f"is showing {here or 'a screen this capability does not know'}"
                ),
                expected=expected,
                observed=here or _text_of(obs)[:200],
            )
        )

    def _check_policy(
        self, cap: Capability, step: Any, intent: str, ctx: RunContext
    ) -> str:
        """Consult the guardrail and record what it said, allow or deny alike.

        Written to the step before it is acted on, so a denied run and a permitted one leave the
        same shape of record. Recording only refusals would make the evidence for "this transfer
        was allowed to happen" the absence of an entry.
        """
        action = step.action if isinstance(step, ActStep) else step.on_found_action
        decision: PolicyDecision = self.policy.decide(action, step.risk, intent)
        ctx.policy_decision = decision
        if decision.disposition == "denied":
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.POLICY_DENIED,
                    step_id=step.id,
                    message=f"{decision.rule}: {decision.detail} ({intent})",
                )
            )
        return decision.disposition

    def _check_url(self, step: Any, url: str) -> None:
        try:
            self.policy.check_url(url)
        except PolicyDenied as e:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.POLICY_DENIED, step_id=step.id, message=str(e)
                )
            ) from e

    async def _check_url_after(self, step: Any) -> None:
        """A click can navigate. Checking only on explicit navigation would leave
        the allowlist trivially bypassable by any link on the page."""
        url = self.driver.current_url()
        if url:
            self._check_url(step, url)

    async def _recover(
        self, cap: Capability, step: Any, name: str, ctx: RunContext
    ) -> None:
        recovery = next((r for r in self.policy.recoveries if r.name == name), None)
        if recovery is None:  # pragma: no cover - classify only names real ones
            return
        ctx.recovery_counts[name] = ctx.recovery_counts.get(name, 0) + 1
        # Which handler fired, on the step it fired on.
        ctx.recovery_applied = name
        # …and whether it fired during the attempt in flight, which is what may buy a
        # re-execution. Per attempt, so a modal cleared on attempt one cannot justify a third.
        ctx.recovered_this_attempt = True

        for action in recovery.actions:
            kind = action.get("action")
            if kind == "wait":
                # The one sleep on this path, declared in policy with a number.
                await asyncio.sleep(int(action.get("value", "1000")) / 1000)
            elif kind == "click" and ctx.obs is not None:
                target = Target(
                    intent=f"dismiss {name}",
                    target_desc=f"the {name} interstitial's dismiss control",
                    anchor_text=action.get("anchor_text", ""),
                )
                try:
                    found = self.resolver.resolve(target, ctx.obs, ctx.params)
                except Unresolvable:
                    return
                await self._act_on_surface(ctx, lambda f=found: self.driver.click(f.point))
            elif kind == "key":
                keys = action.get("value", "Escape")
                await self._act_on_surface(ctx, lambda k=keys: self.driver.key(k))
            elif kind == "reload":
                # "Wait and try again" for a condition a wait alone cannot clear, bounded by
                # `max_per_run`.
                await self._act_on_surface(ctx, lambda: self.driver.reload())
            elif kind == "sign_on":
                await self._re_authenticate(cap, step, ctx, name)

    async def _re_authenticate(
        self, cap: Capability, step: Any, ctx: RunContext, condition: str
    ) -> None:
        """Sign in again and start the capability over, or hand it to a human.

        The gate is `ctx.mutated`: if this run has already executed a step policy judged risky,
        it stops, because resuming a flow whose earlier half may or may not have committed is
        not a decision an engine gets to make.
        """
        if ctx.mutated:
            await self._escalate(
                cap,
                step,
                ctx,
                reason=InterventionReason.APP_CONDITION,
                message=(
                    f"{condition}: the session expired after this run had already "
                    f"performed a risky action, so it cannot be re-run from the start"
                ),
            )
            return

        if self.sign_on is None:
            await self._escalate(
                cap,
                step,
                ctx,
                reason=InterventionReason.APP_CONDITION,
                message=f"{condition}, and this deployment has no sign-on recipe to re-run",
            )
            return

        try:
            await self.sign_on()
        except Exception as e:  # noqa: BLE001 - a bad credential is a human's problem
            # Not quoting the screen: sign-on is the one place a message may carry a
            # credential.
            await self._escalate(
                cap,
                step,
                ctx,
                reason=InterventionReason.APP_CONDITION,
                message=f"{condition}, and signing in again failed ({type(e).__name__})",
            )
            return

        raise _Restart(condition)

    async def _fail_or_escalate(
        self,
        cap: Capability,
        step: Any,
        ctx: RunContext,
        kind: FailureKind,
        message: str,
        expected: str | None = None,
        observed: str | None = None,
        region: Bbox | None = None,
    ) -> None:
        """Honour the step's declared `on_error`. Under `escalate` the run stops for a human
        rather than failing; the artifact says which steps those are."""
        if step.on_error is OnError.ESCALATE:
            await self._escalate(
                cap,
                step,
                ctx,
                reason=_reason_for(kind),
                message=message,
                expected=expected,
                observed=observed,
                failure_kind=kind,
            )
            return
        raise _Failed(
            FailureDetail(
                kind=kind,
                step_id=getattr(step, "id", None),
                message=message,
                expected=expected,
                observed=observed,
                region=region,
            )
        )

    async def _escalate(
        self,
        cap: Capability,
        step: Any,
        ctx: RunContext,
        reason: InterventionReason,
        message: str,
        expected: str | None = None,
        observed: str | None = None,
        failure_kind: FailureKind | None = None,
    ) -> None:
        """Raise an intervention and park until a human resolves it. The session is not torn
        down and the run is not unwound: it waits on an event, holding the same browser, the
        same cookies and the same half-filled form."""
        request = InterventionRequest(
            id=f"int_{uuid4().hex[:8]}",
            run_id=self.evidence.run_id,
            mode="replay",
            capability=cap.ref,
            goal=cap.goal,
            reason=reason,
            failure_kind=failure_kind,
            step_id=getattr(step, "id", None),
            step_intent=_intent(step, ctx.params),
            message=message,
            expected=expected,
            observed=observed,
            evidence=ctx.evidence,
            vnc_url=self.vnc_url or None,
            raised_at=now_iso(),
        )
        self.evidence.intervention(request)
        ctx.escalations += 1

        resolution = await self.control.escalate(request)
        self.evidence.intervention(request, resolution)

        if resolution.outcome == "abort":
            raise _Escalated(request.id, resolution.note)

        # Re-observe rather than trusting the frame we parked on: the operator may have
        # advanced the application several screens.
        await self._observe(cap, step, ctx)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _text_of(obs: Observation) -> str:
    return " ".join(t for t in ((e.text or "").strip() for e in obs.elements) if t)


def _intent(step: Any, params: dict[str, Any] | None = None) -> str:
    """What the step did, for the log. May include the value, rendered against this run's
    inputs — a navigate has no declared intent, and `members/{{member_id}}` does not say which
    member was opened."""
    declared = _declared_intent(step, params)
    if declared:
        return declared
    action = getattr(step, "action", None)
    name = action.value if action is not None else "step"
    value = render(getattr(step, "value", "") or "", params) or ""
    return f"{name} {value}".strip()


def _declared_intent(step: Any, params: dict[str, Any] | None = None) -> str:
    """What the step *means*, as written by whoever recorded it. What policy classifies and
    what pre-click verification is about: prose a person wrote, never a URL, a coordinate or a
    typed value. Rendered against this run's inputs, so a failure message names the record."""
    target = getattr(step, "target", None) or getattr(step, "scope", None)
    if target is not None:
        return render(str(target.intent), params) or ""
    return render(str(getattr(step, "note", "") or ""), params) or ""


def _terms(step: Any, params: dict[str, Any] | None = None) -> list[str]:
    """A scan predicate's terms as this run bound them, so a report names the values that were
    matched rather than the templates."""
    return [render(t, params) or "" for t in step.predicate.terms]


def _timeout_of(step: Any, default_ms: int) -> int:
    """How long this step is allowed to take, from its own checkpoint. One source for both ends
    of the step: waiting for its screen to arrive, and waiting for its checkpoint."""
    checkpoint = getattr(step, "checkpoint", None)
    return default_ms if checkpoint is None else checkpoint.timeout_ms


def _expected_of(step: Any, params: dict[str, Any] | None = None) -> str | None:
    """What the step was waiting for, as this run bound it, so a failed step reports the value
    it compared rather than the template."""
    checkpoint = getattr(step, "checkpoint", None)
    if checkpoint is None:
        return None
    return f"{checkpoint.kind.value} {render(checkpoint.value, params)!r}"


def _terminal_step(
    step: Any, intent: str, began: float, ctx: RunContext, ended: _Terminal
) -> StepResult:
    """The step record for a step that ended the run.

    A business outcome or an escalation is not a failed step: it stopped because the run
    stopped, so it is recorded SKIPPED rather than FAILED.
    """
    failure = getattr(ended, "failure", None)
    trace = ctx.resolution_trace
    return StepResult(
        step_id=getattr(step, "id", 0),
        intent=intent,
        status=StepStatus.FAILED if failure is not None else StepStatus.SKIPPED,
        # The tier reached before it went wrong: failing after falling through to the
        # recorded box and failing before resolving are different diagnoses.
        resolution=trace.tier if trace is not None else ResolutionTier.NONE,
        duration_ms=int(monotonic_ms() - began),
        # As are failing on the third attempt and failing on the first.
        attempts=ctx.attempt,
        note="; ".join(ctx.notes) or None,
        expected=failure.expected if failure is not None else _expected_of(step, ctx.params),
        observed=failure.observed if failure is not None else str(ended),
        recovery_applied=ctx.recovery_applied,
        policy=ctx.policy_decision,
        resolution_trace=trace,
        phases=_phases(ctx),
        evidence=ctx.evidence,
    )


def _phases(ctx: RunContext) -> Phases:
    """The step's own accounting, rounded to whole milliseconds for the record."""
    return Phases(
        observe_ms=int(ctx.observe_ms),
        observations=ctx.observations,
        resolve_ms=int(ctx.resolve_ms),
        act_ms=int(ctx.act_ms),
        verify_ms=int(max(0.0, ctx.verify_ms)),
    )


def _reason_for(kind: FailureKind) -> InterventionReason:
    return {
        FailureKind.RESOLUTION_EXHAUSTED: InterventionReason.RESOLUTION_EXHAUSTED,
        FailureKind.TARGET_MISMATCH: InterventionReason.TARGET_MISMATCH,
        FailureKind.UNEXPECTED_OVERLAY: InterventionReason.UNEXPECTED_OVERLAY,
        FailureKind.AMBIGUOUS_MATCH: InterventionReason.AMBIGUOUS_MATCH,
        FailureKind.POLICY_DENIED: InterventionReason.POLICY_DENIED,
    }.get(kind, InterventionReason.AGENT_REQUESTED)


def _row_text(row: list[Element]) -> str:
    return " ".join((e.text or e.name or "").strip() for e in row).strip()


def _read_row(row: list[Element], column: str | None, obs: Any, step: Any) -> str:
    """One cell of a matched row, or the whole row when no column was named.

    A capability declaring an amount output has to return an amount; the whole row would fail
    to coerce against the declared type.
    """
    if not column or obs is None:
        return _row_text(row)
    span = column_span(obs, column, above=min(e.bbox.y for e in row))
    if span is None:
        raise _Failed(
            FailureDetail(
                kind=FailureKind.EXTRACTION_FAILED,
                step_id=step.id,
                message=f"no column headed {column!r} above the matched row",
                expected=column,
                observed=_row_text(row)[:200],
            )
        )
    cell = cell_in_column(row, span)
    if cell is None:
        raise _Failed(
            FailureDetail(
                kind=FailureKind.EXTRACTION_FAILED,
                step_id=step.id,
                message=f"the matched row has no cell under {column!r}",
                expected=column,
                observed=_row_text(row)[:200],
            )
        )
    return (cell.text or cell.name or "").strip()


def _row_bbox(row: list[Element]) -> Bbox:
    x0 = min(e.bbox.x for e in row)
    y0 = min(e.bbox.y for e in row)
    x1 = max(e.bbox.x + e.bbox.w for e in row)
    y1 = max(e.bbox.y + e.bbox.h for e in row)
    return Bbox(x=x0, y=y0, w=x1 - x0, h=y1 - y0)
