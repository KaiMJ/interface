# REPORT

Design write-up for the computer-use automation take-home. Seven headings per §6.2 of the brief. Deeper detail in `/docs/`.

---

## 1. Architecture

Single-process Python monolith: FastAPI backend, Next.js debug/operator UI, headed Playwright Chromium. `docker compose up` runs it all. No queues, no cluster — brief warns against premature scaling infra (§7).

Stack: Python 3.12, FastAPI, Playwright (Chromium headed, 1440×900), Anthropic Claude Sonnet 4.6 (vision), Pydantic v2, SQLite, Next.js + Tailwind.

Six seams, each a `typing.Protocol` with a swappable impl:

1. **`Surface`** — `observe()` / `act()`. Local: Playwright. Extends to desktop AX/UIA or vision detectors.
2. **`AgentLoop`** — observe→decide→act. LLM-driven in discovery; artifact-driven in replay. Same `Surface`, same step-event stream.
3. **`ArtifactStore`** — content-addressed, versioned. Filesystem now; Postgres+S3-shaped interface.
4. **`EvidenceStore`** — append-only per-run.
5. **`Escalation`** — pause/resume on same live session. Operator UI mocked (documented seam).
6. **`Policy`** — allowlist + risky-action check, called before every action in both modes.

An artifact is a **capability contract**, not a Playwright script — reusable across surfaces, distinguishes business outcomes from failures, callable by an upstream agent with typed args. Details: `docs/ARCHITECTURE.md`, `docs/ARTIFACT_SCHEMA.md`.

---

## 2. Artifact schema

An artifact is a **typed, versioned capability contract**. Full spec + examples in `docs/ARTIFACT_SCHEMA.md`. Load-bearing choices:

- **Descriptors, not selectors.** Every target is a prioritized fallback chain (`role_name` → `text_near` → `label_for` → `dom_path` → `css` → `bbox_ratio`). Re-resolved every replay. Bbox is last-resort only, logs a drift event.
- **Business outcomes are first-class.** "No such member" is a *result*, not a failure. Declared per artifact; detectors run before the success check each step.
- **Inputs / outputs typed + validated** via Pydantic. Artifact doubles as a callable-tool schema for an upstream agent.
- **Content-addressed + versioned.** `sha256(canonical(artifact))[:12]`; `artifact_id` stable across versions; hash changes on any semantic edit → reviewable diffs.
- **Multi-tenant seam.** Base artifact per app; per-tenant overrides in a sibling file, layered at load time.

---

## 3. Determinism & error handling

Perception is stochastic; targeting must not be. Discovery uses **Set-of-Marks**: enumerate interactable candidates from the a11y tree (with DOM fallback), overlay numbered boxes on a fixed-viewport screenshot, send image + candidate JSON to the LLM. LLM returns `{action, mark_id, value?}` — bounded output. On commit, a semantic descriptor is derived from the chosen candidate and stored — **not the mark id, not the raw coords**. Replay re-resolves the descriptor against the current page; LLM is not in the loop. Waits are declarative checkpoints (`url_matches`, `text_present`, `network_idle`, `element_visible`) — never `sleep()`. Full pipeline: `docs/PERCEPTION.md`.

**Error taxonomy** (three disjoint classes):

| Class | Example | Replay behavior | Result contract |
|---|---|---|---|
| **Business outcome** | "no such member", "insufficient funds" | Stop cleanly, return typed outcome | `{status: "outcome", name, data}` |
| **Recoverable** | Session-expired modal, transient 5xx, slow load | Run declared recovery (bounded retries) | Continue; note in evidence |
| **Hard failure** | Descriptor unresolvable, checkpoint timeout, policy denial | Abort, snapshot, escalate | `{status: "failure", step, expected, observed}` |

Business outcomes declared per artifact; recovery handlers declared per app (`policies/<app>.yaml`) so they generalize across capabilities on the same app. Drift events fire when resolution falls to a fallback tier; nightly canaries surface patterns before production breaks.

---

## 4. Heterogeneity & multi-tenant

Same artifact, different enumerator. `Surface` is deliberately narrow (`observe`/`act`); swapping surfaces means swapping the candidate enumerator (web → per-frame legacy → OS a11y for desktop → vision detector for canvas). Descriptors are semantic (role + name + geometry + fallbacks) and encode no surface specifics. Detail: `docs/PERCEPTION.md`.

