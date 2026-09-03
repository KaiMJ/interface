# cua

An LLM discovers how to complete a goal by driving an application's UI; what it learned
replays without it. Nothing in this package knows which application it is driving — that
is a YAML file in `policies/` and a screenshot.

```
  api/  cli.py            control plane — an agent, an operator, the console
      │
  runtime/                composition root, session lifecycle
      │
      ├── discovery/      a model decides          ─┐
      │                                             ├─ catalog/  the artifact between them
      └── replay/         the artifact decides     ─┘
      │
  resolve/                "the View button on Marcus Webb's row" -> a coordinate
      │
      ├── perception/     screen  -> elements
      └── action/         elements -> input
      │
  policy/  evidence/      guardrails and audit, across every layer
      │
  schema/                 typed contracts; depends on nothing
```

---

## 1) The two paths (discovery/ · replay/)

Both drive the same stack below `resolve`; they differ in what decides the next step.

- **discovery/** — a screenshot with numbered boxes goes to the model, one tool call
  comes back. Every step is verified against the next frame before it is recorded.
- **replay/** — the artifact decides. Steps in order, each with a checkpoint that is
  polled rather than slept on, plus a scan loop for "find the row that matches" and
  outcome classification for the difference between a failure and a business answer.

## 2) The surface seam (perception/ · action/)

Two narrow protocols, symmetric, both speaking normalized `0..1` display coordinates.

- **perception/** — capture, detect controls, read text, fuse into one `Element` list;
  plus spatial queries, table reconstruction, and the numbered overlay discovery shows
  the model.
- **action/** — click, type, key, scroll, navigate. Playwright is used as an *input
  engine*, not a locator library: it moves a mouse to a coordinate that perception
  computed, and never queries the DOM.

## 3) The semantic layer (resolve/)

Turns what a step *means* into where to click, then checks it happened.

- Resolution tiers, weakest declared last, so a recorded coordinate is the fallback and
  never the first answer.
- Templates — the one place a caller's inputs enter a recorded string.
- Normalizers, declared per artifact rather than defaulted, so replay compares strings
  the way the recording did.
- Verification before and after the action.

## 4) What a human authors (policy/)

One YAML file per application, in `policies/`. Guardrails are per-app rather than
per-capability: a session-expiry interstitial can interrupt every flow on the app, and
duplicated handlers drift.

Carries the URL allowlist, the permitted primitives, risk disposition, recovery recipes,
declared app errors and escalations, the sign-on recipe, redaction patterns, and the one
sentence of prompt that is a fact about the application rather than about the loop.

## 5) What the system produces (catalog/ · evidence/)

- **catalog/** — capabilities as files: `artifacts/<id>.v<n>.json`, plus the manifest
  that is the agent-facing surface. No database; filename versioning retains old
  versions by construction.
- **evidence/** — one directory per run, written *as the run proceeds*, so a run that
  crashes at step 9 is still inspectable.

## 6) Business outcome or failure (the distinction the system is built around)

"No such member" is an answer the caller branches on; a broken automation is not. Telling them
apart is the whole point, and the hard part is that a capability is recorded from one
successful run — so it has seen exactly one path, and every business outcome is by definition
a screen that run never visited. The model sees every frame the run saw. It never sees the
others.

**Where declarations come from.** Three sources, each covering the last one's blind spot:

- *synthesis*, at discovery — proposes the alternatives, but guesses their wording, and
  falsification can only reject a detector that fires on the successful run, so a plausible
  invention survives.
- *policy*, authored once per app — the conditions every flow shares, inherited by name.
- *`learn-outcome` / `diagnose`* — the rest, read off the real screen after the fact.

`cua learn-outcome` is for a case you can reach with inputs: it replays the capability twice,
once with the recorded inputs and once with yours, and takes the detector from the longest
line on the second run's final screen that appears on none of the first run's. No model in it
— a diff. It refuses a capability with a risky step, because it runs the flow twice.

`cua diagnose <run>` is for a screen you cannot reproduce: it shows the model the lines that
were on it and asks which kind of condition it is and which line identifies it — an index,
never a phrase, so a detector the model invented is not expressible — then falsifies the pick
against successful runs. Emits YAML for a person.

Neither applies anything. One writes a draft version, the other a patch. A model that could
rewrite a guardrail is not a guardrail.

**How replay uses them.** Two doors, different in kind:

```
   the app announces it        classify() matches a declared detector
                               "No member matches the search criteria entered."

   the app says nothing        the scan exhausts the list without a match.
                               There is no wording to detect — a member without
                               the account simply has no row — so the outcome is
                               named on the step and raised directly.
```

Door one runs first in `classify`, before the step's checkpoint: evaluated the other way
round, "no member matches" arrives as a failed assertion an operator goes looking for and
never finds. Door two is gated on *how the region was found* — an empty scan only means "not
there" if the scope resolved on its anchor. Off a recorded box the loop may have been reading
a different table, and that fails rather than answering confidently about the wrong list.

Both are the same rule the resolution ladder follows: a recorded coordinate never concludes
something about data.

## 7) Human in the loop (escalation/)

Control transfer over the *same live session*. One token, one holder, as explicit state:
both parties are pointed at the same X display, and two actors clicking at once on a
banking screen is a race.

## 8) Single-purpose modules

- **calibration.py** — every tuned perception threshold, with the measurement that set
  it.
- **clock.py** — every timestamp in the system, so the auditable records agree on
  format.
- **config.py** — environment-driven settings, one place, no scattered `getenv`.
- **diagnose.py** — a run that stopped on an undeclared screen, turned into YAML for a
  person to apply. Never applied automatically: a model that could rewrite a guardrail
  is not a guardrail.
- **cli.py** — the demo path; the top-level README's commands map to these subcommands.

---

## Where to read next

| Folder | What its README covers |
|---|---|
| [`schema/`](schema/README.md) | The vocabulary everything else is built from |
| [`perception/`](perception/README.md) | Screenshot to elements, and how a frame settles |
| [`resolve/`](resolve/README.md) | Tiers, templates, normalizers, verification |
| [`discovery/`](discovery/README.md) | The loop, the action space, artifact synthesis |
| [`replay/`](replay/README.md) | Step execution, the scan loop, outcome classification |
| [`policy/`](policy/README.md) | What a policy file declares and where each field is read |
| [`escalation/`](escalation/README.md) | Control transfer and human-action capture |
| [`runtime/`](runtime/README.md) | Session lifecycle and the composition root |

`action/`, `api/`, `catalog/` and `evidence/` are small enough to read directly.

Design write-up: [`../../../REPORT.md`](../../../REPORT.md)
