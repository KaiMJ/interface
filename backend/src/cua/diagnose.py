"""Diagnosing a run that stopped on a screen nobody had declared.

The long tail of enterprise screens cannot be enumerated in advance. A capability
declares the outcomes it can detect, an application declares the conditions it
knows how to survive, and everything else arrives as `FAILURE` — which is the
right answer (§3.3: guessing in a banking application is worse than stopping) and
an expensive one, because every unforeseen screen costs an operator.

What decides whether this system is usable at scale is not how few unknown screens
there are. It is whether an unknown screen costs an escalation **once** or **every
time**. This is the once.

    a run fails ──► cua diagnose <run_id> ──► a typed proposal ──► a human
                                                                  applies it
                                                                     │
                              policies/<app>.yaml ◄──────────────────┘
                                     │
                     every capability on that application, at every
                     institution running it, now has an answer for it

Three rules make it safe to point a model at this, and they are the whole design:

**Healing is a proposal, never a repair.** This runs *after* the run is terminal,
over evidence on disk, with no session open. Replay is untouched — it still
constructs no model client and still stops rather than improvising. What the model
produces is a patch a person reads and applies, gated by the same review that
gates a recorded capability. A system that let a model edit its own guardrails
mid-run would have no guardrails.

**The detector is chosen, not written.** The model is shown the lines that were
actually on the failing screen, numbered, and returns *an index*. It cannot
propose a phrase the screen does not contain, because it never emits a phrase —
which turns "the model might hallucinate a detector" from a risk to be mitigated
into a sentence that cannot be expressed. Everything downstream verifies against
the same list.

**Then it is falsified.** A line that also appears in a successful run of the same
capability discriminates nothing — it is chrome, and a detector built on it would
report every success as that outcome. This is the rule `learn-outcome` already
uses, applied to a proposal instead of to a demonstration, and it is why the two
share `catalog.learn`'s comparison helpers rather than growing a second copy.

What this deliberately does not do is propose an *action*. A recovery's handler
comes from a human: the model may say "this looks like an interstitial and here is
the line that identifies it", never "click at 0.55, 0.5". And a condition on a
step that mutates is never proposed as a recovery, whatever the model thinks it
is — see `_downgrade`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog.learn import all_lines, final_lines, normalized_line

# What a diagnosis may conclude. Each maps to a different place the answer goes,
# which is the test of whether the taxonomy is real — a classification that
# produced the same patch as another one would be the same classification.
CLASSIFICATIONS = {
    "business_outcome": "a legitimate alternative answer the caller should branch on",
    "recoverable": "an interstitial or transient state the automation could clear itself",
    "app_error": "the application itself failed; nobody can fix it from here",
    "escalation": "a declared state only a human can clear, such as a dead session",
    "drift": "the application moved; the recording needs re-recording, not a detector",
    "our_bug": "nothing is wrong with the application; the failure is in this system",
}

# Where each one lands. `drift` and `our_bug` land nowhere: they are answers about
# what to do next, not conditions to declare, and emitting a patch for them would
# be inventing work.
_TARGET = {
    "business_outcome": "policy",      # detector shared by every flow on the app
    "recoverable": "policy",
    "app_error": "policy",
    "escalation": "policy",
    "drift": None,
    "our_bug": None,
}

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "classification": {"type": "string", "enum": list(CLASSIFICATIONS)},
        "line": {
            "type": "integer",
            "description": (
                "Index of the line on the failing screen that identifies this "
                "condition. Must be one of the numbered lines given. Choose the "
                "most specific line that would not appear on a normal screen of "
                "this application. Use -1 only for 'drift' or 'our_bug', where no "
                "line identifies anything."
            ),
        },
        "name": {
            "type": "string",
            "description": "snake_case name for the condition, e.g. account_dormant",
        },
        "description": {"type": "string", "description": "one sentence, for a reviewer"},
        "rationale": {"type": "string", "description": "why this classification and not another"},
    },
    "required": ["classification", "line", "name", "description", "rationale"],
}

_SYSTEM = """You are diagnosing why an automated run of a back-office banking
application stopped. You are not fixing it and you are not driving anything: you
are reading evidence after the fact and proposing one entry for a human to review.

You will be given the failure, the lines that were on the screen when it happened,
and what the application already knows how to handle. Decide which kind of
condition this is, and point at the line that identifies it.

Two things matter more than being helpful:

- Pick a line that is *specific to this condition*. A column header, a navigation
  label or an app name appears on every screen; using one as a detector would make
  every successful run report this condition instead.
