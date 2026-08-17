"""The discovery loop: observe -> decide -> act -> record.

Runs until the goal's success condition is met or a stopping condition fires
(max steps, timeout, policy denial, dead end, or the model asking to escalate).

The recording is a side effect of acting, not a separate pass. Every accepted
tool-call is appended as a typed step at the moment it is executed, together with
the observation it was resolved against. That means the transcript and the
artifact cannot disagree, and it means a run that fails at step 9 still leaves
eight verified steps behind for a human to inspect.

A step is only accepted if its `expect` came true. The model states, before
acting, what it believes the screen will say afterwards; the loop checks that
against the next frame. A step whose expectation failed is discarded, the model is
told what is actually on screen, and it tries again. That is what "replayable by
construction" means concretely: every checkpoint in a saved artifact has already
been verified once, on the run that wrote it.

Termination is bounded on three axes independently — steps, wall clock, and LLM
calls — because they fail differently. A model stuck in a two-action cycle burns
steps; a model waiting on a hung page burns wall clock; a model retrying a
malformed tool call burns neither but costs money.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..clock import monotonic_ms, now_iso
from ..perception import Unsettled
from ..perception.som import annotate, candidate_digest, truncated
from ..policy import PolicyDenied
from ..replay.scan import Scanner, Untestable
from ..resolve import Resolver, Unresolvable, evaluate, region_text
from ..schema import (
    ActStep,
    CheckKind,
    Checkpoint,
    DiscoveryResult,
    Element,
    Evidence,
    FailureDetail,
    FailureKind,
    FindAndActStep,
    Observation,
    Point,
    Primitive,
    ResolutionTier,
    RunStatus,
    StepResult,
    StepStatus,
)
from . import actions as tools
from . import prompts

MAX_CANDIDATES = 80
STUCK_REPEATS = 3


@dataclass
class DiscoveryState:
    """Everything the loop accumulates. Serialized to evidence on every step, so a
    crashed run is still inspectable."""

    run_id: str
    goal: str
    steps: list[Any] = field(default_factory=list)          # typed artifact steps
    results: list[StepResult] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    llm_calls: int = 0
    seen_frames: set[str] = field(default_factory=set)      # loop detection
    # Steps the loop recorded on the model's behalf — the entry navigation. The
    # model has to do more than nothing before it may finish.
    seeded: int = 0

    # Narration handed back to the model each turn, and the audit trail of what
    # it tried that did not work.
    history: list[str] = field(default_factory=list)
    frame_sequence: list[str] = field(default_factory=list)
    last_call: tuple[str, str] | None = None
    repeated_calls: int = 0
    extracted: dict[int, str] = field(default_factory=dict)
    # The frame each recorded step acted on. Screens are derived from these at
    # synthesis: a state of the application is a set of steps that saw the same
    # thing.
    step_frames: dict[int, Observation] = field(default_factory=dict)
    success_text: str = ""
    summary: str = ""
    escalation_reason: str = ""
    stop_reason: str = ""
    # What synthesis proposed, verbatim. Kept because the declaration is the one
    # part of an artifact a model wrote freehand, and a reviewer approving the
    # capability should be able to see what was proposed and what was rejected.
    declaration: dict[str, Any] = field(default_factory=dict)
    status: RunStatus = RunStatus.FAILURE
    failure: FailureDetail | None = None


class DiscoveryLoop:
    def __init__(
        self,
        perceiver: Any,
        driver: Any,
        policy: Any,
        evidence: Any,
        llm: Any,
        max_steps: int = 30,
        max_llm_calls: int = 60,
        wall_clock_ms: int = 600_000,
        settle_timeout_ms: int = 8000,
        settle_poll_ms: int = 120,
    ) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.policy = policy
        self.evidence = evidence
        self.llm = llm
        self.max_steps = max_steps
        self.max_llm_calls = max_llm_calls
        self.wall_clock_ms = wall_clock_ms
        self.settle_timeout_ms = settle_timeout_ms
        self.settle_poll_ms = settle_poll_ms
        # Discovery resolves too — `find_and_act` has to locate its own scope, and
        # it does so with the same ladder replay will use. Building the recording
        # with a different resolver than the one that will execute it is how an
        # artifact ends up working only on the run that made it.
        self.resolver = Resolver(allow_vlm=False)
        self.scanner = Scanner(perceiver, driver, self.resolver)
        self._deadline = 0.0
        self._scratch = Path(tempfile.mkdtemp(prefix="cua-discovery-"))

    # -----------------------------------------------------------------------

    async def run(self, goal: str, start_url: str, inputs: dict[str, Any]) -> DiscoveryResult:
        """Drive the surface until the goal is met.

        `inputs` are the concrete values used for this run (member id, amount).
        They matter beyond just being typed into fields: they are what the
        synthesis pass looks for when deciding which literals in the recording are
        parameters. See `synthesize.py`.
        """
        state = DiscoveryState(run_id=self.evidence.run_id, goal=goal)
        started = now_iso()
        self._deadline = monotonic_ms() + self.wall_clock_ms
        self.evidence.open()

        self.llm.preflight()
        self.goal_inputs = inputs

        try:
            self.policy.check_url(start_url)
            await self.driver.navigate(start_url)
            # Recorded, not just performed. Without it the artifact begins wherever
            # the browser happened to be, and replays only from the state the
            # recording started in — measured: a second invocation in the same
            # session started on the previous run's member profile and failed at
            # step 1 with a target mismatch. A capability establishes its own
            # entry point.
            state.steps.append(
                ActStep(
                    id=1,
                    action=Primitive.NAVIGATE,
                    value=start_url,
                    note="entry point; the run started here",
                )
            )
            state.seeded = len(state.steps)

            while True:
                if len(state.steps) >= self.max_steps:
                    state.stop_reason = f"reached the step budget of {self.max_steps}"
                    state.failure = FailureDetail(
                        kind=FailureKind.MAX_STEPS, message=state.stop_reason
                    )
                    break
                if self.llm.calls >= self.max_llm_calls:
                    state.stop_reason = f"reached the model-call budget of {self.max_llm_calls}"
                    state.failure = FailureDetail(
                        kind=FailureKind.MAX_STEPS, message=state.stop_reason
                    )
                    break
                if monotonic_ms() > self._deadline:
                    state.stop_reason = "ran out of wall-clock budget"
                    state.failure = FailureDetail(
                        kind=FailureKind.TIMEOUT, message=state.stop_reason
                    )
                    break

                obs = await self._observe(state)
                stuck = self._is_stuck(state)
                if stuck:
                    state.stop_reason = stuck
                    state.status = RunStatus.ESCALATED
                    state.escalation_reason = stuck
                    break

                if not await self._step(state, obs):
                    break

        except PolicyDenied as e:
            state.stop_reason = str(e)
            state.failure = FailureDetail(kind=FailureKind.POLICY_DENIED, message=str(e))
        except Unsettled as e:
            state.stop_reason = str(e)
            state.failure = FailureDetail(kind=FailureKind.TIMEOUT, message=str(e))
        except Exception as e:  # noqa: BLE001 - a discovery run must still report
            state.stop_reason = f"{type(e).__name__}: {e}"
            state.failure = FailureDetail(kind=FailureKind.INTERNAL, message=state.stop_reason)

        result = DiscoveryResult(
            run_id=state.run_id,
            goal=goal,
            status=state.status,
            stop_reason=state.stop_reason,
            model=getattr(self.llm, "model", ""),
            steps_taken=len(state.steps),
            llm_calls=self.llm.calls,
            steps=state.results,
            failure=state.failure,
            started_at=started,
            finished_at=now_iso(),
            evidence_dir=str(self.evidence.dir),
        )
        self.evidence.result(result)
        self.state = state
        return result

    # -----------------------------------------------------------------------

    async def _step(self, state: DiscoveryState, obs: Observation) -> bool:
        """One observe-decide-act cycle. Returns False when the run should stop."""
        step_id = len(state.steps) + 1
        # Into scratch, not into evidence: the writer copies it in under the name
        # the manifest expects, and writing it twice under two names is how a
        # reviewer ends up wondering which one the model saw.
        overlay = annotate(obs, self._scratch / f"step-{step_id:02d}.som.png")
        evidence = self.evidence.frame(obs, step_id, annotated=overlay)

        call = await self.llm.decide(
            system=prompts.system_prompt(
                frozenset(a.value for a in self.policy.allowed_actions),
                getattr(self.policy, "surface", ""),
            ),
            messages=[
                {
                    "role": "user",
                    "content": prompts.turn(
                        goal=state.goal,
                        inputs=self.goal_inputs,
                        candidates=candidate_digest(obs.elements, MAX_CANDIDATES),
                        history=state.history,
                        truncated=truncated(obs.elements, MAX_CANDIDATES),
                    ),
                }
            ],
            tools=tools.tool_definitions(frozenset(a.value for a in self.policy.allowed_actions)),
            image_path=overlay,
        )
        state.llm_calls = self.llm.calls
        signature = (call.name, repr(sorted(call.input.items())))
        state.repeated_calls = state.repeated_calls + 1 if signature == state.last_call else 0
        state.last_call = signature

        if call.name == "finish":
            return self._finish(state, obs, call, evidence)
        if call.name == "escalate":
            state.status = RunStatus.ESCALATED
            state.escalation_reason = str(call.input.get("reason", "the model asked for help"))
            state.stop_reason = f"the model escalated: {state.escalation_reason}"
            state.history.append(f"- escalated: {state.escalation_reason}")
            return False

        return await self._act(state, obs, call, step_id, evidence)

    async def _act(
        self,
        state: DiscoveryState,
        obs: Observation,
        call: Any,
        step_id: int,
        evidence: Evidence,
    ) -> bool:
        """Execute one tool call, then keep it only if its expectation came true."""
        began = monotonic_ms()
        primitive = tools.PRIMITIVES.get(call.name)
        if primitive is None:
            state.history.append(f"- rejected {call.name}: not an action on this surface")
            return True

        element = _element_for(obs, call.input.get("mark"))
        if primitive in (Primitive.CLICK, Primitive.TYPE, Primitive.EXTRACT) and (
            call.name != "find_and_act" and element is None
        ):
            state.history.append(
                f"- rejected {call.name}: mark {call.input.get('mark')!r} is not on this screen"
            )
            return True

        intent = str(call.input.get("intent", call.name))
        try:
            step = tools.to_step(
                call.name,
                call.input,
                element,
                step_id,
                obs,
                identifiers=[str(v) for v in self.goal_inputs.values()],
            )
        except ValueError as e:
            state.history.append(f"- rejected {call.name}: {e}")
            return True

        # The same policy object replay uses, on the same primitive, before the
        # action rather than after it. A guardrail that only guards the LLM is not
        # a guardrail, and one that only guards replay is not either.
        try:
            self.policy.check_action(primitive, step.risk, intent)
            if call.name == "navigate":
                self.policy.check_url(str(call.input.get("url", "")))
        except PolicyDenied as e:
            state.history.append(f"- refused by policy: {intent} ({e})")
            state.results.append(
                StepResult(
                    step_id=step_id,
                    intent=intent,
                    status=StepStatus.FAILED,
                    duration_ms=int(monotonic_ms() - began),
                    observed=str(e),
                    evidence=evidence,
                )
            )
            return True

        try:
            await self._execute(state, step, obs, call, element)
        except (Unresolvable, Untestable) as e:
            state.history.append(f"- {intent}: could not be carried out ({e})")
            return True

        after = await self._observe(state)
        if primitive is not Primitive.EXTRACT:
            # Reads do not move the screen, and counting them as actions that had
            # no effect is how a run that is reading a value gets reported as
            # stuck. Measured: a model that extracted a balance twice was
            # escalated for making no progress while doing exactly what it was
            # asked to do.
            state.frame_sequence.append(after.frame_hash or "")
        expect = str(call.input.get("expect", "")).strip()
        unmet = bool(expect) and not evaluate(
            Checkpoint(kind=CheckKind.TEXT_PRESENT, value=expect), after
        )
        changed = after.frame_hash != obs.frame_hash

        if unmet and not changed:
            # Nothing happened. The action had no effect and the model's
            # expectation was wrong about that, so there is no step here worth
            # replaying — it is discarded, and the model is told what is actually
            # on screen so its next attempt is different.
            state.history.append(
                f"- attempted: {intent}\n"
                f"  DISCARDED — nothing changed, and the screen does not show {expect!r}. "
                f"It reads: {_summary(after)}"
            )
            state.results.append(
                StepResult(
                    step_id=step_id,
                    intent=intent,
                    status=StepStatus.FAILED,
                    duration_ms=int(monotonic_ms() - began),
                    expected=expect,
                    observed=_summary(after),
                    evidence=evidence,
                )
            )
            self.evidence.step(state.results[-1])
            return True

        if unmet:
            # The screen *did* change: the action had an effect, and only the
            # prediction about it was wrong. Dropping the step would leave the
            # recording missing a state transition the flow actually made, and
            # replay would then start from a screen it never reaches — which is
            # exactly what a discarded "click View" produced before this branch
            # existed. So the step is kept and its checkpoint is not: an assertion
            # the run could not verify has no business in an artifact.
            step = step.model_copy(
                update={
                    "checkpoint": None,
                    "note": (
                        f"recorded without a checkpoint: the run expected {expect!r} here "
                        f"and the screen showed something else. Review before approving."
                    ),
                }
            )
            state.history.append(
                f"- {intent}\n"
                f"  NOTE — you expected {expect!r}; the screen changed but reads: "
                f"{_summary(after)}"
            )

        state.steps.append(step)
        state.step_frames[step.id] = obs
        # What the step achieved, in the model's own terms. An extraction that
        # does not report the value it read leaves the model unable to tell a
        # successful read from a silent one — and it extracts again.
        read = state.extracted.get(step.id)
        outcome = f" -> read {read!r}" if read else (f" (saw {expect!r})" if expect else "")
        state.history.append(f"{len(state.steps)}. {intent}{outcome}")
        result = StepResult(
            step_id=step_id,
            intent=intent,
            status=StepStatus.OK,
            resolution=ResolutionTier.ANCHOR_TEXT if element is not None else ResolutionTier.NONE,
            duration_ms=int(monotonic_ms() - began),
            expected=expect or None,
            observed=state.extracted.get(step_id),
            evidence=evidence,
        )
        state.results.append(result)
        self.evidence.step(result)
        return True

    async def _execute(
        self,
        state: DiscoveryState,
        step: Any,
        obs: Observation,
        call: Any,
        element: Element | None,
    ) -> None:
        """Carry out the action the model chose.

        Clicks go to the chosen element's centre directly: the model picked from an
        enumerated list, so there is nothing to resolve. The artifact still records
        the anchor text and the box, which is what replay resolves against later.
        """
        if isinstance(step, FindAndActStep):
            found = await self.scanner.scan(step, self.goal_inputs, lambda: self._observe(state))
            if not found.matches:
                raise Unresolvable(
                    "no row matched "
                    + ", ".join(step.predicate.terms)
                    + (" (the list was still changing)" if found.inconclusive else "")
                )
            row = found.matches[0]
            if step.on_found_action is Primitive.EXTRACT:
                state.extracted[step.id] = " ".join(
                    (e.text or e.name or "").strip() for e in row
                ).strip()
                return
            box = row[0].bbox
            await self.driver.click(
                Point(x=min(1.0, box.x + box.w / 2), y=min(1.0, box.y + box.h / 2))
            )
            return

        assert isinstance(step, ActStep)
        point = (
            Point(x=element.bbox.center.x, y=element.bbox.center.y) if element is not None else None
        )

        if step.action is Primitive.NAVIGATE:
            await self.driver.navigate(step.value or "")
        elif step.action is Primitive.KEY:
            await self.driver.key(step.value or "Enter")
        elif step.action is Primitive.SCROLL:
            await self.driver.scroll(Point(x=0.5, y=0.5), float(step.value or 0.8))
        elif step.action is Primitive.CLICK and point is not None:
            await self.driver.click(point)
        elif step.action is Primitive.TYPE and point is not None:
            await self.driver.click(point)
            await self.driver.type_text(step.value or "")
        elif step.action is Primitive.EXTRACT and element is not None:
            state.extracted[step.id] = region_text(obs, element.bbox) or (element.text or "")

        if step.action is not Primitive.EXTRACT:
            url = self.driver.current_url()
            if url:
                # A click can navigate. Checking after the fact is the only way to
                # notice when it navigated somewhere the allowlist forbids.
                self.policy.check_url(url)

    def _finish(
        self, state: DiscoveryState, obs: Observation, call: Any, evidence: Evidence
    ) -> bool:
        """The model says the goal is met. Believe it only if the screen agrees."""
        success_text = str(call.input.get("success_text", "")).strip()
        state.summary = str(call.input.get("summary", ""))
        if success_text and not evaluate(
            Checkpoint(kind=CheckKind.TEXT_PRESENT, value=success_text), obs
        ):
            state.history.append(
                f"- tried to finish claiming {success_text!r} is on screen; it is not. "
                f"The screen reads: {_summary(obs)}"
            )
            return True
        if len(state.steps) <= state.seeded:
            state.history.append("- tried to finish without having done anything")
            return True

        state.success_text = success_text
        state.status = RunStatus.SUCCESS
        state.stop_reason = state.summary or "goal reached"
        return False

    def _is_stuck(self, state: DiscoveryState) -> str | None:
        """Detect a dead end before the step budget runs out.

        Signals, cheapest first: the frame hash has repeated N times (the actions
        are having no effect); the same tool-call has been issued twice in a row
        with the same arguments; resolution has failed on consecutive steps.

        Detecting this early matters because the alternative is an escalation that
        arrives at max-steps with twenty near-identical screenshots attached — the
        operator has to work out what went wrong, instead of being told.
        """
        # Post-action frames only. Counting every observation would flag a model
        # that is reading the screen before deciding — which is not stuck, it is
        # working — and the signal we want is specifically "the actions are having
        # no effect".
        recent = state.frame_sequence[-STUCK_REPEATS:]
        if len(recent) == STUCK_REPEATS and len(set(recent)) == 1:
            return (
                f"the screen has not changed across {STUCK_REPEATS} actions; "
                f"the run is not making progress"
            )
        if state.repeated_calls >= 2:
            return "the model repeated the same action three times without effect"
        return None

    async def _observe(self, state: DiscoveryState) -> Observation:
        obs: Observation = await asyncio.to_thread(
            self.perceiver.settle,
            Path(self.evidence.dir) / ".frame.png",
            self.settle_timeout_ms,
            self.settle_poll_ms,
        )
        state.observations.append(obs)
        return obs


def _element_for(obs: Observation, mark: Any) -> Element | None:
    """Marks are indices into the observation, so a reply is looked up rather than
    interpreted. An out-of-range mark is a bad turn, not a crash."""
    if mark is None:
        return None
    try:
        index = int(mark)
    except (TypeError, ValueError):
        return None
    return obs.elements[index] if 0 <= index < len(obs.elements) else None


def _summary(obs: Observation, limit: int = 400) -> str:
    text = " ".join(t for t in ((e.text or "").strip() for e in obs.elements) if t)
    return text[:limit]
