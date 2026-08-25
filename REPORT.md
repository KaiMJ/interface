# Design write-up

An LLM discovers how to complete a goal in an application with no API, records what it learned as a
typed capability artifact, and replays it deterministically with no model in the decision loop.
`README.md` runs it; `diagram.html` walks one run through the code; `evidence/README.md` indexes the
runs that prove it.

## 1. Architecture

**Vision-first, everywhere.** Perception is a screenshot of the X display through an icon detector
(OmniParser) and OCR (PP-OCR), merged into one `Element` list; there is no `page.locator()` in the
codebase, because Playwright here is an input engine and never a locator library. So **nothing above
perception knows what kind of surface it is looking at** — a legacy frameset, a desktop app and a
React app all arrive as the same `Element` list, at ~0.9s per observation against ~50ms for a DOM
query: a race a DOM design wins today and loses the first time it meets an app without one. Accuracy
is not what the design rests on, though — every read is asserted against a checkpoint or bounded by a
declared output constraint, so the guarantee is not "OCR is right" but **"when OCR is wrong, the run
stops and says so."** One composition root builds both runners:
discovery gets a real LLM client, replay is handed collaborators that raise if it reaches for one, so
determinism is structural rather than promised. And **the discovery action space *is* the artifact's
step vocabulary**, so recordings are replayable by construction rather than by inference.

## 2. Artifact schema

Three readers at once, and the shape falls out of that: a **calling agent** needs typed inputs,
outputs and outcomes to branch on; a **human reviewer** must approve it without watching a video; the
**replay engine** must execute it with no model present.

```jsonc
{ "id": "cap_get_savings_balance", "version": 2, "status": "approved",
  "app": { "name": "targetapp", "base_url_pattern": "^http://targetapp:8080(/.*)?$" },
  "inputs":  [{ "name": "member_id", "type": "string", "constraints": {"pattern": "^[0-9]{5}$"} }],
  "outputs": [{ "name": "balance", "type": "number", "from_step": 4, "normalize": ["strip_currency"],
                "constraints": { "min": -1e6, "max": 1e8 } }],           // read it: is it possible?
  "steps": [{ "kind": "act", "id": 4, "action": "extract", "extract_as": "balance", "risk": "safe",
    "target": { "anchor_text": "{{account_nickname}}", "relation": "right_of", // tier 1: portable
                "role": null, "name": null, "bbox": {...} },                   // tiers 2 and 3
    "checkpoint": { "kind": "text_present", "value": "Available Balance" } }],
  "business_outcomes": [{ "name": "member_not_found" }] }    // detector inherited from app policy
```

A bare click track fails all three readers — a coordinate cannot be judged reversible, and rebranding
moves every pixel — so every step carries its *semantic intent*, and **targeting is a ladder, most
portable first**, with the winning tier recorded; `relation` is what makes it work at all, since a
form field is an empty box beside a label with no `for=` to follow. And **output constraints are not
decoration**: `18204.55` misread as `1820455` coerces to a valid float and passes every assertion,
while a declared range makes it `OUTPUT_REJECTED` instead.

## 3. Determinism & error handling

**No `sleep()`** — waiting is polling to the step's declared timeout, at **both ends of the step**,
because "not true yet" and "not true" are the same picture. Frames must **settle before resolving**,
**normalizers live in the artifact**, and there is **a checkpoint per step**, so a wrong click at step
3 fails at step 3 rather than a plausible wrong output at step 9.

| Class | Meaning | Caller does |
|---|---|---|
| `success` | checkpoint held, declared outputs extracted | uses `outputs` |
| `business_outcome` | a legitimate answer: "no such member", "permission denied" | branches on `outcome.name` |
| `escalated` | stopped, handed to a human, may resume | waits or gives up |
| `failure` | something we do not understand | pages someone, with evidence |

Conflating the first two is the mistake the brief names, so a business outcome exits 0, and
`FailureKind`'s 15 members each map to a different operator action. Detector order enforces the
taxonomy, in one function so it cannot be re-litigated per call site: **declared business outcomes**
first, since after the checkpoint "no such member" reads as a failed assertion rather than the answer
asked for; then **recoverable conditions**, checked *before* the step acts as well as after, because a
dialog that does not move the page leaves the recorded coordinate resolving and the click is eaten;
then **conditions with no handler** (`app_errors` stop, `escalations` park); then **the checkpoint**;
then **hard failure**. **Retry and ambiguity are gated on `risk`**:
a safe step may run twice, a risky one never does, and the residual is a misread that is *both*
type-valid and inside its bound.

## 4. Heterogeneity & multi-tenant

