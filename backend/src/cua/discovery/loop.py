"""The discovery loop: observe -> decide -> act -> record.

Runs until the goal's success condition holds or a stopping condition fires — max steps,
timeout, policy denial, dead end, or the model asking to escalate. The recording is a
side effect of acting rather than a separate pass: every accepted tool call is appended
as a typed step as it executes, with the observation it resolved against, so the
transcript and the artifact cannot disagree.
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
from ..perception.som import annotate, candidate_digest, truncated
from ..policy import PolicyDenied, RiskDisposition
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
    InterventionReason,
    InterventionRequest,
    ModelTurn,
    Observation,
    Phases,
    Point,
    PolicyDecision,
    Primitive,
    ResolutionTier,
    Risk,
    RunStatus,
    SettledBy,
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
    started_at: str = ""
    steps: list[Any] = field(default_factory=list)          # typed artifact steps
    results: list[StepResult] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    llm_calls: int = 0
    seen_frames: set[str] = field(default_factory=set)      # loop detection
    # Steps the loop recorded on the model's behalf — the entry navigation. The
    # model must do more than nothing before it may finish.
    seeded: int = 0

    # Handed back each turn, and the audit trail of what it tried that did not work.
    history: list[str] = field(default_factory=list)
    frame_sequence: list[str] = field(default_factory=list)
    last_call: tuple[str, str] | None = None
    repeated_calls: int = 0
    extracted: dict[int, str] = field(default_factory=dict)
    # The frame each recorded step acted on; what screen derivation would work from.
    step_frames: dict[int, Observation] = field(default_factory=dict)
    # Discovery splits roughly half perceiving, half waiting on the model, and only
    # one half is ours to optimise — so the split has to be visible.
    observe_ms: float = 0.0
    observation_count: int = 0
    observe_ms_at_last_step: float = 0.0
    observations_at_last_step: int = 0
    success_text: str = ""
    summary: str = ""
    escalation_reason: str = ""
    stop_reason: str = ""
    # What synthesis proposed, verbatim — the one part of an artifact a model wrote
    # freehand, so a reviewer can see what was proposed and what was rejected.
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
        control: Any = None,
        max_steps: int = 30,
        max_llm_calls: int = 60,
        wall_clock_ms: int = 600_000,
        settle_timeout_ms: int = 8000,
        settle_poll_ms: int = 120,
        vnc_url: str = "",
    ) -> None:
        self.perceiver = perceiver
        self.driver = driver
        self.policy = policy
        self.evidence = evidence
        self.llm = llm
        # The same token replay uses. Discovery is supervised, but "supervised" is
        # not "may do anything", and a guardrail binding only the unattended path has
        # a hole shaped exactly like a recording session.
        self.control = control
        self.max_steps = max_steps
        self.max_llm_calls = max_llm_calls
        self.wall_clock_ms = wall_clock_ms
        self.settle_timeout_ms = settle_timeout_ms
        self.settle_poll_ms = settle_poll_ms
        # Where an operator connects to take over this exact session.
        self.vnc_url = vnc_url
        # Discovery resolves too: the find tools locate their own scope with the same
        # ladder replay uses. A recording built against a different resolver works
        # only on the run that made it.
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
        started = now_iso()
        state = DiscoveryState(run_id=self.evidence.run_id, goal=goal, started_at=started)
        self._deadline = monotonic_ms() + self.wall_clock_ms
        self.evidence.open()
        # So a run is visible while it happens, not only once it ends.
        self.evidence.result(
            DiscoveryResult(
                run_id=state.run_id,
                goal=goal,
                app=self.policy.app,
                status=RunStatus.RUNNING,
                model=getattr(self.llm, "model", ""),
                started_at=started,
                evidence_dir=str(self.evidence.dir),
            )
        )

        self.llm.preflight()
        self.goal_inputs = inputs

        try:
            self.policy.check_url(start_url)
            await self.driver.navigate(start_url)
            # Recorded, not just performed. Otherwise the artifact begins wherever
            # the browser happened to be — measured: a second invocation in the same
            # session started on the previous run's member profile and failed at step
            # 1. A capability establishes its own entry point.
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

                # Step 1 of the artifact is step 1 of the log, recorded here so it
                # carries the frame the run actually started on. With an empty one it
                # rendered as a black panel — the first thing anyone watching looks at.
                if not state.results:
                    self._record(
                        state,
                        step_id=1,
                        intent=f"entry point; the run started at {start_url}",
                        status=StepStatus.OK,
                        evidence=self.evidence.frame(obs, 1),
                        policy=self.policy.decide(
                            Primitive.NAVIGATE, Risk.SAFE, "navigate to the entry point"
                        ),
                    )

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
            app=self.policy.app,
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

    async def _intervene(
        self,
        state: DiscoveryState,
        reason: InterventionReason,
        message: str,
        step_id: int,
        intent: str,
        evidence: Evidence,
    ) -> Any:
        """Park the recording session and hand it to a human.

        The same mechanism replay uses, on the same control token and the same
        live browser — the operator takes over mid-recording, does whatever is
        needed, and gives it back. Returns the resolution, or None when this loop
        was built without a control token (the offline tests), in which case the
        caller treats it as a refusal: the safe reading of "nobody can be asked"
        is not "proceed anyway".
        """
        if self.control is None:
            return None
        request = InterventionRequest(
            id=f"int_{uuid4().hex[:8]}",
            run_id=state.run_id,
            mode="discovery",
            capability=None,
            goal=state.goal,
            reason=reason,
            step_id=step_id,
            step_intent=intent,
            message=message,
            evidence=evidence,
            vnc_url=self.vnc_url or None,
            raised_at=now_iso(),
        )
        self.evidence.intervention(request)
        resolution = await self.control.escalate(request)
        self.evidence.intervention(request, resolution)
        return resolution

    async def _step(self, state: DiscoveryState, obs: Observation) -> bool:
        """One observe-decide-act cycle. Returns False when the run should stop."""
        step_id = len(state.steps) + 1
        # Into scratch, not evidence: the writer copies it in under the name the
        # manifest expects, and two names leave a reviewer wondering which was shown.
        overlay = annotate(obs, self._scratch / f"step-{step_id:02d}.som.png")
        evidence = self.evidence.frame(obs, step_id, annotated=overlay)

        dropped = truncated(obs.elements, MAX_CANDIDATES)
        system = prompts.system_prompt(
            frozenset(a.value for a in self.policy.allowed_actions),
            getattr(self.policy, "surface", ""),
        )
        turn_prompt = prompts.turn(
            goal=state.goal,
            inputs=self.goal_inputs,
            candidates=candidate_digest(obs.elements, MAX_CANDIDATES),
            history=state.history,
            truncated=dropped,
        )
        # Once per run, not once per step: it does not change, and a copy on every
        # step would be most of the evidence directory.
        if step_id == 1:
            self.evidence.note(
                "prompt_system",
                {"model": getattr(self.llm, "model", ""), "system": self._for_evidence(system)},
            )
        asked = monotonic_ms()
        # A model call is the longest thing in a step — measured at 70s on one turn
        # of the recording run — and until it returns there is nothing on disk for
        # the console to tail, so a live run looks like a stalled one. This is the
        # only record written *before* an outcome exists; it is a heartbeat, not
        # evidence, and it is superseded by the step whose id it carries.
        self.evidence.note(
            "thinking",
            {
                "step_id": step_id,
                "since": now_iso(),
                "model": getattr(self.llm, "model", ""),
            },
        )
        call = await self.llm.decide(
            system=system,
            messages=[{"role": "user", "content": turn_prompt}],
            tools=tools.tool_definitions(frozenset(a.value for a in self.policy.allowed_actions)),
            image_path=overlay,
        )
        state.llm_calls = self.llm.calls
        # What it was shown and what it answered. `intent` is the model's gloss on
        # itself; this is the turn.
        turn = ModelTurn(
            call=call.name,
            text=str(getattr(call, "text", "") or ""),
            reasoning=str(getattr(call, "reasoning", "") or ""),
            prompt=self._for_evidence(turn_prompt),
            arguments=dict(call.input),
            intent=str(call.input.get("intent", "")),
            expect=str(call.input.get("expect", "")) or None,
            mark=_as_int(call.input.get("mark")),
            anchor_proposed=str(call.input.get("anchor", "")) or None,
            candidates_shown=min(len(obs.elements), MAX_CANDIDATES),
            candidates_truncated=dropped,
            latency_ms=int(monotonic_ms() - asked),
        )
        signature = (call.name, repr(sorted(call.input.items())))
        state.repeated_calls = state.repeated_calls + 1 if signature == state.last_call else 0
        state.last_call = signature

        if call.name == "finish":
            return self._finish(state, obs, call, evidence, turn)
        if call.name == "escalate":
            why = str(call.input.get("reason", "the model asked for help"))
            # The model asking for help is a step of the run. It used to leave
            # nothing behind but a line of history the model itself was shown.
            self._record(
                state,
                step_id=step_id,
                intent=f"asked for help: {why}",
                status=StepStatus.SKIPPED,
                evidence=evidence,
                turn=turn.model_copy(update={"verdict": "escalated", "detail": why}),
                began=asked,
            )
            resolution = await self._intervene(
                state,
                reason=InterventionReason.AGENT_REQUESTED,
                message=why,
                step_id=step_id,
                intent="the model asked for help",
                evidence=evidence,
            )
            if resolution is None or resolution.outcome == "abort":
                state.status = RunStatus.ESCALATED
                state.escalation_reason = why
                state.stop_reason = f"the model escalated: {why}"
                state.history.append(f"- escalated: {why}")
                return False
            # Tell the model what the human did: it is looking at a screen it did not
            # produce, and without that line its next action is reasoned from a state
            # it never saw.
            state.history.append(
                f"- you asked for help ({why}). A human took the session and "
                f"resolved it{': ' + resolution.note if resolution.note else ''}. "
                f"The screen may have moved; look again before acting."
            )
            return True

        return await self._act(state, obs, call, step_id, evidence, turn)

    def _record(
        self,
        state: DiscoveryState,
        step_id: int,
        intent: str,
        status: StepStatus,
        evidence: Evidence,
        turn: ModelTurn | None = None,
        began: float | None = None,
        expected: str | None = None,
        observed: str | None = None,
        policy: PolicyDecision | None = None,
        resolution: ResolutionTier = ResolutionTier.NONE,
    ) -> StepResult:
        """Append one step result and stream it to evidence.

        Every turn goes through here, including the ones that produced no artifact
        step — a rejected mark, a policy refusal, a discarded action. Those used to
        exist only as a line of narration handed back to the model, which meant the
        step log a human reads was a filtered version of what actually happened,
        filtered by the thing being debugged.
        """
        # Perceiving done since the previous step, attributed here because that is
        # when it was paid for.
        spent = state.observe_ms - state.observe_ms_at_last_step
        frames = state.observation_count - state.observations_at_last_step
        state.observe_ms_at_last_step = state.observe_ms
        state.observations_at_last_step = state.observation_count
        # Rewritten after every step. Discovery wrote it once at the start and once
        # at the end, which is why a run in progress showed no steps until it
        # finished — the console tails this file, and there was nothing to tail.
        result = StepResult(
            step_id=step_id,
            intent=intent,
            status=status,
            phases=Phases(observe_ms=int(spent), observations=frames),
            resolution=resolution,
            settled_by=state.observations[-1].settled_by if state.observations else SettledBy.UNSET,
            duration_ms=int(monotonic_ms() - began) if began is not None else 0,
            expected=expected,
            observed=observed,
            policy=policy,
            model_turn=turn,
            evidence=evidence,
        )
        state.results.append(result)
        self.evidence.step(result)
        self.evidence.result(self._snapshot(state))
        return result

    def _snapshot(self, state: DiscoveryState) -> DiscoveryResult:
        """The run as it stands, for the file a watcher is tailing."""
        return DiscoveryResult(
            run_id=state.run_id,
            goal=state.goal,
            app=self.policy.app,
            status=RunStatus.RUNNING,
            model=getattr(self.llm, "model", ""),
            steps_taken=len(state.steps),
            llm_calls=state.llm_calls,
            steps=state.results,
            started_at=state.started_at,
            evidence_dir=str(self.evidence.dir),
        )

    def _for_evidence(self, text: str) -> str:
        """Prompt text on its way to disk, through the same filter as everything else.

        `EvidenceWriter.step` writes a `StepResult` verbatim, so a prompt field is
        written exactly as handed over. Credentials never reach here — they are
        resolved in the action layer, below the serialization boundary — but a run's
        own inputs are all over a turn prompt, and the redactor is what the frames
        and the logs are already filtered through.
        """
        redactor = getattr(self.evidence, "redactor", None)
        if redactor is None:
            return str(text or "")
        return str(redactor.redact_text(str(text or "")))

    async def _act(
        self,
        state: DiscoveryState,
        obs: Observation,
        call: Any,
        step_id: int,
        evidence: Evidence,
        turn: ModelTurn,
    ) -> bool:
        """Execute one tool call, then keep it only if its expectation came true."""
        began = monotonic_ms()

        def reject(why: str) -> bool:
            state.history.append(f"- rejected {call.name}: {why}")
            self._record(
                state,
                step_id=step_id,
                intent=str(call.input.get("intent", call.name)),
                status=StepStatus.FAILED,
                evidence=evidence,
                turn=turn.model_copy(update={"verdict": "rejected", "detail": why}),
                began=began,
                observed=why,
            )
            return True

        primitive = tools.PRIMITIVES.get(call.name)
        if primitive is None:
            return reject("not an action on this surface")

        element = _element_for(obs, call.input.get("mark"))
        if primitive in (Primitive.CLICK, Primitive.TYPE, Primitive.EXTRACT) and (
            call.name not in tools.FIND_TOOLS and element is None
        ):
            return reject(f"mark {call.input.get('mark')!r} is not on this screen")

        # What the mark actually was, measured off the observation rather than taken
        # from the model's description of it. The join the recording is built from,
        # and how a reviewer judges a step they did not watch.
        if element is not None:
            turn = turn.model_copy(
                update={"element_id": element.id, "element_label": element.label}
            )

        intent = str(call.input.get("intent", call.name))
        try:
            step = tools.to_step(
                call.name,
                call.input,
                element,
                step_id,
                obs,
                identifiers=[str(v) for v in self.goal_inputs.values()],
                extracted=[str(v) for v in state.extracted.values()],
            )
        except ValueError as e:
            return reject(str(e))

        recorded_anchor = getattr(getattr(step, "target", None), "anchor_text", None)
        recorded_expect = getattr(getattr(step, "checkpoint", None), "value", None)
        turn = turn.model_copy(
            update={"anchor_recorded": recorded_anchor, "expect_recorded": recorded_expect}
        )
        # Told, not silently dropped. The model chooses an expectation for the next step
        # too, and a run that refutes the same mistake five times without ever saying so
        # is a prompt that never gets the chance to work.
        if turn.expect and not recorded_expect:
            state.history.append(
                f"- NOTE — {turn.expect!r} was not recorded as a checkpoint: it is this "
                f"record's data, not the application's own words, so it would be false "
                f"for every other input. Expect a heading, a panel title, or a label."
            )

        # The same policy object replay uses, on the same primitive, before the
        # action rather than after it. A guardrail that only guards the LLM is not
        # a guardrail, and one that only guards replay is not either.
        decision = self.policy.decide(primitive, step.risk, intent)
        try:
            if decision.disposition == "denied":
                raise PolicyDenied(decision.rule or "policy", f"{decision.detail} ({intent})")
            if call.name == "navigate":
                self.policy.check_url(str(call.input.get("url", "")))
        except PolicyDenied as e:
            state.history.append(f"- refused by policy: {intent} ({e})")
            self._record(
                state,
                step_id=step_id,
                intent=intent,
                status=StepStatus.FAILED,
                evidence=evidence,
                turn=turn.model_copy(update={"verdict": "rejected", "detail": str(e)}),
                began=began,
                observed=str(e),
                policy=decision,
            )
            return True
        disposition = decision.disposition

        # A risky action is risky whoever is driving. Replay parks here; so does
        # discovery. The alternative — letting a recording session submit a
        # transfer because a human is notionally watching a VNC window — is a
        # guardrail that binds only the path that needs it least.
        if disposition == RiskDisposition.CONFIRM:
            resolution = await self._intervene(
                state,
                reason=InterventionReason.RISKY_ACTION_CONFIRMATION,
                message=f"risky action needs confirmation before it is recorded: {intent}",
                step_id=step_id,
                intent=intent,
                evidence=evidence,
            )
            if resolution is None or resolution.outcome == "abort":
                state.status = RunStatus.ESCALATED
                state.stop_reason = f"a human declined the risky action: {intent}"
                state.escalation_reason = state.stop_reason
                return False
            state.history.append(
                f"- a human approved the risky action: {intent}"
                + (f" ({resolution.note})" if resolution.note else "")
            )

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
            self._record(
                state,
                step_id=step_id,
                intent=intent,
                status=StepStatus.FAILED,
                evidence=evidence,
                turn=turn.model_copy(
                    update={
                        "verdict": "discarded",
                        "detail": "the action had no effect and the expectation was wrong",
                    }
                ),
                began=began,
                expected=expect,
                observed=_summary(after),
                policy=decision,
            )
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

        # kept-with-checkpoint, or kept-with-the-checkpoint-dropped. Until now this
        # verdict survived only as prose in the artifact's `note`, so a reviewer
        # asking "which steps could the run not verify?" had to read every step.
        turn = turn.model_copy(
            update={
                "verdict": "kept_without_checkpoint" if unmet else "kept",
                "detail": step.note if unmet else None,
            }
        )

        state.steps.append(step)
        state.step_frames[step.id] = obs
        # What the step achieved, in the model's own terms. An extraction that
        # does not report the value it read leaves the model unable to tell a
        # successful read from a silent one — and it extracts again.
        read = state.extracted.get(step.id)
        outcome = f" -> read {read!r}" if read else (f" (saw {expect!r})" if expect else "")
        state.history.append(f"{len(state.steps)}. {intent}{outcome}")
        self._record(
            state,
            step_id=step_id,
            intent=intent,
            status=StepStatus.OK,
            evidence=evidence,
            turn=turn,
            began=began,
            expected=expect or None,
            observed=state.extracted.get(step_id),
            policy=decision,
            resolution=(
                ResolutionTier.ANCHOR_TEXT if element is not None else ResolutionTier.NONE
            ),
        )
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
        self,
        state: DiscoveryState,
        obs: Observation,
        call: Any,
        evidence: Evidence,
        turn: ModelTurn,
    ) -> bool:
        """The model says the goal is met. Believe it only if the screen agrees."""
        success_text = str(call.input.get("success_text", "")).strip()
        state.summary = str(call.input.get("summary", ""))
        step_id = len(state.steps) + 1

        def refused(why: str) -> bool:
            state.history.append(f"- {why}")
            self._record(
                state,
                step_id=step_id,
                intent="claimed the goal was reached",
                status=StepStatus.FAILED,
                evidence=evidence,
                turn=turn.model_copy(update={"verdict": "rejected", "detail": why}),
                expected=success_text or None,
                observed=_summary(obs),
            )
            return True

        if success_text and not evaluate(
            Checkpoint(kind=CheckKind.TEXT_PRESENT, value=success_text), obs
        ):
            return refused(
                f"tried to finish claiming {success_text!r} is on screen; it is not. "
                f"The screen reads: {_summary(obs)}"
            )
        if len(state.steps) <= state.seeded:
            return refused("tried to finish without having done anything")

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
        began = monotonic_ms()
        obs: Observation = await asyncio.to_thread(
            self.perceiver.settle,
            Path(self.evidence.dir) / ".frame.png",
            self.settle_timeout_ms,
            self.settle_poll_ms,
        )
        state.observe_ms += monotonic_ms() - began
        state.observation_count += 1
        state.observations.append(obs)
        return obs


def _as_int(value: Any) -> int | None:
    """A mark the model sent, as an int, or None. A malformed one is a bad turn."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
