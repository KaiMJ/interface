# Computer-Use Automation

An LLM discovers how to complete a goal in a legacy application by driving its UI the way a
human operator would, then records what it learned as a typed, versioned **capability artifact**
that replays deterministically — no model in the decision loop.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how
> the AI agent invokes it in production.

Design write-up: [`REPORT.md`](REPORT.md) · How it runs, stage by stage: [`diagram.html`](diagram.html) ·
Brief: [`ASSIGNMENT.md`](ASSIGNMENT.md) · Runs that prove it: [`evidence/`](evidence/README.md) ·
What is next: [`PLAN.md`](PLAN.md)

---

## Layout

```
backend/      the automation system (Python)
  src/cua/
    schema/       typed contracts — depends on nothing
    perception/   screen -> elements   (OmniParser + PP-OCR, over a screenshot)
    action/       elements -> input    (Playwright as an input engine, not a locator library)
    resolve/      semantic target -> coordinate, plus pre/post verification
    discovery/    the LLM loop and artifact synthesis
    replay/       deterministic execution, scan loop, outcome classification
    policy/       allowlist, risk classification, redaction seam
    escalation/   control transfer and X-layer human-action capture
    evidence/     per-run logs, frames, observations
    catalog/      capability store + agent-facing tool manifest
    runtime/      session lifecycle, composition root
  scripts/      smoke checks: perception, the spine, replay, scan, recovery, escalation
targetapp/    the application under automation — mock credit-union back office
console/      operator + debug UI, embeds the live session over noVNC

policies/     what a human authors — one YAML file per application
artifacts/    what the system records — typed, versioned capabilities
evidence/     what it did — one directory per run, whatever the outcome
```

---

## Setup

