# Computer-Use Automation

An LLM discovers how to complete a goal in a legacy application by driving its UI the way a
human operator would, then records what it learned as a typed, versioned **capability artifact**
that replays deterministically — no model in the decision loop.

> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how
> the AI agent invokes it in production.

Working design notes: [`PLAN.md`](PLAN.md)

---

## Status

🚧 In design. See [`PLAN.md`](PLAN.md) for decisions made and still open.

---

## Setup

<!-- TODO: fill in once the stack is picked (PLAN.md decision 5) -->

**Prerequisites**

```
TODO
```

**Configuration**

```bash
cp .env.example .env
# ANTHROPIC_API_KEY=...   # required for discovery runs only; replay never calls the LLM
```

**Install / run**

```
TODO
```

---

## Demo path

<!-- TODO: fill in as each piece becomes real. These are the four things the brief asks a
     reviewer to be able to run (§6.1, §6.3). -->

**1. Discovery** — LLM-driven run against a live surface, emits an artifact

```
TODO
```

**2. Replay** — deterministic re-run with input parameters, returns typed outputs

```
TODO
```

**3. Replay hitting a business outcome** — e.g. an ID that doesn't exist; returns a typed
outcome rather than failing

```
TODO
```

**4. Escalation** — how to trigger a handoff, take control of the live session, and resume

```
TODO
```

---

## Running without live services

<!-- TODO: replay-from-fixtures / mock mode so a reviewer can exercise the system with no
     API key and no network. -->

```
TODO
```

---

## Layout

```
artifacts/    saved capability artifacts (typed, versioned)
evidence/     per-run logs, screenshots, artifacts, replay results
PLAN.md       working design notes
ASSIGNMENT.md the brief
```

