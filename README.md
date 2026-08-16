# Computer-Use Automation

An LLM discovers how to complete a goal in a legacy application by driving its UI the way a
human operator would, then records what it learned as a typed, versioned **capability artifact**
that replays deterministically — no model in the decision loop.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how
> the AI agent invokes it in production.

Design notes: [`PLAN.md`](PLAN.md) · Brief: [`ASSIGNMENT.md`](ASSIGNMENT.md)

---

## Status

🚧 **Scaffolding complete; core loops not yet implemented.**

| | |
|---|---|
| ✅ Typed schemas (artifact, results, intervention) | real, tested |
| ✅ Target application + fault injection | real, runs |
| ✅ Container / compose / packaging | real, builds |
| ✅ Operator console shell + noVNC client | real, builds |
| 🚧 Perception, resolver, discovery loop, replay engine | typed stubs with documented contracts |

---

## Layout

```
backend/      the automation system (Python)
  src/cua/
    schema/       typed contracts — depends on nothing
    perception/   screen -> elements   (OmniParser + PaddleOCR)
    action/       elements -> input    (Playwright as an input engine, not a locator library)
    resolve/      semantic target -> coordinate, plus pre/post verification
    discovery/    the LLM loop and artifact synthesis
    replay/       deterministic execution, scan loop, outcome classification
    policy/       allowlist, risk classification, redaction seam
    escalation/   control transfer and human-action capture
    evidence/     per-run logs, frames, observations
    catalog/      capability store
    runtime/      session lifecycle, composition root
  policies/     per-app guardrail config
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
its entire output is one structured action. Needed for discovery only; replay never calls a model.

```bash
docker compose up --build
```

| | |
|---|---|
| http://localhost:8080 | target app — sign in as `teller01` (password in `.env.example`) |
| http://localhost:3000 | operator console |
| http://localhost:8000/docs | control plane API |
| http://localhost:6080 | raw noVNC view of the automation's display |

Or without Docker: `make install`, then `make targetapp` / `make console` / `make api` in
separate shells. `make help` lists everything.

---

## Demo path

<!-- Filled in as each piece lands. These are the four things §6.1/§6.3 ask a reviewer to run. -->

**1. Discovery** — LLM-driven run against the live surface, emits a draft artifact

```
TODO
```

**2. Replay** — deterministic re-run with input parameters, returns typed outputs

```
TODO
```

**3. Replay hitting a business outcome** — an ID that doesn't exist; returns a typed outcome
rather than failing

```
TODO
```

**4. Escalation** — trigger a handoff, take control of the live session, resume

```
TODO
```

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
| `slow` | recoverable — wait |
| `expired` | escalate (one-shot; clears itself) |
| `denied` | business outcome |
| `error500` | hard failure |
| `validation` | hard failure, with the fields shifted down |
| `confirm` | hard failure — an undeclared interstitial |

---

## Running without live services

<!-- TODO: replay-from-fixtures mode so a reviewer can exercise the deterministic path with no
     model credentials and no browser. The seam exists (perception.screen.ImageFileScreen). -->

```
TODO
```