**Prerequisites**: Docker + compose. For local (non-container) work: [uv](https://docs.astral.sh/uv/),
Node 20+, and pnpm.

```bash
cp .env.example .env
```

The model is routed through **LiteLLM**, so `CUA_MODEL` is a provider-qualified string and the
credential is whichever env var that provider expects. Set the one you're using:

```bash
CUA_MODEL=xai/grok-4                 # XAI_API_KEY
CUA_MODEL=anthropic/claude-opus-5    # ANTHROPIC_API_KEY
CUA_MODEL=openai/gpt-5               # OPENAI_API_KEY
```

The model must support **vision and tool calling** — the loop's entire input is a screenshot and
its entire output is one structured action. Needed for discovery only; replay never constructs a
model client at all.

```bash
docker compose up --build
docker compose exec desktop python3 scripts/fetch_models.py   # one-time, ~270MB into ./models
```

| | |
|---|---|
| http://localhost:8080 | target app — sign in as `teller01` (password in `.env.example`) |
| http://localhost:8080/dev | fault injection panel |
| http://localhost:3000 | operator console — start runs, watch them step by step, take over |
| http://localhost:8000/docs | control plane API |
| http://localhost:6080 | noVNC view of the automation's display — watch it work, or take over |

Or without Docker: `make install`, then `make targetapp` / `make console` / `make api` in
separate shells. `make help` lists everything.

---

## Demo path

Everything below runs inside the automation container, which is where the display and the
browser live:

```bash
docker compose exec desktop bash
```

**1. Discovery** — an LLM-driven run against the live surface, which emits a draft artifact

```bash
cua discover \
  --goal "open member 12345 and read the current balance of their Primary Savings account" \
  --input member_id=12345 \
  --input account_nickname="Primary Savings" \
  --capability-id cap_get_savings_balance
```

Watch it happen at http://localhost:6080. The run writes `artifacts/cap_get_savings_balance.v1.json`
and `evidence/discover-<id>/`, which holds the annotated frames the model actually saw.

**2. Replay** — the same flow, deterministically, with input parameters

```bash
cua replay cap_get_savings_balance \
  --input member_id=12345 \
  --input account_nickname="Primary Savings"
```

```json
{ "status": "success", "outputs": { "balance": 18204.55 }, ... }
```

No model is constructed on this path. `unset XAI_API_KEY` and it behaves identically — that is
the check that matters, because if the deterministic path needs a model it is not deterministic.

**3. Replay hitting a business outcome** — a member that does not exist

```bash
cua replay cap_get_savings_balance \
  --input member_id=99999 \
  --input account_nickname="Primary Savings"
```

```json
{ "status": "business_outcome", "outcome": { "name": "member_not_found", "fields": {"member_id": "99999"} } }
```

Exit code 0: this is an answer, not a crash. Member `44100` returns `permission_denied` — a
different answer again, from the same artifact and the same code path.

**3b. Replay hitting a runtime condition** — the same artifact against an injected fault

```bash
cua replay cap_get_savings_balance --input member_id=12345 \
  --input account_nickname="Primary Savings" --fault modal
```

```json
{ "status": "success", "steps": [ { "id": 1, "status": "recovered",
  "note": "cleared 'maintenance_notice' before acting" }, ... ] }
```

Dismissed *before* the step acts, which is the ordering that matters: the dialog does
not move the page, so a recorded coordinate still resolves to the control underneath
and a click into it is eaten. `--fault slow` is the other half — the step polls for
~3.5s rather than reporting drift. All three tiers in one command:

```bash
python3 scripts/smoke_recover.py      # modal, slow, expired — see evidence/README.md
```

**4. Replay hitting a hard failure** — an injected application error

Toggle `error500` at http://localhost:8080/dev (or pass `--fault error500`), then
replay member `12345` again:

```json
{ "status": "failure", "failure": { "kind": "app_error", "step_id": 1,
  "observed": "An unexpected error occurred" } }
```

Exit code 1, with the step, the expectation and what was on screen instead.

**5. Escalation** — a risky step hands the live session to a human

```bash
cua replay cap_transfer_funds --input member_id=12345 --input amount=25.00 ...
```

The transfer's submit step is `risky`, and the app policy's disposition is `confirm`, so the run
parks and raises an intervention. Then, as the operator:

```bash
curl localhost:8000/interventions
curl -X POST "localhost:8000/interventions/<id>/take?operator=you"
```

Take over at http://localhost:6080 — same browser, same session, same half-filled form. Anything
you do there is captured at the X layer. Hand back:

```bash
curl -X POST localhost:8000/interventions/<id>/resolve \
  -H 'content-type: application/json' \
  -d '{"outcome":"resume","operator":"you","note":"confirmed with the member"}'
```

The run resumes on the same session and finishes. `evidence/<run>/intervention/` holds the
request, the handoff and handback frames, what the operator did, and how they resolved it.

**6. Teaching it an outcome** — what "no such member" looks like, learned rather than guessed

A recording declares only the outcomes it can detect — synthesis refuses any that appear
on the successful run's own frames, so a fresh one often declares none. Show it one:

```bash
cua learn-outcome cap_get_savings_balance \
  --name member_not_found \
  --description "no member exists with that id" \
  --input member_id=99999
```

It replays with the recorded example inputs, replays again with yours, and takes the
detector from the difference — the app's own wording, off the screen that produces it:

```json
{ "capability": "cap_get_savings_balance@v2", "learned": "member_not_found",
  "detector": "No member matches the search criteria entered." }
```

v2 is a draft; v1 keeps working. Replaying v2 with `member_id=99999` now returns a typed
outcome instead of a failure.

**7. Diagnosing a screen nobody declared** — the long tail, made cheap

When a run stops on a state the application has never been told about, the answer is
a hard failure, which is correct and expensive. `diagnose` is how it costs an
escalation *once* rather than every time:

```bash
cua diagnose replay-e83d23ba
```

It reads that run's evidence, shows the model the failing screen's lines **numbered**,
and asks which kind of condition it is and which line identifies it. The model returns
an *index*, never a phrase, so an invented detector is not expressible — and the chosen
line is falsified against every successful run of the same capability first:

```json
{ "classification": "business_outcome", "detector": "This account is dormant and cannot be viewed.",
  "target": "policy", "actionable": true }
```

```yaml
business_outcomes:
  - name: account_dormant
    detector: { kind: text_present, value: "This account is dormant and cannot be viewed." }
```

YAML to paste, not an edit — a model that could rewrite a guardrail is not a guardrail.
Nothing here touches a session. A refused proposal is written to
`evidence/<run>/diagnosis.json` with its reason, which is what shows the falsification
runs.

Because the patch lands in the *application's* policy, every capability on that app
inherits it, at every institution running the product. A capability opts in by name:

```jsonc
"business_outcomes": [{ "name": "account_dormant" }]
```

**The catalog** — what an AI agent sees

```bash
cua catalog                                        # everything, with contracts
cua approve cap_get_savings_balance 1 --operator you
cua manifest                                       # approved capabilities as callable tools
```

Drafts never appear in the manifest: an agent able to call an unreviewed recording would be
running unapproved automation against member accounts.

---

## The console

Everything above is also doable from http://localhost:3000, and one thing is only doable there:
seeing *why* a step did what it did.

Four columns, no routing. **Start** — a goal in English with its inputs (Record) or a
recorded capability with typed inputs (Replay), plus the catalog and every run this
deployment has done. **Steps** — the run's log, streaming. **Evidence** — the frame and
the step taken apart. **Session** — the live display over noVNC, the escalation queue,
and the two contracts governing the run.

The frame carries three layers, and the third is the one worth knowing about:

| layer | what it is |
|---|---|
| capture | what an operator would have seen over VNC |
| marks | the numbered overlay the model was shown — where an argument about a decision is settled |
| elements | every box perception found, coloured by source, with role and confidence on hover |

Below it, the step inspector shows the four stages the step went through:

- **Decision** — which mark the model chose, *which element that mark actually was* (measured, not
  the model's description of it), how many candidates it was shown and how many were truncated
  away, how long it took, and what the loop then did with the answer: `kept`,
  `kept_without_checkpoint`, `discarded`, `rejected`.
- **Guardrail** — what policy decided *before* the step ran, allow as well as deny, and whether it
  promoted a step the recording declared `safe` to `risky` and on which pattern.
- **Resolution** — every rung of the resolver ladder and why each missed, not just the tier that
  won. "Fell through to the recorded box" and "the anchor matched three elements" are different
  applications and different fixes.
- **Verification** — expected against observed, how the frame settled, any recovery that fired.

Interventions live in the same page rather than behind an operator route: whoever
handles an escalation needs the evidence on screen, and a debug view and an operator
view differ only in whether you may touch it. Take control, work the session, write a
note, hand back or abort — the handoff and handback frames and every X-layer input
appear underneath.

A CLI run and a console run are the same thing here: it tails the run's own
`steps.jsonl` and `run.json` over SSE, so what it shows and what the audit trail says
cannot disagree. Any view is a URL — `?run=<id>&step=<n>`.

One display means one run: starting a second while one holds the session is refused with a 409
rather than queued, and the console greys the button and names the run that has it.

---

## Running against a different application

Nothing in `backend/src` knows the demo app exists. An application is **one YAML file**:

```bash
policies/<app>.yaml             # then: cua discover --app <app> --goal "..."
```

That file carries everything per-app: identity, entry URL, allowlist, permitted
primitives, risky disposition, recoveries, app errors, business-outcome detectors,
volatile text and the sign-on recipe. Copy `targetapp.yaml`, change it, touch no Python
— `tests/test_apps.py` asserts exactly that by standing up a second app from a temp
directory.

```bash
cua discover --app coreview --goal "..."     # selects policies/coreview.yaml
cua replay   cap_x                           # defaults to the capability's own app
cua catalog  --app coreview
```

Replay defaults `--app` to the capability's recorded `app.name`, so an artifact is always
executed under the guardrails of the application it was recorded against and cannot be run under
another's by accident. One display holds one application at a time — the X display is the
coordinate space, so a second app's browser on it would put two applications in one picture.

**Start a new application at `risky_disposition: block`.** You do not yet know what mutates
there. Loosen to `confirm` once you do.

**The same app at a second institution** is not a new policy file — it is one variable:

```bash
CUA_TARGET_BASE_URL=https://coreview.lakeside.example cua replay cap_x --input ...
```

`base_url_pattern` in the policy is a pattern rather than a literal precisely so one artifact is
valid against both installs. See REPORT §4 for how far that goes and where it stops.

**Point perception at an unfamiliar page before recording anything:**

```bash
python3 scripts/smoke_observe.py --url https://some-app.example/page \
    --expect "Account" --expect "Available Balance"
```

It reports rather than asserts: element count and frame time, how many detected controls
carry text (zero means the merge thresholds do not fit this surface), whether rows
reconstruct into single lines, whether the page settles on pixels or only text, and which
`--expect` strings are unreadable. It writes the frame and the overlay to `/tmp/smoke/`,
which usually answers the question faster than the checks do.

---

## Running without live services

Three levels, in order of how little they need.

**No browser, no model, no target app.** The full test suite — 213 tests covering the resolver
ladder, the error taxonomy, the scan loop's termination rules, control transfer, synthesis,
outcome learning and inheritance, diagnosis of an undeclared screen, screen identity, artifact
referential integrity, redaction of declared-sensitive inputs, per-app policy selection, and a
discover → synthesize → replay round trip — runs against fakes at the perception and action
seams:

```bash
make test
```

**No browser, no model, real pixels.** Replay a capability against a previous run's recorded
frames. The engine re-derives every decision from the same images: resolving targets, asserting
before each click, evaluating checkpoints and business outcomes.

```bash
cua replay cap_get_savings_balance \
  --frames evidence/replay-dd1bbee1/frames \
  --input member_id=12345 --input account_nickname="Primary Savings"
```

This proves the decision path is reproducible from pixels alone. It does **not** prove
the application responds the way it did, because nothing is being clicked.

**No model.** Every command above except `cua discover` runs with no credential configured.

---

## The target application

A mock credit-union back office at http://localhost:8080. Purpose-built rather than a public
sandbox for one reason: §3.3 asks replay to handle runtime conditions and §6.3 asks for evidence
of one, and you cannot make a public demo site return "session expired" on command.

**Business outcomes** are ordinary behaviour, reachable with ordinary inputs — no toggle:

| | |
|---|---|
| member not found | search `99999` |
| member has no savings account | member `30992` |
| permission denied | member `44100` (restricted) |
| insufficient funds | transfer more than the source account's *available* balance |
| over daily limit | transfer more than $5,000 |

**Faults** are injected at http://localhost:8080/dev, or with `cua replay --fault <name>`, and
are a deliberately separate category — conflating "a legitimate answer" with "an injected
failure" is the mistake the system exists to avoid:

| fault | expected handling |
|---|---|
| `banner` | variance — handled by anchor-relative resolution |
| `modal` | recoverable — dismissed before the step acts, so it never eats the click |
| `slow` | recoverable — the step polls for its target until its declared timeout |
| `expired` | escalate — a declared app condition a human must clear |
| `denied` | business outcome |
| `error500` | hard failure, `app_error` |
| `validation` | hard failure, with the fields shifted down |
| `confirm` | hard failure — an undeclared interstitial |

Faults live in a cookie so a reviewer's tab and the automation's browser do not fight
over them — which means arming one *for the automation* means arming it inside that
browser, where `curl` cannot reach. `--fault` drives the session through
`/api/faults?set=…` before the run starts. That route and `/dev` are excluded from the
allowlist: an agent that can arm its own faults can disarm them.

---

## Latency

Perception is the system's runtime: ~95% of a replay's wall clock is one text
recognition call per observation. Measured on the dense member screen, 1440×900:

```bash
docker compose exec desktop python3 scripts/bench_perception.py --frames /tmp/frames
```

| | dense screen |
|---|---|
| OCR, `CUA_OCR_ENGINE=torch` (GPU) | 886 ms |
| OCR, `CUA_OCR_ENGINE=onnxruntime` (CPU) | 2646 ms |
| control detection (GPU) | 37 ms |
| merge | 5 ms |

Compose defaults to `torch` because it reserves a GPU; the code default is
`onnxruntime`, so `make api` on a machine without one still works. Run
`scripts/fetch_models.py` once to cache the torch weights. `onnxruntime-gpu` is
deliberately not used — it ships CUDA 12 builds and this image needs CUDA 13 for
the detector, so its CUDA provider loads and then silently runs on the CPU.

Each step records where its own time went (`phases` on the step record, shown
under **cost** in the console's step inspector), including how many full
perceptions it paid for. One per step is the floor and the normal case: the frame
a step ends on is the frame the next step starts from.

---

## Checking the machinery

Seven smoke scripts, each answering one question, in the container:

```bash
python3 scripts/smoke_perception.py   # do the model weights load and run at all?
python3 scripts/smoke_observe.py      # does cua.perception see this surface? (takes --url)
python3 scripts/smoke_drive.py        # does a click resolved from screen text land on it?
python3 scripts/smoke_replay.py       # does one artifact produce three different results?
python3 scripts/smoke_scan.py         # does find_and_act handle found / absent / ambiguous?
python3 scripts/smoke_recover.py      # do the three runtime-condition tiers behave live?
python3 scripts/smoke_escalate.py     # does the handoff work on the live session?
```

`smoke_recover.py` is the one that exercises §3.3 end to end: it arms one fault at a time and
replays the same recorded capability against each, so an interstitial, a slow screen and a dead
session produce three different kinds of *not failing* — recovered, waited, and handed to a
human who signs back in on the same browser.

`smoke_scan.py` is the one that exercises the hardest step type: it finds a transaction by
merchant in a 22-row list, reads the amount out of the named column, returns a typed business
outcome when no row matches, and escalates rather than guessing when four rows do.

Two more that need nothing but the repository:

```bash
python3 scripts/index_evidence.py           # regenerate evidence/README.md's table
python3 scripts/index_evidence.py --check   # fail if that table names a run that is gone
```

`smoke_drive.py` is the one worth running first if something is wrong: it signs in by reading
labels off the screen, opens a member, reads a balance, and refuses a deliberately stale
coordinate — so a failure there localizes the problem to perception, resolution, or input.

**Source changes need the image rebuilt** (`docker compose build desktop`) or copied in
(`docker compose cp backend/src desktop:/app/`). `docker compose up -d desktop` recreates from
the image and silently discards a `cp`.