**The surface seam is `Perceiver` + `Driver`.** A legacy frameset or a desktop app is a new
`Screen`/`Detector` pair and a new `Driver`, with schema, resolver ladder and replay engine untouched
— and framesets need no handling at all, *because* nothing reads a DOM; `action/offline.py` proves the
seam by replaying a run's recorded PNGs with no browser and no display. **The unit of sharing is the
application, as configuration**: `policies/<app>.yaml` carries the allowlist, risk disposition,
declared conditions and sign-on recipe, and nothing in `backend/src` knows `targetapp` exists. Three
pieces let one artifact span tenants: `base_url_pattern` is a *pattern*, `entry_url` is the one
per-institution fact, and **a recorded URL is rebased onto it before every navigate** — without which
a capability recorded at riverside and replayed from lakeside navigates to riverside, passes the
allowlist, and reports success about the wrong credit union's member. Where that stops is
configuration rather than branding: "Current Balance" for "Available Balance" makes the anchor miss
and verification stop the run — **detected and refused, not repaired**. `Screen` is where per-tenant overrides belong, and the
honest gap — enforced, never derived.

## 5. Escalation & handoff

**Detecting stuck.** Four routes, three declared rather than inferred: `on_error`/`on_multiple:
escalate` on a step, a risky action under a `confirm` disposition, a declared `escalations` condition,
or discovery hitting a dead end. **Control is then a token with exactly one holder:**

```
AUTOMATION ──escalate──► NOBODY ──take_control──► HUMAN
     ▲                                              │
     └──────────── resume ◄──── NOBODY ◄────release ┘
```

`NOBODY` is the interval between the automation stopping and the operator connecting, and it makes
"the agent clicked while I was typing" impossible rather than unlikely; control is surrendered
*before* the request is published, and the check lives in the **driver**, before every input event, so
a path that forgot to yield still cannot act.
The run parks on an `asyncio.Event`, not unwound — browser, display, cookies and any half-filled form
survive, and the operator connects over noVNC to the identical pixels the resolver saw. **Human input
is captured at the X layer**, which a headful-Playwright design cannot do, and **typed text is
counted, never recorded**, since the operator may be entering a credential. **Handing back
re-observes** rather than trusting the parked frame, and the queue is in-process — the deliberate
mock, because a control token that outlives the session it coordinates is a token that lies.

## 6. Safety

Guardrails are checked in **one place on both paths** — discovery and replay call the identical
`Policy.check_action()` and `check_url()`, because a guardrail that only guards the LLM is not a
guardrail: a tampered artifact submits the wrong transfer just as well as a confused model does. The
**allowlist fails closed**, is re-checked after every action since a click can navigate, and omits the
control plane, the console and noVNC, because the agent runs on the same machine as its own operator
surface. **Approval** gates the unattended invoke route and only that one, since replaying a draft is
*how* it gets reviewed. **Risk** is declared per step and dispositioned per app, promotable
one-directionally: mislabelling a submit as safe is the expensive failure, while a read misclassified
as risky costs one confirmation.

**Redaction is where the line honestly is**, since a screenshot is simultaneously the evidence and the
model input. Declared sensitive values are redacted for real, because `InputSpec.sensitive` is a
declaration rather than a pattern guess; pattern masking is a wired **seam** that paints nothing onto
a frame, and the call sites existing is what matters. **Limits**: no dynamic risk scoring; **no
defence against prompt injection via page content**, which the allowlist bounds but does not prevent;
an in-process control token; an unauthenticated control plane; unsigned evidence written by the
process being audited — each deliberate for a single-operator deployment and wrong for anything
else.

## 7. Cuts

**Cut, with the seam left real.** The **desktop surface** stops at `action/desktop.py`, which
implements the driver protocol and documents the X-input path — that the perceiver needs no change at
all is the claim worth testing. **DOM / accessibility perception** is enum values with nothing behind
them: it would make the easy surface easier while teaching nothing about the hard one. The **operator
console** is real enough to run the system from and is not multi-operator; **authentication** and
**pattern masking** are cut on the same terms (§6); and a **router** picking a capability for a goal
is not ours to build, the manifest being the boundary, as are queues and clustering. **Gated LLM
recovery inside a replay** stays unreachable by construction, since `cua diagnose` gets the same
benefit off the hot path.

**Not done:** no discovery run has recorded a *write* capability — `cap_transfer_funds` is a
hand-written fixture whose escalation path runs against the live app, and its three `<select>`
elements are a real perception question rather than a gap in the plumbing.

**Next:** `cua learn-screens` — replay twice with different inputs and intersect the frames — turns
§4's multi-tenant story into a mechanism and gives per-tenant overrides somewhere to attach; then a
recorded write capability; then a second read on every declared output, so disagreement becomes
`EXTRACTION_UNSTABLE` rather than a confident wrong number.