- If nothing on the screen identifies a distinct condition — the run simply did
  not find what it was looking for, or the screen is an ordinary one — say `drift`
  or `our_bug` and use line -1. Proposing a condition that is not there is worse
  than proposing nothing, because a person will apply it."""


@dataclass(frozen=True)
class Diagnosis:
    """One proposal, plus everything needed to decide whether to trust it."""

    run_id: str
    classification: str
    rationale: str
    name: str = ""
    description: str = ""
    detector: str = ""
    # Where the patch belongs, or None when the classification is an answer about
    # what to do rather than a condition to declare.
    target: str | None = None
    patch: str = ""
    # Why a proposal the model made was refused. Present *and* recorded, because a
    # rejection is the more interesting half of the evidence: it is what shows the
    # falsification actually runs.
    rejected: str | None = None
    lines_offered: int = 0
    model: str = ""

    @property
    def actionable(self) -> bool:
        return self.rejected is None and self.target is not None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "classification": self.classification,
            "rationale": self.rationale,
            "name": self.name,
            "description": self.description,
            "detector": self.detector,
            "target": self.target,
            "patch": self.patch,
            "rejected": self.rejected,
            "lines_offered": self.lines_offered,
            "model": self.model,
            "actionable": self.actionable,
        }


@dataclass
class RunEvidence:
    """The parts of a finished run this reads. Loaded from disk, never from a session."""

    run_id: str
    status: str
    capability: str = ""
    failure: dict[str, Any] = field(default_factory=dict)
    screen: list[str] = field(default_factory=list)
    step_risk: str = "safe"


def load_run(evidence_dir: Path) -> RunEvidence:
    """Read a terminal run's evidence. Works on any run already on disk."""
    payload = json.loads((evidence_dir / "run.json").read_text())
    failure = payload.get("failure") or {}
    step_id = failure.get("step_id")
    risk = "safe"
    for step in payload.get("steps", ()):
        if step.get("step_id") == step_id:
            policy = step.get("policy") or {}
            risk = str(policy.get("effective_risk") or "safe")
    return RunEvidence(
        run_id=str(payload.get("run_id", evidence_dir.name)),
        status=str(payload.get("status", "")),
        capability=str(payload.get("capability") or payload.get("capability_ref") or ""),
        failure=failure,
        screen=final_lines(evidence_dir),
        step_risk=risk,
    )


def reference_lines(evidence_root: Path, capability: str, exclude: str) -> list[str]:
    """Every line any *successful* run of the same capability has ever read.

    The falsification side. Broad on purpose — a line has to be absent from all of
    them to count as identifying, and `catalog.learn` records what happens when
    this set is too narrow: comparing final frames alone taught the first attempt
    that the search page's hint text meant "member not found", because the
    successful run passed through that page on its way somewhere else.

    An empty set is not a failure; it means nothing has succeeded here yet and the
    proposal is correspondingly less checked. `Diagnosis.rejected` stays None and
    the reviewer is the only filter, which is stated rather than hidden.
    """
    lines: list[str] = []
    for run in sorted(evidence_root.glob("*/run.json")):
        if run.parent.name == exclude:
            continue
        try:
            payload = json.loads(run.read_text())
        except (OSError, ValueError):
            continue
        if payload.get("status") != "success":
            continue
        ref = payload.get("capability") or payload.get("capability_ref") or ""
        if capability and ref != capability:
            continue
        lines.extend(all_lines(run.parent))
    return lines


def prompt_for(run: RunEvidence, policy: Any) -> str:
    """What the model is shown: the failure, the screen, and what is already known.

    The declared conditions are included so it does not propose one the
    application already handles — a duplicate detector is not harmless, it is a
    second thing to keep in sync with the first.
    """
    known = [
        *(f"recovery: {r.name} — {r.detector_value!r}" for r in policy.recoveries),
        *(f"app error: {c.name} — {c.detector_value!r}" for c in policy.app_errors),
        *(f"escalation: {c.name} — {c.detector_value!r}" for c in policy.escalations),
        *(
            f"business outcome: {o.name} — {o.detector_value!r}"
            for o in policy.business_outcomes
        ),
    ]
    screen = "\n".join(f"{i}: {line}" for i, line in enumerate(run.screen))
    failure = run.failure
    return (
        f"Run {run.run_id} of capability {run.capability or '(unknown)'} ended as "
        f"{run.status}.\n\n"
        f"Failure kind: {failure.get('kind', '(none)')}\n"
        f"Step: {failure.get('step_id', '(none)')}\n"
        f"Message: {failure.get('message', '')}\n"
        f"Expected: {failure.get('expected') or '(nothing declared)'}\n"
        f"Observed: {failure.get('observed') or '(nothing readable)'}\n"
        f"The step that failed was classified {run.step_risk}.\n\n"
        f"Lines on the screen when it stopped:\n{screen or '(the screen read as blank)'}\n\n"
        f"Already declared for this application:\n"
        + ("\n".join(known) if known else "(nothing)")
    )


