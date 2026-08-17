"""Deterministic replay. The production execution path.

This is what an AI agent actually invokes. No model is in the decision loop — the
engine is constructed with `NoLLM` and a resolver that cannot use the VLM tier, so
determinism is a structural property rather than a promise.

Per-step lifecycle, in order:

    ┌─ verify permission ─┐   policy: is this capability allowed to do this, to
    │                     │   this URL, at this risk level?
    ├─ resolve ───────────┤   Target -> coordinate, via the resolver ladder
    ├─ verify target ─────┤   does the region say what the recording said?
    │                     │   is anything stacked on top of it?
    ├─ execute ───────────┤   the primitive
    └─ verify effect ─────┘   did the declared checkpoint become true?

Between resolve and verify-target the engine settles the frame — two consecutive
hash-equal observations — so nothing is resolved against a page that is still
laying out.

Detector evaluation order at each step is deliberate and is where the error
taxonomy is actually enforced:

    1. business outcomes   a declared, legitimate answer ends the run cleanly with
                           BUSINESS_OUTCOME. Checked first, because "no such
                           member" is an answer and must not be reported as a
                           checkpoint failure.
    2. recoverable         a declared app-level condition; apply the handler,
                           re-observe, retry the step, count against max_per_run.
    3. step checkpoint     the expected path.
    4. anything else       hard failure. Unknown states are not guessed at.

Waiting is the same loop. A checkpoint that is not true yet is indistinguishable
from one that is not true at all until the clock runs out, so the engine polls the
step's checkpoint until its declared timeout and there is no `sleep()` anywhere on
this path — except inside a recovery the policy explicitly declares as a wait.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..clock import monotonic_ms, now_iso
from ..perception import Unsettled
from ..policy import PolicyDenied
from ..resolve import (
    Resolution,
    Unresolvable,
    apply,
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
    Point,
    Primitive,
    ReplayResult,
    ResolutionTier,
    RunStatus,
    Status,
    StepResult,
    StepStatus,
    Target,
    ValueType,
)
from .outcomes import Classification, Classified, classify
from .scan import Scanner, Untestable, cell_in_column, column_span


class _Terminal(Exception):
    """Base for the three ways a run ends other than by finishing its steps.

    Exceptions rather than sentinel returns: every one of these can be raised from
    four levels down inside a step, and threading a status back up through each of
    them is how the taxonomy gets quietly re-litigated at each call site.
    """


class _Business(_Terminal):
    def __init__(self, outcome: OutcomeDetail) -> None:
        super().__init__(outcome.name)
        self.outcome = outcome


class _Failed(_Terminal):
    def __init__(self, failure: FailureDetail) -> None:
        super().__init__(failure.message)
        self.failure = failure


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
    ) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.resolver = resolver          # constructed with allow_vlm=False
        self.policy = policy
        self.evidence = evidence
        self.control = control            # for escalation / handoff
        self.settle_timeout_ms = settle_timeout_ms
        self.settle_poll_ms = settle_poll_ms
        self.step_timeout_ms = step_timeout_ms
        self.vnc_url = vnc_url
        # Off by default so the demo path can replay a fresh recording. A real
        # deployment turns it on, and then a draft can only be run by a human
        # watching it.
        self.require_approved = require_approved
        self.scanner = Scanner(perceiver, driver, resolver)
        # Working frames, not evidence. Every settle poll writes one and only the
        # frame a step actually acted on is worth keeping; leaving the rest in the
        # evidence directory would bury the four files a reviewer wants.
        self._scratch = Path(tempfile.mkdtemp(prefix="cua-frames-"))

    # -----------------------------------------------------------------------
    # entry point
    # -----------------------------------------------------------------------

    async def replay(self, cap: Capability, inputs: dict[str, Any]) -> ReplayResult:
        """Execute a capability with caller-supplied inputs.

        Inputs are validated against the declared `InputSpec` list before anything
        is touched. A type error should be a rejected call, not a run that gets
        four steps in and types "None" into an amount field.
        """
        run_id = self.evidence.run_id
        started = now_iso()
        clock = monotonic_ms()
        sensitive = frozenset(i.name for i in cap.inputs if i.sensitive)

        result = ReplayResult(
            run_id=run_id,
            capability=cap.ref,
            status=RunStatus.FAILURE,
            inputs={},
            started_at=started,
            evidence_dir=str(self.evidence.open()),
        )

        try:
            params = self._validate_inputs(cap, inputs)
        except _Failed as e:
            result.failure = e.failure
            return self._finish(result, started, clock)

        ctx = RunContext(params=params, sensitive=sensitive)
        # What is recorded is what the caller sent, minus anything they declared
        # sensitive. Redaction happens before the first write, not before the last.
        result.inputs = dict(self.evidence.redactor.redact_mapping(params, sensitive))
        self.evidence.result(result)

        try:
            for step in cap.steps:
                await self._run_step(cap, step, ctx)
                result.steps = ctx.steps
                self.evidence.result(result)

            await self._verify_success(cap, ctx)
            result.outputs = self._extract_outputs(cap, ctx)
            result.status = RunStatus.SUCCESS

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

        The recording happens here rather than in the caller so that a step which
        terminates the run — a business outcome, an escalation, a failure — still
        appears in the step log. A run whose evidence says nothing about the step
        it stopped on is evidence of the wrong thing.
        """
        began = monotonic_ms()
        intent = _intent(step)
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

        # The frame this step acts on. Also the frame a declared interstitial is
        # detected on, before it can swallow the click.
        obs = await self._observe(cap, step, ctx, verify=False)

        # Policy classifies the *declared* intent — the human-readable description
        # of what the step does — and never the step's value. A navigate to
        # `/transfer/review` is not a transfer, and matching risk patterns against
        # a URL would stop the run before every page whose path contains a verb.
        # Before anything else: are we even looking at the screen this step was
        # recorded on? A flow that has gone somewhere else fails here with the
        # name of where it actually is, rather than three lines later with "the
        # target was not found" — which sends an operator hunting for a layout
        # change that never happened.
        self._check_screen(cap, step, obs)

        disposition = self._check_policy(cap, step, _declared_intent(step))
        if disposition == "confirm":
            await self._escalate(
                cap,
                step,
                ctx,
                reason=InterventionReason.RISKY_ACTION_CONFIRMATION,
                message=f"risky action needs confirmation: {intent}",
            )

        if isinstance(step, FindAndActStep):
            resolution = await self._run_find_and_act(cap, step, ctx)
        else:
            resolution = await self._run_act(cap, step, ctx, obs)

        # Effects are verified against a fresh, settled frame, polled until the
        # step's own timeout: "not true yet" and "not true" are the same picture.
        result_obs, verdict = await self._await_effect(cap, step, ctx)
        if verdict.kind is Classification.FAILURE:
            await self._fail_or_escalate(
                cap,
                step,
                ctx,
                kind=FailureKind.CHECKPOINT_FAILED,
                message=f"checkpoint did not hold after {intent}",
                expected=verdict.expected,
                observed=verdict.observed,
            )

        return StepResult(
            step_id=step.id,
            intent=intent,
            status=StepStatus.OK,
            resolution=resolution.tier if resolution else ResolutionTier.NONE,
            duration_ms=int(monotonic_ms() - began),
            expected=_expected_of(step),
            observed=(str(ctx.extracted.get(step.id))[:200] if step.id in ctx.extracted else None),
            evidence=ctx.evidence,
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
            self._check_url(step, value)
            await self.driver.navigate(value)
            return None

        if step.action in (Primitive.WAIT, Primitive.ASSERT):
            # Both are pure checkpoint steps: the waiting is done by the effect
            # verification that follows, which polls until the declared timeout.
            return None

        if step.action is Primitive.KEY:
            await self.driver.key(value or "Enter")
            return None

        resolution = self._resolve(cap, step, ctx, obs)

        if step.action is Primitive.EXTRACT:
            text = region_text(obs, resolution.bbox)
            if not text:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.EXTRACTION_FAILED,
                        step_id=step.id,
                        message=f"nothing readable at the target for {step.extract_as!r}",
                        expected=step.target.target_desc if step.target else None,
                    )
                )
            ctx.extracted[step.id] = text
            return resolution

        if step.action is Primitive.SCROLL:
            await self.driver.scroll(resolution.point, float(value or "0.8"))
            return resolution

        if step.action is Primitive.CLICK:
            await self.driver.click(resolution.point)
            await self._check_url_after(step)
            return resolution

        if step.action is Primitive.TYPE:
            # Focus first. Typing into whatever happened to have focus is how a
            # password ends up in a search box.
            await self.driver.click(resolution.point)
            secret = bool(placeholders(step.value) & ctx.sensitive)
            await self.driver.type_text(value or "", secret=secret)
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
            return await self._observe(cap, step, ctx, verify=False)

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
            # The distinction the brief singles out: we ran out of budget while
            # the list was still moving, so "not found" would be a guess.
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.SCAN_INCONCLUSIVE,
                    step_id=step.id,
                    message=(
                        f"stopped after {found.advances} advances with the region still "
                        f"changing; cannot distinguish absent from not-yet-seen"
                    ),
                    expected=", ".join(step.predicate.terms),
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
                    expected=", ".join(step.predicate.terms),
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
                        f"{', '.join(step.predicate.terms)}"
                    ),
                )
            elif step.on_multiple is MultiplePolicy.FAIL:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.AMBIGUOUS_MATCH,
                        step_id=step.id,
                        message=f"{len(found.matches)} records match; policy is fail",
                        expected=", ".join(step.predicate.terms),
                    )
                )

        if step.collect_all or step.on_found_action is Primitive.EXTRACT:
            column = render(step.on_found_extract_column, ctx.params)
            values = [_read_row(row, column, found.observation, step, ctx) for row in found.matches]
            ctx.extracted[step.id] = values if step.collect_all else values[0]
            return None

        row = _row_bbox(found.matches[0])
        point = Point(
            x=min(1.0, row.x + step.on_found_offset[0] * row.w),
            y=min(1.0, row.y + step.on_found_offset[1] * row.h),
        )
        await self.driver.click(point)
        await self._check_url_after(step)
        return Resolution(point=point, bbox=row, tier=ResolutionTier.ANCHOR_TEXT)

    # -----------------------------------------------------------------------
    # perception, resolution, verification
    # -----------------------------------------------------------------------

    async def _observe(
        self, cap: Capability, step: Any, ctx: RunContext, verify: bool = True
    ) -> Observation:
        """Settle, record the frame as evidence, and handle declared conditions.

        Perception is CPU-bound (OCR, a detector forward pass) so it runs in a
        thread: the control plane has to keep answering an operator while a run is
        in flight, and a parked run has to be able to wake.
        """
        ctx.frames += 1
        live = self._scratch / f"frame-{ctx.frames:04d}.png"
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
        ctx.obs = obs
        ctx.evidence = self.evidence.frame(obs, getattr(step, "id", 0))
        return obs

    async def _await_effect(
        self, cap: Capability, step: Any, ctx: RunContext
    ) -> tuple[Observation, Classified]:
        """Poll the step's checkpoint until it holds or its timeout expires.

        This is the whole waiting strategy. A checkpoint that has not become true
        yet looks exactly like one that never will, so the only honest answer is
        to keep looking until the artifact's declared deadline.
        """
        checkpoint = getattr(step, "checkpoint", None)
        timeout = checkpoint.timeout_ms if checkpoint is not None else self.step_timeout_ms
        deadline = monotonic_ms() + timeout

        while True:
            obs = await self._observe(cap, step, ctx, verify=False)
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
                await self._recover(verdict.name, ctx)
                continue

            if verdict.kind is Classification.APP_ERROR:
                # No handler exists and none should be invented. Stop with the
                # kind that names the cause instead of the symptom.
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.APP_ERROR,
                        step_id=getattr(step, "id", None),
                        message=f"the application reported an error: {verdict.name}",
                        observed=verdict.observed,
                    )
                )

            if verdict.kind is Classification.ESCALATE:
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
        try:
            resolution: Resolution = self.resolver.resolve(step.target, obs, ctx.params)
        except Unresolvable as e:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.RESOLUTION_EXHAUSTED,
                    step_id=step.id,
                    message=str(e),
                    expected=step.target.target_desc,
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
                )
            )
        return resolution

    async def _verify_success(self, cap: Capability, ctx: RunContext) -> None:
        """The capability's own final assertion.

        Separate from the last step's checkpoint: a flow can execute every step
        correctly and still not have achieved what it was recorded to achieve.
        """
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
        """Assert the step's declared screen, and name the one we are on if not.

        Silent when a capability declares no screens: an artifact that makes no
        claim about where it is gets no assertion, rather than a guess.
        """
        expected = getattr(step, "screen", None)
        if not expected or not cap.screens:
            return
        if getattr(step, "action", None) is Primitive.NAVIGATE:
            # A screen claim is about where an action lands. Navigation is how you
            # *leave* a screen, and it is legal from anywhere — asserting one
            # before it would make a capability refuse to start from a cold
            # browser.
            return
        declared = {s.name: s for s in cap.screens}
        wanted = declared.get(expected)
        if wanted is None or evaluate(wanted.signature, obs, ctx_params(step)):
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

    def _check_policy(self, cap: Capability, step: Any, intent: str) -> str:
        action = step.action if isinstance(step, ActStep) else step.on_found_action
        try:
            return str(self.policy.check_action(action, step.risk, intent))
        except PolicyDenied as e:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.POLICY_DENIED,
                    step_id=step.id,
                    message=str(e),
                )
            ) from e

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

    async def _recover(self, name: str, ctx: RunContext) -> None:
        recovery = next((r for r in self.policy.recoveries if r.name == name), None)
        if recovery is None:  # pragma: no cover - classify only names real ones
            return
        ctx.recovery_counts[name] = ctx.recovery_counts.get(name, 0) + 1

        for action in recovery.actions:
            kind = action.get("action")
            if kind == "wait":
                # The one sleep on this path, and it is declared in policy with a
                # number rather than guessed at by the engine.
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
                await self.driver.click(found.point)
            elif kind == "key":
                await self.driver.key(action.get("value", "Escape"))

    async def _fail_or_escalate(
        self,
        cap: Capability,
        step: Any,
        ctx: RunContext,
        kind: FailureKind,
        message: str,
        expected: str | None = None,
        observed: str | None = None,
    ) -> None:
        """Honour the step's declared `on_error`.

        `escalate` is the interesting one: a step that a human could unstick is
        worth stopping for rather than failing, and the artifact says which steps
        those are.
        """
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
        """Raise an intervention and park until a human resolves it.

        The session is not torn down and the run is not unwound — it is waiting on
        an event, holding the same browser, the same cookies and the same
        half-filled form the operator is about to look at.
        """
        request = InterventionRequest(
            id=f"int_{uuid4().hex[:8]}",
            run_id=self.evidence.run_id,
            mode="replay",
            capability=cap.ref,
            goal=cap.goal,
            reason=reason,
            failure_kind=failure_kind,
            step_id=getattr(step, "id", None),
            step_intent=_intent(step),
            message=message,
            expected=expected,
            observed=observed,
            evidence=ctx.evidence,
            vnc_url=self.vnc_url or None,
            raised_at=now_iso(),
        )
        self.evidence.intervention(request)

        resolution = await self.control.escalate(request)
        self.evidence.intervention(request, resolution)

        if resolution.outcome == "abort":
            raise _Escalated(request.id, resolution.note)

        # Resumed. Re-observe rather than trusting the step counter: the operator
        # may have advanced the application several screens while they had it.
        await self._observe(cap, step, ctx, verify=False)

    # -----------------------------------------------------------------------
    # contract
    # -----------------------------------------------------------------------

    def _validate_inputs(self, cap: Capability, inputs: dict[str, Any]) -> dict[str, Any]:
        if self.require_approved and cap.status is not Status.APPROVED:
            raise _Failed(
                FailureDetail(
                    kind=FailureKind.POLICY_DENIED,
                    message=f"{cap.ref} is {cap.status.value}; unattended replay needs approval",
                )
            )

        params: dict[str, Any] = {}
        for spec in cap.inputs:
            if spec.name not in inputs or inputs[spec.name] is None:
                if spec.required:
                    raise _Failed(
                        FailureDetail(
                            kind=FailureKind.INTERNAL,
                            message=f"missing required input {spec.name!r}",
                        )
                    )
                continue
            try:
                params[spec.name] = _coerce(inputs[spec.name], spec.type)
            except (TypeError, ValueError) as e:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.INTERNAL,
                        message=f"input {spec.name!r}: {e}",
                        expected=spec.type.value,
                        observed=repr(inputs[spec.name]),
                    )
                ) from e
            _check_constraints(spec, params[spec.name], params)

        # Anything the caller sent that the capability does not declare is
        # dropped rather than passed through: a template can only reference
        # declared inputs, and silently accepting extras invites a caller to
        # believe they are steering something.
        return params

    def _extract_outputs(self, cap: Capability, ctx: RunContext) -> dict[str, Any]:
        """Read declared outputs from the recorded extract steps.

        A missing required output is EXTRACTION_FAILED, not a partial success. The
        caller's contract says what it gets back; returning three of four fields
        with no signal is how a downstream agent ends up acting on a null balance.
        """
        outputs: dict[str, Any] = {}
        for spec in cap.outputs:
            raw = ctx.extracted.get(spec.from_step)
            if raw is None:
                if spec.required:
                    raise _Failed(
                        FailureDetail(
                            kind=FailureKind.EXTRACTION_FAILED,
                            step_id=spec.from_step,
                            message=f"no value was extracted for output {spec.name!r}",
                        )
                    )
                continue
            try:
                if isinstance(raw, list):
                    outputs[spec.name] = [_coerce(apply(v, spec.normalize), spec.type) for v in raw]
                else:
                    outputs[spec.name] = _coerce(apply(raw, spec.normalize), spec.type)
            except (TypeError, ValueError) as e:
                raise _Failed(
                    FailureDetail(
                        kind=FailureKind.EXTRACTION_FAILED,
                        step_id=spec.from_step,
                        message=f"output {spec.name!r} is not a {spec.type.value}: {e}",
                        observed=str(raw)[:200],
                    )
                ) from e
        return outputs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def ctx_params(step: Any) -> dict[str, Any]:
    """Screen signatures are not parameterized: a screen is a property of the
    application, not of the caller's arguments."""
    return {}


