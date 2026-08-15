# Architecture

Deeper view than REPORT §1. Components, data flow, extension seams.

## Runtime topology

```
┌────────────────────────────────────────────────────────────┐
│  docker compose up                                         │
│                                                            │
│  ┌──────────────┐   ┌────────────────┐   ┌──────────────┐  │
│  │  Next.js UI  │──▶│  FastAPI       │──▶│  Runner      │  │
│  │  :3000       │   │  :8000         │   │  (in-proc)   │  │
│  │              │   │  REST + WS     │   │              │  │
│  └──────────────┘   └────────────────┘   └──────┬───────┘  │
│         ▲                    ▲                  │          │
│         │  step events (WS)  │                  ▼          │
│         └────────────────────┘           ┌──────────────┐  │
│                                          │  Playwright  │  │
│                                          │  Chromium    │  │
│                                          │  (headed)    │  │
│                                          └──────┬───────┘  │
│                                                 ▼          │
│                                          ┌──────────────┐  │
│                                          │  Target app  │  │
│                                          │  (ParaBank)  │  │
│                                          └──────────────┘  │
│                                                            │
│  Filesystem: ./artifacts/  ./evidence/  ./policies/        │
└────────────────────────────────────────────────────────────┘
```

- **Chromium headed** — the browser window IS the operator's view during handoff. That's why hosting isn't in scope.
- **WebSocket** streams step events (screenshot, overlay, LLM I/O, action) to the UI. Same stream drives discovery and replay.
- **Runner is in-process** — but the interface is `async def run(cmd) -> RunResult`, so lifting into a worker later is a rename, not a redesign.

## Top-level layout

```
interface/
├── README.md              # brief
├── REPORT.md              # design write-up
├── docs/                  # deeper docs (this file, schema, perception, prompts, ux)
├── backend/               # FastAPI + runner + surface + artifact + policy + llm
├── ui/                    # Next.js 14 app-router
├── policies/              # per-app YAML
├── artifacts/             # <app>/<cap>.v<n>.json
├── tenants/               # per-tenant overrides (design; single-tenant demo)
└── evidence/              # <run_id>/{run.json, steps/, llm/, intervention.json, human_actions.jsonl}
```

Backend module boundaries (see code for detail):
`runner/` (loop, discovery, replay, RunContext), `surface/` (Playwright web + enumerator + overlay + redactor), `locator/` (descriptor types + fallback resolver), `artifact/` (Pydantic schema + FS store + versioning), `policy/` (engine + YAML loader), `escalation/` (detector + bundle + human-action recorder), `evidence/` (append-only writer), `llm/` (Anthropic client + prompts + tool-use schemas).

## Data flow — discovery run

1. `POST /runs {mode: "discover", goal, start_url}` → `RunContext(id, state=RUNNING)`.
2. Surface opens Chromium at `start_url`. `observe()`:
   - Screenshot at 1440×900.
   - Enumerator walks a11y tree → `[{id, role, name, bbox, dom_path, frame}]`.
   - Redactor boxes over secret-shaped fields.
   - Overlay draws numbered rectangles.
3. `AgentLoop.decide()` → LLM call (system + goal + candidates JSON + overlay image). Returns tool-use `{action, mark_id?, value?, reason}`.
4. `Policy.check(action)` — deny → surfaced back to LLM as tool error.
5. Action executes via Playwright.
6. `StepEvent` → WS → UI. Written to evidence.
7. Descriptor derived from chosen candidate → appended to in-memory artifact draft.
8. Success check runs. True → freeze artifact, store, return. Max steps / timeout / dead-end → escalate.

## Data flow — replay run

1. `POST /runs {mode: "replay", artifact_id, inputs}`.
2. Load + validate artifact (Pydantic). Validate inputs against declared types.
3. For each step:
   - `observe()`.
   - `Resolver.resolve(descriptor, perception)` — fallback chain in order; drift events logged.
   - Business-outcome detectors run **first** — match → return outcome, stop.
   - Recoverable-condition detectors — match → run recovery, loop.
   - `Policy.check(action)`.
   - Action executes. Checkpoint asserted. Fail → `on_error` (hard_fail / escalate / ignore / retry).
4. Success asserted. Declared outputs extracted. Return `{status, outputs? | outcome? | failure?}`.

## Extension seams

| Seam | Protocol | Local impl | Prod impl (design) |
|---|---|---|---|
| Surface | `observe / act` | Playwright web | Desktop a11y, vision detector |
| Artifact store | `get/put/list/version` | Filesystem | Postgres + S3 |
| Evidence store | `append_step / finalize` | Filesystem | Object storage + index |
| Policy | `check(action, ctx)` | YAML | Central policy service |
| Secret resolver | `resolve(ref) -> str` | Env var | Vault / KMS |
| Runner | `run(cmd) -> result` | In-proc async | Queue + worker pool |

DI in `backend/app/config.py`.
