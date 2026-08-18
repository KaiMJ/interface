"""Deterministic replay. The production execution path.

This is what an AI agent actually invokes. No model is in the decision loop — the
engine is constructed with `NoLLM` and a resolver that cannot use the VLM tier, so
determinism is a structural property rather than a promise.

Per-step lifecycle, in order:

    ┌─ clear the way ─────┐   is a declared app condition already on screen? A
    │                     │   modal that is up *before* the click swallows it.
    ├─ verify permission ─┤   policy: is this capability allowed to do this, to
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
                           re-observe, and re-execute the step if it is safe to,
                           counting against max_per_run.
    3. step checkpoint     the expected path.
    4. anything else       hard failure. Unknown states are not guessed at.

Waiting is the same loop. A checkpoint that is not true yet is indistinguishable
from one that is not true at all until the clock runs out, so the engine polls the
step's checkpoint until its declared timeout and there is no `sleep()` anywhere on
this path — except inside a recovery the policy explicitly declares as a wait.

Retry, and why it is narrow
---------------------------
A step runs more than once in exactly two situations, and never blindly:

    on_error: retry     the artifact declared the step repeatable and gave a
                        budget (`retries`). For a transient load this is what the
                        caller wants.
    a recovery fired    an interstitial was dismissed and the checkpoint still did
                        not hold by its deadline, which is the signature of an
                        action the modal ate. The recording could not have declared
                        a handler for a dialog the app grew afterwards, so this one
                        attempt is granted by the engine rather than by the artifact.

Both are gated on the same question, and it is one the system already answers:
**is the step safe?** `risk` is the declaration of reversibility — that is what it
is *for* — so a step policy considers safe may be executed twice, and a risky one
may not, ever, automatically. Re-clicking a submit whose checkpoint we could not
read is how one transfer becomes two, and no retry budget is worth that.

The order matters too. The checkpoint is polled to its full deadline *before* any
re-execution, so a step whose action did land and was merely obscured is never run
a second time. Re-execution is what happens when the evidence says nothing
happened.
"""

from __future__ import annotations

import asyncio
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
from .scan import Scanner, Untestable, cell_in_column, column_span
from .tenant import rebase

# One re-execution after a declared recovery cleared and the checkpoint still did
# not hold. Not configurable: a second one would mean the first re-execution also
# ran into the interstitial, and at that point the recovery is not working — which
# `Recovery.max_per_run` already calls a hard failure.
_RECOVERY_RETRIES = 1

# How many times a run may re-authenticate and start over, and how many times one
# step may park for a human, both live in app policy (`max_restarts`,
# `max_escalations_per_step`) — they are judgements about an application, not
# about this mechanism. These are the defaults policy applies when a file says
# nothing, kept here only so the reason survives next to the code that enforces
# them: a session that dies twice inside ninety seconds is not a transient, and an
# operator who resumes without clearing the condition would otherwise be handed
# the same intervention forever.


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


