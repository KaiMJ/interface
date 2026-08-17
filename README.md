# Computer-Use Automation

An LLM discovers how to complete a goal in a legacy application by driving its UI the way a
human operator would, then records what it learned as a typed, versioned **capability artifact**
that replays deterministically — no model in the decision loop.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how
> the AI agent invokes it in production.

Design write-up: [`REPORT.md`](REPORT.md) · Brief: [`ASSIGNMENT.md`](ASSIGNMENT.md) ·
State and traps: [`HANDOFF.md`](HANDOFF.md) · What is next: [`PLAN.md`](PLAN.md)

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
  policies/     per-app guardrail config
  scripts/      smoke checks: perception, the spine, replay
targetapp/    the application under automation — mock credit-union back office
console/      operator + debug UI, embeds the live session over noVNC
artifacts/    saved capability artifacts (typed, versioned)
evidence/     per-run logs, screenshots, replay results
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
| http://localhost:3000 | operator console — runs, the frames the model saw, policy, handoff |
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

**4. Replay hitting a hard failure** — an injected application error

Toggle `error500` at http://localhost:8080/dev, then replay member `12345` again:

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

A recorded capability declares only the outcomes it can actually detect. Synthesis refuses any
the model proposed that appear on the successful run's own frames, so a fresh recording often
declares none. Show it one instead:

```bash
cua learn-outcome cap_get_savings_balance \
  --name member_not_found \
  --description "no member exists with that id" \
  --input member_id=99999
```

It replays the capability with its recorded example inputs, replays it again with yours, and
takes the detector from the difference — the app's own wording, copied off the screen that
produces it:

```json
{ "capability": "cap_get_savings_balance@v2", "learned": "member_not_found",
  "detector": "No member matches the search criteria entered." }
```

v2 is a draft; v1 keeps working. Replaying v2 with `member_id=99999` now returns a typed
outcome instead of a failure.

**The catalog** — what an AI agent sees

```bash
cua catalog                                        # everything, with contracts
cua approve cap_get_savings_balance 1 --operator you
cua manifest                                       # approved capabilities as callable tools
```

Drafts never appear in the manifest: an agent able to call an unreviewed recording would be
running unapproved automation against member accounts.

---

## Running without live services

Three levels, in order of how little they need.

**No browser, no model, no target app.** The full test suite — 116 tests covering the resolver
ladder, the error taxonomy, the scan loop's termination rules, control transfer, synthesis,
outcome learning, screen identity, and a discover → synthesize → replay round trip — runs against
fakes at the perception and action seams:

```bash
make test
```

**No browser, no model, real pixels.** Replay a capability against a previous run's recorded
frames. The engine re-derives every decision from the same images: resolving targets, asserting
before each click, evaluating checkpoints and business outcomes.

```bash
cua replay cap_get_savings_balance \
  --frames evidence/replay-12345-<id>/frames \
  --input member_id=12345 --input account_nickname="Primary Savings"
```

This proves the decision path is reproducible from pixels alone. It does **not** prove the
application responds the way it did, because nothing is being clicked — a green offline replay and
a green live replay say different things.

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

**Faults** are injected at http://localhost:8080/dev (or `POST /api/faults`), and are a
deliberately separate category — conflating "a legitimate answer" with "an injected failure" is
the mistake the system exists to avoid:

| fault | expected handling |
|---|---|
| `banner` | variance — handled by anchor-relative resolution |
| `modal` | recoverable — declared dismissal handler in policy |
| `slow` | recoverable — the checkpoint is polled until its declared timeout |
| `expired` | escalate — a declared app condition a human must clear |
| `denied` | business outcome |
| `error500` | hard failure, `app_error` |
| `validation` | hard failure, with the fields shifted down |
| `confirm` | hard failure — an undeclared interstitial |

---

## Checking the machinery

Three smoke scripts, each answering one question, in the container:

```bash
python3 scripts/smoke_perception.py   # do the model weights load and run at all?
python3 scripts/smoke_observe.py      # does cua.perception see the application?
python3 scripts/smoke_drive.py        # does a click resolved from screen text land on it?
python3 scripts/smoke_replay.py       # does one artifact produce three different results?
python3 scripts/smoke_scan.py         # does find_and_act handle found / absent / ambiguous?
python3 scripts/smoke_escalate.py     # does the handoff work on the live session?
```

`smoke_scan.py` is the one that exercises the hardest step type: it finds a transaction by
merchant in a 22-row list, reads the amount out of the named column, returns a typed business
outcome when no row matches, and escalates rather than guessing when four rows do.

`smoke_drive.py` is the one worth running first if something is wrong: it signs in by reading
labels off the screen, opens a member, reads a balance, and refuses a deliberately stale
coordinate — so a failure there localizes the problem to perception, resolution, or input.
