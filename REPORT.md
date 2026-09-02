# Design write-up

This project records a computer-use run as a typed capability, then replays it without an LLM. The target is a mock credit-union back office built to be hostile to easy automation — no test hooks, generic labels, framesets. The [README](README.md) runs the demo; [evidence](evidence/README.md) indexes the recorded runs.

## 1. Architecture

*Code: [`cua/`](backend/src/cua/README.md) — the layer map

**Two paths, shared perception.**

| Path | What it does |
|---|---|
| **Discovery** | Observe → decide → act loop. Captures the X display, runs OCR/UI detection into a surface-neutral `Element` list, asks an LLM to pick a numbered mark, records the action. Synthesis turns the trace into a draft capability. |
| **Replay** | Same perception and action layers, no LLM. |

**Why marks, not coordinates.** The model sees a set-of-marks screenshot and returns one tool call per turn — a mark number, never a pixel. A misread coordinate can't become a click. Model choice is a one-line LiteLLM config change; nothing in the loop depends on a specific provider.

**Why Playwright without DOM locators.** Playwright handles browser input only. A `Perceiver` produces elements; a `Driver` executes input. The artifact, resolver, policy, and replay engine never touch a web DOM. The browser runs in a persistent X session — the same surface noVNC shows during escalation.

**The latency trade-off.** Text recognition is ~95% of replay wall clock: 2.7s/observation on CPU vs 0.8s on GPU for identical output. The target environment includes framesets, inaccessible legacy web apps, and desktop software — there is no cheaper surface to read.

**Why we built the app.** Two reasons:

1. **Realistic hostility.** No `data-testid`, generic anchor labels, table-shaped detail screens. Perception does real work instead of reading a clean DOM that won't exist in production.
2. **Reachable fault states.** No public sandbox returns "session expired", a permission denial, and a 500 on command. A replay that only shows the happy path proves little. Faults are injected by cookie so a reviewer's tab and the automation session don't fight over them. Two faults separate failure modes that look alike: a banner shifts every coordinate below it (better targeting survives), a modal moves nothing and lands on top (only post-action verification catches it).

## 2. Artifact schema

*Code: [`schema/`](backend/src/cua/schema/README.md) — every type in the contract · [`discovery/`](backend/src/cua/discovery/README.md) — how one is synthesised*

`Capability` is a versioned Pydantic schema:

- app identity and permitted base-URL pattern
- typed inputs and outputs
- ordered actions with risk, retry/error behavior, and checkpoints
- semantic targets: `anchor_text`, relation, role/name, then recorded bounds
- declared business outcomes and a final success condition
- provenance for the recording run and viewport

**Storage.** One file per version: `<id>.v<n>.json`. No database — the version is in the filename, old versions are kept by construction, and a reviewer diffs two capabilities with `diff`.

**Three readers.** Callers get a typed contract. Reviewers can inspect what will execute. The replay engine gets enough to run without the model.

**Target resolution ladder.** Text and spatial relationship first, role/name second, recorded bounds last. Falling to a lower tier is logged as drift; failing to resolve stops the run. A target anchored on a caller's input doesn't fall through at all: a recorded box would click the member the *recording* saw, so a missed `{{member_id}}` stops the step rather than degrading to a confident wrong answer.

**Expectations.** Every action carries an `expect` the model wrote. The prompt restricts these to text the application renders about itself — never text from the record on screen. A checkpoint of `$18,204.55` passes the recording run and fails every other member. That pushes the record-once/replay-many constraint into the prompt, where proposals can still be checked: an expectation that never appeared on the run's frames is dropped and the step is kept without its assertion.

**What the model proposes vs. what is measured.** Discovery proposes prose, intent, and expectations. Coordinates, roles, types, and input substitutions come from the recording. New artifacts are drafts; an explicit approval step is required before the agent-facing API exposes one.

**Business outcomes the model can't verify.** A business-outcome detector may name wording on a screen the successful run never reached. Falsification can only reject detectors that fire on the *successful* run — a plausible invention survives. Example: a run proposed `"No matching members found"` where the app says `"No member matches the search criteria entered."` — reads as declared, never matches. Survivors are recorded as `verified: false` and withheld from the agent-facing manifest. Outcomes the *application* declares in `policies/<app>.yaml` are inherited by name instead, with the detector resolved from policy on each run. That's how a freshly recorded capability returns `member_not_found` and `permission_denied` without promotion.

## 3. Determinism & error handling

*Code: [`replay/`](backend/src/cua/replay/README.md) — step execution and classification · [`resolve/`](backend/src/cua/resolve/README.md) — the ladder, templates, verification*

Replay constructs no model client and disables the resolver's VLM tier. Before each action: URL, policy, control token, target resolution, relevant runtime conditions. Afterward: wait for the declared checkpoint. No fixed sleeps — settling and checkpoints poll to bounded timeouts.

**Four caller-visible states:**

| Result | Meaning |
|---|---|
| `success` | Checkpoint held; declared outputs extracted. |
| `business_outcome` | Expected answer — member-not-found, permission-denied, etc. |
| `escalated` | Automation paused; live session transferred to a person. |
| `failure` | Unexpected, unrecoverable condition with step and evidence. |

The brief names three classes; the contract exposes four because recoverable conditions are not one of them. That is a claim about the *run* status, not about visibility: a recovered step is typed `recovered`, names its handler in `recovery_applied`, and carries an `attempts` count above one — a step-level drift signal, since a surface that needs a handler it used not to need is changing. What a recovery never becomes is a terminal state the caller branches on. It either clears and the run continues, or it doesn't and the result is failure.

**Business outcomes are classified first.** A frame is tested for declared outcomes *before* the step's checkpoint. Evaluated the other way round, "no member matches" arrives as a failed assertion — a layout problem an operator goes looking for and never finds — instead of the answer the caller asked for.