def _text_of(obs: Observation) -> str:
    return " ".join(t for t in ((e.text or "").strip() for e in obs.elements) if t)


def _intent(step: Any) -> str:
    """What the step did, for the log. May include the value — a navigate whose
    URL is missing from the evidence is a step nobody can retrace."""
    declared = _declared_intent(step)
    if declared:
        return declared
    action = getattr(step, "action", None)
    name = action.value if action is not None else "step"
    return f"{name} {getattr(step, 'value', '') or ''}".strip()


def _declared_intent(step: Any) -> str:
    """What the step *means*, as written by whoever recorded it.

    This is what policy classifies and what pre-click verification is about. It is
    prose a person wrote; it is never a URL, a coordinate or a typed value.
    """
    target = getattr(step, "target", None) or getattr(step, "scope", None)
    if target is not None:
        return str(target.intent)
    return str(getattr(step, "note", "") or "")


def _expected_of(step: Any) -> str | None:
    checkpoint = getattr(step, "checkpoint", None)
    return None if checkpoint is None else f"{checkpoint.kind.value} {checkpoint.value!r}"


def _terminal_step(
    step: Any, intent: str, began: float, ctx: RunContext, ended: _Terminal
) -> StepResult:
    """The step record for a step that ended the run.

    A business outcome or an escalation is not a failed step — the step stopped
    because the run stopped, and calling that FAILED would put a red mark against
    the one screen that answered the caller's question correctly.
    """
    failure = getattr(ended, "failure", None)
    return StepResult(
        step_id=getattr(step, "id", 0),
        intent=intent,
        status=StepStatus.FAILED if failure is not None else StepStatus.SKIPPED,
        duration_ms=int(monotonic_ms() - began),
        expected=failure.expected if failure is not None else _expected_of(step),
        observed=failure.observed if failure is not None else str(ended),
        evidence=ctx.evidence,
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


def _read_row(
    row: list[Element], column: str | None, obs: Any, step: Any, ctx: RunContext
) -> str:
    """One cell of a matched row, or the whole row when no column was named.

    A capability that says it returns an amount has to return an amount. Handing
    back the whole row makes the caller parse a screen we already parsed, and the
    declared output type then fails to coerce — which is the failure surfacing in
    the wrong place.
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


def _coerce(value: Any, kind: ValueType) -> Any:
    if kind is ValueType.INTEGER:
        return int(str(value).strip())
    if kind is ValueType.NUMBER:
        return float(str(value).strip())
    if kind is ValueType.BOOLEAN:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "yes", "1")
    return str(value).strip()


def _check_constraints(spec: Any, value: Any, params: dict[str, Any]) -> None:
    c = spec.constraints
    if c is None:
        return
    if c.pattern and not re.match(c.pattern, str(value)):
        raise ValueError(f"{value!r} does not match {c.pattern}")
    if c.min is not None and float(value) < c.min:
        raise ValueError(f"{value!r} is below the minimum {c.min}")
    if c.max is not None and float(value) > c.max:
        raise ValueError(f"{value!r} is above the maximum {c.max}")
    if c.choices and str(value) not in c.choices:
        raise ValueError(f"{value!r} is not one of {c.choices}")
    if c.not_equal_to and str(value) == str(params.get(c.not_equal_to)):
        # "transfer from X to Y" where X == Y. A validation rule the application
        # would also enforce, caught before we touch it.
        raise ValueError(f"must differ from {c.not_equal_to}")
