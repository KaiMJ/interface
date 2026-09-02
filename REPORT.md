# Design write-up

This project records a successful computer-use run as a typed capability, then replays that
capability without an LLM. It is implemented against a deliberately non-semantic mock
credit-union back office. The [README](README.md) runs the demo; [evidence](evidence/README.md)
indexes the recorded runs.

## 1. Architecture

The system has two paths. Discovery is an observe → decide → act loop: it captures the X display,
uses OCR and UI detection to produce a surface-neutral `Element` list, asks an LLM to choose a
numbered mark, and records the resulting action. Synthesis turns that trace into a draft
capability. Replay uses the same perception and action layers but has no LLM client.

The model is shown a set-of-marks screenshot and returns exactly one tool call per turn. It never
emits a coordinate, only a mark number, so a misread pixel cannot become a click. Which model
answers is a one-line config change routed through LiteLLM: nothing in the loop depends on the
answer coming from a particular provider.

Playwright supplies browser input only; it is not used for DOM locators. A `Perceiver` produces
elements and a `Driver` executes input, so the artifact, resolver, policy, and replay engine do
not depend on a web DOM. The browser runs in a persistent X session, which is also what noVNC
shows an operator during an escalation.

The trade-off is latency, and it is not marginal: text recognition is roughly 95% of a replay's
wall clock, 2.7s per observation on a CPU against 0.8s on a GPU for identical output. It is
intentional because the target environment includes framesets, inaccessible legacy web apps, and
desktop software, where there is no cheaper surface to read.

The target application is built rather than borrowed, for two reasons the brief makes unavoidable.
It is hostile in the ways back-office systems are — no `data-testid` anywhere, generic anchor
labels, table-shaped detail screens — so perception is doing real work instead of being handed a
clean DOM it would never meet in production. And the exceptional states have to be reachable on
demand: no public sandbox can be made to return "session expired", a permission denial, and a 500
on command, and a replay that only demonstrates the happy path demonstrates nothing. Faults are
injected by cookie, so a reviewer's own tab and the automation's session do not fight over them.
Two are chosen to separate failure modes that look alike: the banner shifts every coordinate below
it, which better targeting survives, and the modal moves nothing and lands on top, which no
targeting detects and only post-action verification catches.

## 2. Artifact schema

`Capability` is a versioned Pydantic schema with:

- app identity and a permitted base-URL pattern;
- typed inputs and outputs;
- ordered actions, risk, retry/error behavior, and checkpoints;
- semantic targets (`anchor_text`, relation, role/name, then recorded bounds);
- declared business outcomes and a final success condition; and
- provenance for the recording run and viewport.

Storage is one file per version, `<id>.v<n>.json`. No database: the version is in the filename,
so old versions are retained by construction rather than by a retention policy, and a reviewer
diffs two capabilities with `diff`.

The schema serves three readers: a caller sees a typed contract, a reviewer can inspect what will
be executed, and the replay engine receives enough information to operate without the model.
Targets resolve as a ladder: text and spatial relationship first, role/name second, recorded bounds
last. Falling to a lower tier is logged as a drift signal; failing to resolve stops the run.

Every action carries an `expect` the model wrote, and the prompt constrains what it may be: text
the application renders about itself, never text belonging to the record on screen. A checkpoint of
`$18,204.55` passes the recording run and fails every other member, so the record-once/replay-many
constraint is pushed into the prompt, where the proposal can still be checked — an expectation that
never appeared on the run's own frames is dropped and the step is kept without its assertion.

Discovery proposes prose, intent, and expectations, but coordinates, roles, types, and input
substitutions are measured or derived from the recording. New artifacts are drafts; an explicit
approval step is required before the agent-facing invocation API exposes one.

One judgement of the model's cannot be checked against the recording: a business-outcome detector
names wording on a screen the successful run never reached, so falsification can only reject one
that fires on the *successful* run. A plausible invention survives — a run here proposed
`"No matching members found"` where the application says `"No member matches the search criteria
entered."`, a detector that reads as declared and never matches. Survivors are therefore recorded
as `verified: false` and withheld from the agent-facing manifest. Outcomes the *application*
declares in `policies/<app>.yaml` are inherited by name instead, with the detector resolved from
policy on each run, which is how a freshly recorded capability returns `member_not_found` and
`permission_denied` without any promotion step.

## 3. Determinism & error handling

Replay constructs no model client and disables the resolver’s VLM tier. Before an action it checks
the URL, policy, control token, target resolution, and relevant runtime conditions; afterward it
waits for the declared checkpoint. There are no fixed sleeps: settling and checkpoints poll to
bounded timeouts.

