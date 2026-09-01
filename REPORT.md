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

Playwright supplies browser input only; it is not used for DOM locators. A `Perceiver` produces
elements and a `Driver` executes input, so the artifact, resolver, policy, and replay engine do
not depend on a web DOM. The browser runs in a persistent X session, which is also what noVNC
shows an operator during an escalation.

The trade-off is latency: visual perception is slower than DOM queries. It is intentional here
because the target environment includes framesets, inaccessible legacy web apps, and desktop
software.

## 2. Artifact schema

`Capability` is a versioned Pydantic schema with:

- app identity and a permitted base-URL pattern;
- typed inputs and outputs;
- ordered actions, risk, retry/error behavior, and checkpoints;
- semantic targets (`anchor_text`, relation, role/name, then recorded bounds);
- declared business outcomes and a final success condition; and
- provenance for the recording run and viewport.

The schema serves three readers: a caller sees a typed contract, a reviewer can inspect what will
be executed, and the replay engine receives enough information to operate without the model.
Targets resolve as a ladder: text and spatial relationship first, role/name second, recorded bounds
last. Falling to a lower tier is logged as a drift signal; failing to resolve stops the run.

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

This is deliberate detection rather than automatic adaptation: a tenant-specific label change
causes an anchored target to fail and produces evidence. Per-tenant screen overrides and drift
calibration are a next step, not implemented infrastructure.

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
runtime-condition handling, and a live escalation path. The transfer fixture exercises risky
handoff, but it is hand-authored rather than discovered. Desktop input and non-visual perceivers
are interfaces rather than end-to-end implementations. There is no capability router, durable
control service, automated tenant specialization, or LLM recovery inside replay.

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