Multi-tenant: artifacts authored per **app**, not per **tenant**.

- **Base artifact**: `artifacts/<app>/<cap>.v<n>.json`. Written once.
- **Tenant overrides**: `tenants/<tenant>/<app>/<cap>.overrides.json` layer base URL, credential refs, extra selector fallbacks, per-tenant business outcomes. Merged at load; base never mutated.
- **Canonicalization** at authoring time: `/account/12345` → `/account/:account_id`.
- **Drift detection**: nightly canary replay per (tenant, capability) diffs which fallback tier resolved each descriptor. Fallback-tier drift → alert before hard failure.
- **Credential isolation**: `secret_ref` inputs resolve from per-tenant vault at replay. Artifacts never contain secrets.

---

## 5. Escalation & handoff

**Detection triggers**: descriptor resolution exhausts all fallbacks; policy denies a risky action; discovery hits max-steps without a checkpoint match; LLM emits `escalate`.

**Bundle** written to `evidence/<run_id>/intervention.json`: goal, capability id + version, current step, last screenshot (with + without overlay), URL, reason.

**Control transfer — the real seam**: runner flips `RunContext.state = PAUSED_FOR_HUMAN`; action loop yields; **browser page and context stay alive**. Operator UI (mocked) instructs the operator to complete the step directly in the headed Chromium window. Playwright listeners record clicks, keystrokes (redacted for secret fields), navigations, and DOM mutations while paused → appended to evidence as `human_actions[]`. On resume, runner re-observes; in discovery the LLM sees a note about the intervention; in replay we advance to the next step whose checkpoint matches (skip-forward, not blind resume).

Brief permits a mock operator UI (§3.6). Handoff *mechanism* is real; only the operator's *view* is "look at the headed window." Production swaps in a CDP-bridge stream; the pause/resume contract is unchanged.

---

## 6. Safety

**Allowlist**: every action passes through `Policy.check(action, ctx)` before execution — same call in discovery and replay. YAML per app: allowed domains, allowed action types, risky-action list, per-navigation allowlist, max steps. Denied in discovery → surfaced back to LLM as a tool error. Denied in replay → hard failure + escalation.

**Risky/irreversible actions** (transfers, deletes, confirmations) classified per app. Default: `require_confirmation` — replay pauses and raises an intervention rather than clicking. In banking, silent execution of a wrong transfer is worse than latency.

**Secret handling**: `secret_ref` inputs never appear in logs, screenshots, or artifacts. Screenshot redactor boxes over password/PII-shaped fields before evidence write. LLM prompts receive placeholder tokens (`<<SECRET:...>>`); the action layer substitutes at execution.

**Limits (honest)**: screenshot redaction is heuristic; policy is static per-app (no dynamic risk scoring); no defense against prompt injection via page content read by the LLM (real concern for bank statements with hostile memo lines — documented future work).

---

## 7. Cuts

**Deliberately not built** (each with a real seam):
- **Operator UI is minimal** — one page, poll-based, no live browser stream. Handoff mechanism real; view is not.
- **Desktop / canvas surfaces** — `Surface` interface designed for it; only Playwright ships.
- **Multi-tenant plumbing** — schema supports overrides; no tenant registry, no vault (env vars stand in).
- **Queue / worker pool** — synchronous runner. Interface is stateless-per-run so a queue drops in front.
- **Capability catalog / tool-calling surface** — stretch goal; artifacts already Pydantic-typed so exposing them as tools is a thin wrapper.
- **Assisted fallback on replay failure** — hard failures escalate to humans instead of re-invoking the LLM. Safer default for regulated data.
- **Cross-tenant canonicalization demo** — designed in §4; not shown on a second variant.

**Next up (order of value)**:
1. Capability catalog endpoint (`GET /capabilities`, `POST /invoke/<id>`) — makes the system agent-callable.
2. Confidence-gated approval (`draft` → `approved`) — measure replay stability over N runs before unattended use.
3. Assisted fallback for a single failed step, policy-checked, recorded as evidence.
4. Second-tenant demo: same artifact against a modified ParaBank variant with overrides.
5. Prompt-injection hardening on page-read content.