Results distinguish four caller-visible states:

| Result | Meaning |
|---|---|
| `success` | The checkpoint held and declared outputs were extracted. |
| `business_outcome` | An expected answer such as member-not-found or permission-denied. |
| `escalated` | Automation paused and transferred the live session to a person. |
| `failure` | An unexpected, unrecoverable condition with step and evidence. |

The brief names three classes and the contract exposes four states, because recoverable
conditions are deliberately not one of them. A recovery either clears and the run continues —
visible to the caller only as a `note` on the step and an `attempts` count above one — or it does
not, and the result is a failure. A caller branching on "recovered" would be doing the engine's job.

The application policy declares recoveries such as dismissing a maintenance notice, waiting for a
slow load, and restarting a read-only flow after session expiry. It also declares conditions that
must stop (`app_error`) or escalate. A safe action may retry; a risky action is never replayed
automatically.

## 4. Heterogeneity & multi-tenant

The seam for another surface is `Screen`/detector plus `Driver`: a desktop driver or an
accessibility-tree perceiver can produce the same `Element` abstraction, leaving the artifact and
replay engine unchanged. Offline replay against recorded PNGs exercises that separation.

Policies are per application, not per tenant. They hold allowlists, sign-on behavior, risk rules,
and runtime conditions. A capability records an application identity and URL pattern; a deployment
supplies its own entry URL. Recorded navigations are rebased onto that entry URL so a flow recorded
at one institution cannot silently run against another.

Drift is detected by the resolver ladder rather than by a separate monitor. Every step records
which tier produced its coordinate, so a target that used to resolve on `anchor_text` and now
resolves on `recorded_bbox` is a tenant whose labels have moved — reported before anything fails.
Run the same artifact across many tenants and that column is the signal: degradation is per-tenant
and legible in aggregate, and the tenant whose steps have fallen to recorded bounds is the one to
re-record.

This is deliberate detection rather than automatic adaptation: a tenant-specific label change fails
an anchored target and produces evidence rather than a guess. Acting on the signal is the next
step — per-tenant screen overrides and calibration are designed for, not implemented.

## 5. Escalation & handoff

Escalation occurs for a declared escalation condition, an ambiguous/stuck run, a step configured
to escalate, or a risky action whose policy requires confirmation. A control token has exactly one
holder: automation yields before publishing the intervention, a human claims it over the control
plane and operates the same X session through noVNC, then releases it for automation to resume.

The driver checks that token before every input event. The run waits on an in-process event rather
than destroying the session, so browser state, cookies, and a partial form remain intact. Evidence
captures the request, handoff and handback frames, resolution, and human actions; typed manual
text is stored only as a count.

The queue and control plane are intentionally single-process and unauthenticated. They demonstrate
the control-transfer seam, not a multi-operator production service.

## 6. Safety

Both discovery and replay use the same policy enforcement. It fails closed on URLs and action
types, rechecks the URL after navigation, and keeps the target application separate from the
control plane. Each recorded step has a declared risk; intent patterns can only promote risk, and
the policy can allow, block, or require human confirmation.

Credentials are sign-on configuration rather than artifact values. Declared sensitive inputs are
masked from serialized results. Screenshot/OCR pattern masking is deliberately not complete in
this take-home and is the primary safety limitation before a real-data deployment.

## 7. Cuts

The implemented vertical slice is a read capability discovered by an LLM, deterministic replay,
runtime-condition handling, and a live escalation path. Two stretch goals are in, both cheap
because the schema already had to carry a typed contract and a status: the catalog is exposed as an
agent-facing tool manifest over HTTP, and unattended replay is gated on draft → approved.

The transfer fixture exercises risky handoff, but it is hand-authored rather than discovered.
Desktop input and non-visual perceivers are interfaces rather than end-to-end implementations.
There is no capability router, durable control service, automated tenant specialization, or LLM
recovery inside replay.

`cua learn-outcome` teaches the remaining case by demonstration — replay with the recorded inputs,
replay with yours, take the detector from the difference — but it cannot tell a message from data.
Where the application states the condition, the difference is the app's own wording; where it does
not, the difference is that member's row. Asked to learn "this member has no account with that
nickname", it returned `"Transaction History — Account 41220"`, an account number that would only
ever match the one member it was shown. The falsification step rejects a detector that fires on the
successful run; it does not ask whether the survivor is a sentence or a row.

With more time, I would first complete evidence redaction, then record a write capability and add
cross-input/tenant checks that detect unstable extractions and candidate outcome detectors — the
same check that would refuse the account number above.