async def diagnose(
    run: RunEvidence,
    policy: Any,
    llm: Any,
    reference: list[str] | None = None,
) -> Diagnosis:
    """One model call over one run's evidence, validated into a proposal.

    Takes its collaborators rather than building them, so the whole of this is
    testable against a scripted model with no browser, no display and no
    application — which is also what makes the falsification rule assertable.
    """
    answer = await llm.structured(_SYSTEM, prompt_for(run, policy), _SCHEMA)
    classification = str(answer.get("classification", "our_bug"))
    if classification not in CLASSIFICATIONS:
        classification = "our_bug"
    classification, note = _downgrade(classification, run)

    base = Diagnosis(
        run_id=run.run_id,
        classification=classification,
        rationale=(note + str(answer.get("rationale", ""))),
        lines_offered=len(run.screen),
        model=getattr(llm, "model", ""),
    )

    target = _TARGET[classification]
    if target is None:
        return base

    index = int(answer.get("line", -1))
    if not 0 <= index < len(run.screen):
        return _refuse(base, f"chose line {index}, which is not one of the lines offered")
    detector = run.screen[index]

    seen = {normalized_line(line) for line in (reference or [])}
    if normalized_line(detector) in seen:
        return _refuse(
            base,
            f"{detector!r} also appears in a successful run of this capability, so "
            f"it identifies nothing — a detector on it would classify every success "
            f"as {answer.get('name', 'this condition')}",
        )

    name = _slug(str(answer.get("name", "")) or classification)
    description = str(answer.get("description", ""))
    return Diagnosis(
        run_id=base.run_id,
        classification=classification,
        rationale=base.rationale,
        name=name,
        description=description,
        detector=detector,
        target=target,
        patch=_patch(classification, name, description, detector),
        lines_offered=base.lines_offered,
        model=base.model,
    )


def _downgrade(classification: str, run: RunEvidence) -> tuple[str, str]:
    """A condition met on a step that mutates is never auto-recoverable.

    The model does not get a vote on this. `recoverable` means the automation
    clears the condition and carries on unattended, and "carries on unattended"
    past a step that may already have moved money is the one thing this system
    exists to not do. The same read/write line that gates retry (§3) gates the
    proposal, so a mislabelled recording cannot become a policy entry that lets
    the next run through.
    """
    if classification == "recoverable" and run.step_risk != "safe":
        return "escalation", (
            "proposed as recoverable and downgraded: the step it stopped on is "
            f"{run.step_risk}, and a condition met on a step that mutates is a "
            "person's to clear. "
        )
    return classification, ""


def _refuse(base: Diagnosis, why: str) -> Diagnosis:
    return Diagnosis(
        run_id=base.run_id,
        classification=base.classification,
        rationale=base.rationale,
        rejected=why,
        lines_offered=base.lines_offered,
        model=base.model,
    )


def _slug(name: str) -> str:
    cleaned = "".join(c if c.isalnum() else "_" for c in name.strip().casefold())
    return "_".join(part for part in cleaned.split("_") if part) or "unnamed"


def _patch(classification: str, name: str, description: str, detector: str) -> str:
    """The proposal, as the YAML a reviewer pastes into `policies/<app>.yaml`.

    Text rather than a file rewrite, and that is deliberate. A policy file is the
    most heavily commented artifact in this repository — every entry carries the
    argument for why it is classified the way it is — and a program that rewrites
    it with `yaml.safe_dump` deletes all of that. The unit of review here is a
    diff a person applies, which is also what keeps a model from ever being the
    thing that edits a guardrail.

    A `recoverable` proposal is emitted with its `actions` list empty and a marked
    TODO: the detector can be copied off a screen, but what to *do* about it
    cannot, and a handler nobody wrote is not something to guess at.
    """
    key = {
        "business_outcome": "business_outcomes",
        "recoverable": "recoveries",
        "app_error": "app_errors",
        "escalation": "escalations",
    }[classification]
    body = [
        f"{key}:",
        f"  - name: {name}",
        f"    description: {description}" if description else "",
        "    detector:",
        "      kind: text_present",
        f"      value: {json.dumps(detector)}",
    ]
    if classification == "recoverable":
        body += [
            "    # TODO(reviewer): what clears this? The detector was read off the",
            "    # screen; the handler cannot be, and is not guessed at here.",
            "    actions: []",
            "    max_per_run: 2",
        ]
    if classification == "business_outcome":
        body += [
            "    # Then declare it on each capability that can actually reach it:",
            "    #   business_outcomes: [{ name: " + name + " }]",
            "    # The app owns the detector; the capability owns whether its",
            "    # caller may receive this answer.",
        ]
    return "\n".join(line for line in body if line)
