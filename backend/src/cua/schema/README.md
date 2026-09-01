# Schema

Everything else in `cua` depends on these.

```
  common.py                 vocabulary (boxes, types, risk)
      │
      ├──► elements.py      what perception sees / produces
      ├──► artifact.py      what we recorded and can replay
      └──► results.py       what actually happened
                │
                └──► intervention.py   when a human took over
```

---

## 1) Common

- Bbox / Point / Viewport (0..1 of the recorded screen)
- ValueType / Normalizer / MatchMode (how values are typed and compared)
- Risk (safe or risky — can this be undone?)

## 2) Elements (what perception produces)

- Element (role / name / text / box)
- Observation (screenshot + every Element found in it)
- ElementSource (OCR, icon detector, DOM/ax)
- SettledBy (how we decided the screen had stopped moving)

## 3) Targeting — how one step finds its control and knows it worked (artifact.py)

- Target (anchor text → role + name → recorded box — most portable first)
    - Relation (the control is often not the thing with the words on it: a field is an
      empty box beside a label, a balance is the cell right of "Available Balance")
- Primitive (browser action primitives)
- StepBase
    - ActStep (one target: navigate / click / type / extract / wait)
    - FindAndActStep (when a target's position is a function of the data, not the layout)
- Checkpoint (asserts the expected state: text present, element visible, region stable)
- OnError (hard_fail / escalate / retry — and retry on a risky step is refused)

## 4) Artifact — the whole capability (artifact.py)

- Capability
    - goal / description / app (AppRef) / status (draft → approved → deprecated)
    - inputs (InputSpec) / outputs (OutputSpec) / business_outcomes
    - steps + screens + the final success Checkpoint
    - recording (provenance — which run, which model, when)
- InputSpec (`sensitive` values never reach an artifact)
- OutputSpec (typed output — so a misread digit is rejected)
- Constraints (shared by both: pattern / min / max / choices / not_equal_to). On an
  output this is the one field synthesis cannot fill in, and is authored at review time:
  a bound derived from a recording that saw one value is that value, or a guess.
- BusinessOutcome ("no such member")
- Screen (a recognisable state of the app)

`Capability` validates during initialization for
 - duplicate step ids
 - an output reading from a step that extracts nothing
 - an undeclared `{{placeholder}}`
 - `retry` on a risky step.

## 5) Results — what happened

- DiscoveryResult
    - has ModelTurn (what the model was shown, what it chose, and what we did with it)
- ReplayResult
    - has OutcomeDetail (the business outcome that fired)

Both have StepResult / RunStatus / FailureDetail.

- StepResult
    - StepStatus (ok / recovered / skipped / failed)
    - ResolutionTier (which text/box/name/... produced the coordinate)
    - ResolutionTrace (the full walk for one target)
    - PolicyDecision (what the guardrail decided)
    - Phases (time for observe / act / verify...)
    - Evidence (the frame it acted on, and the frame the action produced)
- FailureKind (why we stopped)

## 6) Intervention — when a human took over

- Controller (who)
- InterventionState (state)
- InterventionReason (why)
- InterventionRequest (what / config)
- HumanAction (at / kind / x,y / detail — typed text is counted, never stored)
- InterventionResolution (outcome / note / duration)

---

## End to end flow

```
   Observation        what perception saw
        │
        │  Target     how the step finds its control
        ▼             (anchor text → role + name → recorded box)
   Resolution         where that landed, and which control won
        │
        │  Primitive  browser actions
        ▼
   Observation′       the screen the action produced
        │
        │  a declared business outcome?  → outcome, run stops cleanly
        │  a condition policy knows?     → recovered, or app_error
        │  does the Checkpoint hold?     → ok
        ▼
   StepResult         all of the above, written down
```

## How a run ends

```
                      RunStatus
      ┌───────────┬──────────┬───────────┐
   SUCCESS    BUSINESS_   ESCALATED   FAILURE
      │        OUTCOME        │          │
   outputs     outcome   intervention  failure
```