class _Restart(_Terminal):
    """Re-authenticated. The session is good again but the flow lost its place.

    Signing back in lands on the application's landing page, not on the screen the
    run was three steps into — which is the real reason a mid-flow session expiry
    is not simply "log in again and carry on", and a stronger reason than the one
    this system used to give (that the automation would hold a credential; it
    already holds one, and types it at session start).

    So the only honest recovery is to start the capability over, and that is only
    available while nothing irreversible has happened. `_re_authenticate` is where
    that is decided; this exception is how the decision reaches `replay()`.
    """

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
    # What the current step decided, accumulated as it goes. Held here rather than
    # returned up the call chain because a step that ends the run — a business
    # outcome, an escalation, a hard failure — unwinds through an exception, and
    # the step whose evidence matters most is exactly the one that did not return
    # normally. Reset per step by `_run_step`.
    policy_decision: PolicyDecision | None = None
    resolution_trace: ResolutionTrace | None = None
    recovery_applied: str | None = None
    # How many times the current step has been executed, and whether a recovery
    # fired during the attempt in flight. The second is per-attempt rather than
    # per-step because it is what grants the extra attempt: a recovery on attempt
    # one must not also justify a third execution on attempt two.
    attempt: int = 1
    recovered_this_attempt: bool = False
    escalations: int = 0
    notes: list[str] = field(default_factory=list)
    # Per *run*, not per step, and never reset: whether anything irreversible has
    # already happened. It is what decides whether a session that dies mid-flow
    # can be signed back into and re-run, or has to wait for a person — see
    # `_re_authenticate`.
    mutated: bool = False
    restarts: int = 0
    # The recording's geometry is checked against this deployment's once, on the
    # first frame — the earliest point at which the answer is knowable.
    viewport_checked: bool = False
    # Where this step's wall clock went. Reset per step by `_run_step`; folded
    # into the step record at the end of `_attempt`.
    observe_ms: float = 0.0
    observations: int = 0
    resolve_ms: float = 0.0
    act_ms: float = 0.0
    verify_ms: float = 0.0
    # The settled observation the previous step's verification left behind, when
    # nothing has acted since. See `_clear_the_way`.
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
        self.resolver = resolver          # constructed with allow_vlm=False
        self.policy = policy
        self.evidence = evidence
        self.control = control            # for escalation / handoff
        self.settle_timeout_ms = settle_timeout_ms
        self.settle_poll_ms = settle_poll_ms
        self.step_timeout_ms = step_timeout_ms
        self.vnc_url = vnc_url
        # This deployment's install of the application. A recorded URL names the
        # institution the capability was recorded at, which on replay is a fact
        # about the past rather than about where this run should act — see
        # `replay/tenant.py`.
        self.entry_url = entry_url
        # How to get this session back into an authenticated state. A callable,
        # not a credential: the engine holds a closure over the session's own
        # sign-on, so the secret stays where it already lived and this class has
        # no field anyone could serialize. None means the deployment supplied no
        # recipe, and a session that dies mid-flow then has to wait for a human.
        self.sign_on = sign_on
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
            app=cap.app.name,
            # Not FAILURE. This object is written to evidence before every step so
            # a run can be watched while it happens, and a partially-written run
            # that reads `failure` is a run the console reports as broken until it
            # finishes. The terminal status is set once, below.
            status=RunStatus.RUNNING,
            inputs={},
            started_at=started,
            evidence_dir=str(self.evidence.open()),
        )

        try:
            params = validate_inputs(cap, inputs, self.require_approved)
            # Every inherited detector resolves against this app's policy, checked
            # before anything is touched. An outcome that silently resolved to
            # nothing would not fail — it would simply never match, and the
            # capability would report "no such member" as a checkpoint failure
            # while its contract went on advertising the outcome.
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
        # What is recorded is what the caller sent, minus anything they declared
        # sensitive. Redaction happens before the first write, not before the last.
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
                    # Re-authenticated mid-flow. The session is good and the place
                    # in the flow is gone, so the capability starts over.
                    #
                    # Once. A second expiry inside one run is not a transient
                    # condition — it is a session that will not stay up, and
                    # retrying it forever is how an automation spends a night
                    # signing in. The steps already taken stay in the log: a run
                    # that ran step 2 twice should say so.
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

        The recording happens here rather than in the caller so that a step which
        terminates the run — a business outcome, an escalation, a failure — still
        appears in the step log. A run whose evidence says nothing about the step
        it stopped on is evidence of the wrong thing.
        """
        began = monotonic_ms()
        intent = _intent(step)
        ctx.policy_decision = None
        ctx.resolution_trace = None
        ctx.recovery_applied = None
        ctx.attempt = 1
        ctx.recovered_this_attempt = False
        ctx.escalations = 0
        ctx.notes = []
        ctx.observe_ms = ctx.resolve_ms = ctx.act_ms = ctx.verify_ms = 0.0
        ctx.observations = 0
        # A step's frames are its own. Carried over, the previous step's
        # after-frame would attach to this one and the console would show a
        # picture from before the step it is labelled with.
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

        The loop body is one attempt. Everything that decides whether there is
        another one is here rather than inside the attempt, so "when do we run a
        step twice" is answerable by reading twenty lines in one place — which is
        the property worth having for a rule whose failure mode is a duplicate
        transfer.
        """
        while True:
            resolution, result_obs, verdict = await self._once(cap, step, ctx, intent)

            if verdict.kind is Classification.APP_ERROR:
                # The application said it failed. Whether that is worth another go
                # is the same question as any other re-execution, so it is asked
                # here rather than settled three frames down: a step the artifact
                # declared repeatable gets its declared budget, and everything
                # else stops with the kind that names the cause.
                #
                # Note the two mechanisms this deliberately leaves distinct. A
                # *recovery* is the app's operator saying "this condition is
                # transient on this application"; `on_error: retry` is the
                # recording saying "this step is repeatable". Either can justify
                # another attempt; neither implies the other.
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
                # Reached only when `on_error: escalate` and a human resolved it:
                # the step counts as satisfied by the operator. See
                # `escalation/control.py` for why it is not re-run.
                break

            ctx.attempt += 1
            ctx.recovered_this_attempt = False
            ctx.notes.append(f"attempt {ctx.attempt}: {again}")

        return StepResult(
            step_id=step.id,
            # A step that needed a declared recovery still succeeded, but not in
            # the same way as one that did not — and a capability whose replays
            # increasingly report `recovered` is telling you the app grew an
            # interstitial, which is worth seeing before it becomes a failure.
            status=StepStatus.RECOVERED if ctx.recovery_applied else StepStatus.OK,
            intent=intent,
            resolution=resolution.tier if resolution else ResolutionTier.NONE,
            settled_by=result_obs.settled_by,
            duration_ms=int(monotonic_ms() - began),
            attempts=ctx.attempt,
            note="; ".join(ctx.notes) or None,
            expected=_expected_of(step),
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

        # The frame this step acts on — and the frame a declared interstitial is
        # detected on, *before* it can swallow the click. This is a real check,
        # not a comment: an already-open modal is dismissed here, and the step
        # then acts on the page it was recorded against.
        obs = await self._clear_the_way(cap, step, ctx)
        self._check_viewport(cap, ctx, obs)

        # Policy classifies the *declared* intent — the human-readable description
        # of what the step does — and never the step's value. A navigate to
        # `/transfer/review` is not a transfer, and matching risk patterns against
        # a URL would stop the run before every page whose path contains a verb.
        # First, and without waiting on anything: a denied step should not spend a
        # step timeout discovering that it was never going to be allowed to run.
        disposition = self._check_policy(cap, step, _declared_intent(step), ctx)
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

        # From here on this run has done something it cannot take back, which is
        # what stops a later session expiry from being recoverable by starting
        # over. Policy's verdict rather than the artifact's own field, so a step
        # promoted to risky from its intent counts too.
        decided = ctx.policy_decision
        if decided is not None and decided.effective_risk != Risk.SAFE.value:
            ctx.mutated = True

        # Effects are verified against a fresh, settled frame, polled until the
        # step's own timeout: "not true yet" and "not true" are the same picture.
        result_obs, verdict = await self._await_effect(cap, step, ctx)
        return resolution, result_obs, verdict

    async def _wait_until_ready(
        self, cap: Capability, step: Any, ctx: RunContext, obs: Observation
    ) -> Observation:
        """Poll until the screen this step needs is actually in front of us.

        The same discipline `_await_effect` applies to the far end of a step,
        applied to the near end. It was missing, and the asymmetry was visible the
        first time a page took four seconds to render: the engine polled a
        checkpoint for fifteen seconds when it was *leaving* a screen and gave a
        target one attempt when it was arriving at one. "The row is not there" and
        "the row is not there yet" are the same picture, and only one of them is a
        reason to stop.

        The gap is widest exactly where it is least visible: a step recorded
        without a checkpoint imposes no wait at all, so all of the previous step's
        latency lands on this one as `target_mismatch` — which reads as UI drift
        and is not.

        **This needs no risk gate, and that is the point of doing it here.**
        Everything polled is a read — the screen assertion and the resolver ladder
        execute nothing. Retrying after an action is the direction that can double
        a transfer, and that one is gated on `risk` in `_retry_reason`.
        """
        deadline = monotonic_ms() + _timeout_of(step, self.step_timeout_ms)
        began = monotonic_ms()
        polls = 0

        while True:
            try:
                # Are we even looking at the screen this step was recorded on? A
                # flow that has gone somewhere else fails with the name of where
                # it actually is, rather than three lines later with "the target
                # was not found" — which sends an operator hunting for a layout
                # change that never happened.
                self._check_screen(cap, step, obs)
                if isinstance(step, ActStep) and step.target is not None:
                    # Resolved here only to find out whether it *can* be. The
                    # ladder is pure computation over an observation we already
                    # have, so doing it twice costs nothing and keeps the
                    # execution path below unchanged.
                    self._resolve(cap, step, ctx, obs)
            except _Failed as e:
                # One thing is not worth waiting on: a screen we positively
                # recognise as a *different* declared screen. Mid-navigation looks
                # like a page we cannot name; being definitely on the sign-on
                # screen is not a transient, and burning a step timeout before
                # saying so delays the answer without improving it.
                if e.failure.kind is FailureKind.WRONG_SCREEN and e.failure.observed in {
                    s.name for s in cap.screens
                }:
                    raise
                if monotonic_ms() >= deadline:
                    raise
                polls += 1
                # `_clear_the_way` rather than a bare observe: an interstitial that
                # arrives *while* we are waiting is the same interstitial, and it
                # would otherwise sit on top of the target for the rest of the
                # deadline and then be reported as a target that never appeared.
                obs = await self._clear_the_way(cap, step, ctx)
                continue

            if polls:
                waited = int(monotonic_ms() - began)
                ctx.notes.append(f"waited {waited}ms for this step's screen to arrive")
            return obs

    def _retry_reason(self, step: Any, ctx: RunContext) -> str | None:
        """Why this step gets another execution, or None if it does not.

        Two grants, one gate. The gate is `risk`, which is already the system's
        declaration of reversibility — a step policy judged safe can be executed
        twice because that is what safe *means*, and a risky one cannot be, at any
        budget. That policy verdict is used rather than the artifact's own field so
        that a step promoted to risky from its intent (the backstop against a
        mislabelled recording) is also excluded from retry.
        """
        decision = ctx.policy_decision
        effective = decision.effective_risk if decision is not None else step.risk.value
        if effective != Risk.SAFE.value or step.risk is not Risk.SAFE:
            return None

        # Granted by the engine: a recovery fired and the checkpoint still did not
        # hold, which is the signature of an action the interstitial ate. The
        # recording cannot have declared a budget for a dialog the app grew after
        # it was written, so this attempt does not come out of one.
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

        Before the step acts, not after. An interstitial that is up when the click
        is issued absorbs it, and the recorded coordinate still resolves to the
        right place underneath — which is the whole reason the demo app's modal
        does not move the page. Detecting it only during effect verification costs
        the step's full timeout and then reports a checkpoint failure, which is the
        symptom rather than the cause.

        Deliberately narrow: **only recoverable conditions**. The distinction is
        obstruction versus state. A maintenance modal is something *on top of* the
        page this step was recorded against, it has a declared handler, and
        clearing it puts us back where the recording was — that is an obstruction.
        A session-expiry screen or an application error is not obstructing the
        right page; it *is* a different page, and the machinery that already names
        that is the step's own screen assertion and the effect classification after
        the previous step. Duplicating those here would give the same state two
        detectors and, sooner or later, two different answers.

        Business outcomes are likewise absent: they are answers a step produces, so
        they belong after it, and a checkpoint has nothing to say about a step that
        has not run yet.

        **The first frame is usually free.** The previous step's effect
        verification ended by settling on a frame, and nothing has acted between
        then and now — so that frame *is* this step's starting frame, and taking
        another costs one full perception (~2.4s on a dense page, and it was half
        of a replay's wall clock) to photograph the same screen twice. `ctx.fresh`
        carries it; anything that acts on the surface drops it, so the reuse can
        only ever span an interval in which this system did nothing.
        """
        reused = ctx.fresh
        ctx.fresh = None
        if reused is not None:
            # Its after-frame becomes this step's acted-on frame. Same pixels,
            # deliberately written under both names: one moment described from two
            # steps' points of view, and a reader following one step should not
            # have to know what the previous one did to see what it looked at.
            obs = self._adopt(reused, step, ctx)
        else:
            obs = await self._observe(cap, step, ctx)
        # Bounded by the recovery's own `max_per_run`, which `conditions` enforces;
        # this is the backstop for a policy file declaring an absurd cap.
        for _ in range(8):
            verdict = conditions(obs, self.policy, ctx.recovery_counts)

            if verdict is not None and verdict.kind is Classification.RECOVERABLE and verdict.name:
                await self._recover(cap, step, verdict.name, ctx)
                ctx.notes.append(f"cleared {verdict.name!r} before acting")
                obs = await self._observe(cap, step, ctx)
                continue

            if verdict is not None and verdict.kind is Classification.FAILURE:
                # A recovery past its cap, with the condition still up. Not "the
                # checkpoint failed": the condition has a name and the handler is
                # the thing that stopped working. Not `unexpected_overlay` either
                # — that names something *undeclared* on top of the target, and
                # this one was declared, handled, and did not clear.
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
            # The artifact supplies the path; this deployment supplies the origin.
            # Checked against the allowlist *after* rebasing, so what is permitted
            # is what is actually navigated to.
            value, note = rebase(value, self.entry_url)
            if note:
                ctx.notes.append(note)
            self._check_url(step, value)
            await self._act_on_surface(ctx, lambda: self.driver.navigate(value))
            return None

        if step.action in (Primitive.WAIT, Primitive.ASSERT):
            # Both are pure checkpoint steps: the waiting is done by the effect
            # verification that follows, which polls until the declared timeout.
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
                        expected=step.target.target_desc if step.target else None,
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
            # Focus first. Typing into whatever happened to have focus is how a
            # password ends up in a search box.
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

        Perception only. Classification is the caller's job — `_clear_the_way`
        before a step and `_await_effect` after it — because the two ask different
        questions of the same picture, and folding either into perception is how
        the taxonomy ends up being decided in two places.

        Perception is CPU-bound (OCR, a detector forward pass) so it runs in a
        thread: the control plane has to keep answering an operator while a run is
        in flight, and a parked run has to be able to wake.
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

        Every path to the surface goes through here, which is what makes "nothing
        has touched the screen since we last looked" a property the engine can
        rely on rather than one it has to remember to maintain at seven call
        sites — and the reuse in `_clear_the_way` is only sound because of it.
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

        Which slot it lands in is the whole of the before/after distinction: the
        frame a step acted on is the one its target was resolved against, and the
        frame it produced is the one its checkpoint was judged on. Writing both to
        one path loses whichever the reader is actually asking about.
        """
        ctx.obs = obs
        ctx.evidence = self.evidence.frame(
            obs, getattr(step, "id", 0), after=after, prior=ctx.evidence
        )
        return obs

    async def _unchanged(self, ctx: RunContext) -> bool:
        """Is the screen byte-identical to the observation we already have?

        A poll loop's cheap first question. When the answer is yes there is
        nothing to re-interpret: the verdict on an identical frame is the verdict
        we already computed. Costs a screen grab and a hash.
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

        This is the whole waiting strategy. A checkpoint that has not become true
        yet looks exactly like one that never will, so the only honest answer is
        to keep looking until the artifact's declared deadline.

        What it does *not* do is re-read a screen that has not changed. Polling
        used to cost a full perception per turn, which on a page taking four
        seconds to render meant several seconds of text recognition establishing
        that the previous screen was still the previous screen. A frame hash
        answers that, and a byte-identical frame cannot classify differently.
        """
        deadline = monotonic_ms() + _timeout_of(step, self.step_timeout_ms)
        entered = monotonic_ms()
        observing = ctx.observe_ms
        polled = False

        while True:
            if polled and await self._unchanged(ctx):
                if monotonic_ms() >= deadline:
                    # Out of time on a screen that never moved. Fall through to
                    # one last real read so the verdict — and the `observed` text
                    # a human will be shown — comes from an interpreted frame
                    # rather than from a hash comparison.
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
                # Returned, not raised: no handler exists and none should be
                # invented, but whether the *step* is worth running again is a
                # question `_attempt` owns — it is the same question as any other
                # re-execution and it has the same gate. Polling stops here either
                # way, because an application error is not "not yet".
                ctx.verify_ms += monotonic_ms() - entered - (ctx.observe_ms - observing)
                return obs, verdict

            if verdict.kind is Classification.ESCALATE:
                if ctx.escalations >= self.policy.max_escalations_per_step:
                    # The operator handed the session back with the condition
                    # still on screen. Asking again would park the run on the same
                    # intervention indefinitely; the honest report is that we are
                    # stuck on a state a human has already looked at twice.
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
                # The frame this step ended on. Nothing acts between here and the
                # next step's first look, so that step does not need to take its
                # own — see `_clear_the_way`.
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
                    # Where it is, not just what it said. The console draws this
                    # over the step's frame, which is the difference between an
                    # operator reading "an undeclared element covers the target"
                    # and seeing which element.
                    region=check.region,
                )
            )
        # Booked whether or not the ladder found anything: a step that spent its
        # time failing to resolve is exactly the one whose profile matters.
        ctx.resolve_ms += monotonic_ms() - began
        return resolution

    def _check_viewport(self, cap: Capability, ctx: RunContext, obs: Observation) -> None:
        """Is this deployment's display the shape the capability was recorded on?

        Coordinates in an artifact are normalized 0..1, so a display that is
        larger or smaller in the same proportions is not a problem: every recorded
        box still covers the same fraction of the same content. A different
        *aspect ratio* is a different problem entirely — the application reflows,
        so a recorded box no longer covers what it covered, and the recorded-bbox
        tier starts returning coordinates that are precise and wrong.

        The artifact has always carried `recording.viewport` and nothing compared
        it to anything, which made "the recording viewport is part of the
        contract" a sentence in a config file rather than a fact. Checked once per
        run, on the first frame, because the answer cannot change mid-run and
        paying for it per step would be paying for a constant.

        A scale change is a note; a shape change stops the run. Neither is a
        guess about how much distortion is survivable — the line is whether the
        page laid out differently, and that is what aspect ratio measures.
        """
        if ctx.viewport_checked:
            return
        ctx.viewport_checked = True
        if cap.recording is None:
            # A hand-written capability makes no claim about the geometry it was
            # authored against, and inventing one to check against would be
            # inventing a constraint its author did not write.
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

        The resolver reports ambiguity rather than hiding it — `_pick` chooses the
        match nearest the recorded position, which is a good guess and is exactly
        as good as a guess. On a read that is fine and worth a note: two elements
        said the right words, we took the one where the recording found it, and a
        wrong answer will fail its own checkpoint.

        On a write it is not fine, for the same reason `find_and_act` defaults to
        `on_multiple: escalate`: three rows whose anchor reads "View" are three
        different members, and acting on the wrong one is unrecoverable. The
        discipline already existed on the scan path, which is the harder case and
        so got the attention; this is the same rule on the ordinary one.

        Gated on policy's *effective* risk, so a step the recording mislabelled as
        safe and policy promoted is covered too.
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

    def _check_policy(
        self, cap: Capability, step: Any, intent: str, ctx: RunContext
    ) -> str:
        """Consult the guardrail and record what it said, allow or deny alike.

        The decision is written to the step before it is acted on, so a run that
        is denied and a run that is permitted leave the same shape of record. Only
        recording refusals would mean the evidence for "this transfer was allowed
        to happen" is the absence of an entry.
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
        # Which handler fired, on the step it fired on. `recovery_applied` existed
        # on StepResult and was never populated: a run that dismissed two
        # interstitials looked identical to one that met none.
        ctx.recovery_applied = name
        # …and whether it fired during the attempt currently in flight, which is
        # what may buy that attempt a re-execution. Reset per attempt, not per
        # step: a modal cleared on attempt one must not also justify attempt three.
        ctx.recovered_this_attempt = True

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
                await self._act_on_surface(ctx, lambda f=found: self.driver.click(f.point))
            elif kind == "key":
                keys = action.get("value", "Escape")
                await self._act_on_surface(ctx, lambda k=keys: self.driver.key(k))
            elif kind == "reload":
                # Re-request the page. What makes "wait and try again" expressible
                # for a condition that a wait alone cannot clear — a 5xx, a failed
                # partial render — with the recovery's own `max_per_run` as the
                # bound on how long we are willing to believe it is transient.
                await self._act_on_surface(ctx, lambda: self.driver.reload())
            elif kind == "sign_on":
                await self._re_authenticate(cap, step, ctx, name)

    async def _re_authenticate(
        self, cap: Capability, step: Any, ctx: RunContext, condition: str
    ) -> None:
        """Sign in again and start the capability over, or hand it to a human.

        The gate is `ctx.mutated`: has this run already executed a step policy
        judged risky? If it has not, nothing it did needs a person's judgement to
        re-do, and re-running the flow from step 1 against a fresh session is
        precisely what "safe" means. If it has, we stop — resuming a flow whose
        earlier half may or may not have committed is not a decision an engine
        gets to make, and a balance read at 3am should not page someone while a
        half-finished transfer should.

        Note what is *not* the argument: that the automation would have to hold a
        credential. It already holds one and types it at session start. What
        changes across a mid-flow expiry is the state of the work, not the secret.
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
            # Deliberately not quoting what the screen said: the sign-on screen is
            # the one place a message may contain a credential.
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
        ctx.escalations += 1

        resolution = await self.control.escalate(request)
        self.evidence.intervention(request, resolution)

        if resolution.outcome == "abort":
            raise _Escalated(request.id, resolution.note)

        # Resumed. Re-observe rather than trusting the step counter: the operator
        # may have advanced the application several screens while they had it.
        await self._observe(cap, step, ctx)

    # -----------------------------------------------------------------------
    # contract
    # -----------------------------------------------------------------------


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


def _timeout_of(step: Any, default_ms: int) -> int:
    """How long this step is allowed to take, from its own checkpoint.

    One source for both ends of the step — arriving at a screen and leaving it —
    so a capability that declares a generous timeout for a slow screen gets it
    when the screen is loading as well as when it is settling.
    """
    checkpoint = getattr(step, "checkpoint", None)
    return default_ms if checkpoint is None else checkpoint.timeout_ms


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
    trace = ctx.resolution_trace
    return StepResult(
        step_id=getattr(step, "id", 0),
        intent=intent,
        status=StepStatus.FAILED if failure is not None else StepStatus.SKIPPED,
        # The tier that was reached before it went wrong. A step that failed
        # *after* falling through to the recorded box and one that failed before
        # resolving at all are different diagnoses.
        resolution=trace.tier if trace is not None else ResolutionTier.NONE,
        duration_ms=int(monotonic_ms() - began),
        # A step that failed after three attempts and one that failed on the first
        # are different diagnoses, and the failing step is exactly the one whose
        # record has to say so.
        attempts=ctx.attempt,
        note="; ".join(ctx.notes) or None,
        expected=failure.expected if failure is not None else _expected_of(step),
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