**Where the declarations come from.** A detector needs the words the screen actually uses, and a capability recorded from one successful run has never seen the screens it has to detect. Synthesis can only guess at them, and falsification catches the wrong half: it rejects a detector that fires on the successful run, not one that never fires at all. Two offline tools read them off the real screen instead. `cua learn-outcome` replays a capability twice — with the recorded inputs, and with inputs that reach the other result — and takes the detector from the difference between the two final screens, so the wording is copied rather than invented. `cua diagnose <run>` works backwards from a run that stopped on an undeclared screen: it shows the model the lines that were on it and asks which kind of condition it is and which line identifies it — an index, never a phrase, so a detector the model made up is not expressible. Both emit a proposal for a person to apply: a new draft version for one capability, or a YAML patch for the whole application's policy. Not every outcome has wording to find: a scan that exhausts an account list without a match is an *absence*, and the screen it ends on says nothing about the condition — the member simply has no such row. Those are declared structurally instead, by naming the outcome on the step that raises it, and carry no detector at all.

**The trade-off.** A VLM could classify the frame mid-run instead and return the business outcome on the spot. Rejected, for the caller's sake rather than for determinism: an agent branches on the closed set of outcome names the manifest advertises, so a name minted during the run buys nothing on the run that pays for it — and a business answer the automation invented is worse than an honest failure, because nobody goes looking for it. The cost is real and it is paid once per condition: the first caller to hit an undeclared case still gets a failure, and a person has to approve the detector before the second one doesn't.

**Application policy** declares recoveries (dismiss maintenance notice, wait for slow load, restart after session expiry) and stop/escalate conditions (`app_error`). Safe actions may retry; risky actions are never replayed automatically.

## 4. Heterogeneity & multi-tenant

*Code: [`perception/`](backend/src/cua/perception/README.md) — the surface seam*

**The extension seam:** `Screen`/detector plus `Driver`. A desktop driver or accessibility-tree perceiver can produce the same `Element` abstraction, leaving the artifact and replay engine unchanged. Offline replay against recorded PNGs exercises this separation.

**Policies are per application, not per tenant.** They hold allowlists, sign-on behavior, risk rules, and runtime conditions. A capability records application identity and URL pattern; a deployment supplies its own entry URL. Recorded navigations are rebased onto that entry URL so a flow recorded at one institution can't silently run against another.

**Drift detection via the resolver ladder.** Every step records which tier produced its coordinate. A target that used to resolve on `anchor_text` and now resolves on `recorded_bbox` means labels moved — reported before anything fails. Run the same artifact across tenants and that column is the signal: degradation is per-tenant and legible in aggregate.

This is detection, not automatic adaptation. A tenant-specific label change fails an anchored target and produces evidence rather than a guess. Acting on the signal — per-tenant screen overrides, calibration — is designed for but not implemented.

## 5. Escalation & handoff

*Code: [`escalation/`](backend/src/cua/escalation/README.md) — control transfer and human-action capture*

Escalation triggers: declared escalation condition, ambiguous/stuck run, step configured to escalate, or risky action whose policy requires confirmation.

**Control token.** Exactly one holder at a time. Automation yields before publishing the intervention. A human claims it over the control plane and operates the same X session through noVNC, then releases it for automation to resume.

The driver checks the token before every input event. The run waits on an in-process event rather than destroying the session — browser state, cookies, and partial forms stay intact. Evidence captures the request, handoff/handback frames, resolution, and human actions; typed manual text is stored only as a count.

The queue and control plane are single-process and unauthenticated. They demonstrate the control-transfer seam, not a multi-operator production service.

## 6. Safety

*Code: [`policy/`](backend/src/cua/policy/README.md) — what a policy file declares and where each field is read*

Discovery and replay share the same policy enforcement: fail closed on URLs and action types, recheck URL after navigation, keep the target application separate from the control plane. Each step has declared risk; intent patterns can only promote risk; policy can allow, block, or require human confirmation. Nothing the model produces edits a guardrail directly: `learn-outcome` and `diagnose` emit a draft version or a policy patch, and a person applies it.

Credentials live in sign-on config, not artifact values. Declared sensitive inputs are masked from serialized results. Screenshot/OCR pattern masking is deliberately incomplete in this take-home — the primary safety limitation before a real-data deployment.

## 7. Cuts

**In scope:** read capability discovered by LLM, deterministic replay, runtime-condition handling, live escalation. Two stretch goals shipped because the schema already carried a typed contract and status: agent-facing tool manifest over HTTP, and draft → approved gating for unattended replay.

**Partially in:**
- Transfer fixture exercises risky handoff, but is hand-authored rather than discovered
- Desktop input and non-visual perceivers are interfaces, not end-to-end implementations

**Not in:** capability router, durable control service, automated tenant specialization, LLM recovery inside replay.

**Outcomes with no wording.** `cua learn-outcome` takes the longest line on the outcome screen the successful run never showed, which holds when the application announces the condition in a sentence and fails when it doesn't. Asked to learn "this member has no account with that nickname" it returned `"Debits shown in parentheses. Showing most recent 22 items."` — screen chrome carrying a per-member count. It could not have done better: that screen states nothing about the condition, so every line it could return is wrong. `cua diagnose` reached the same conclusion on the same case and correctly declined to propose a detector, which is what its index-only answer format is for. The case is declared structurally instead. What is missing is the step before: nothing proposes that declaration — synthesis never generated it, and `diagnose` has no classification for "a business outcome with no text to detect", so it answered `drift`.

**With more time:** complete evidence redaction, record a write capability, add cross-input/tenant checks that detect unstable extractions and candidate outcome detectors — the same check that would refuse the account number above.
