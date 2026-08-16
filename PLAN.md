# Plan

Working design doc. Organized under the seven headings the brief mandates for `/REPORT.md`
(§6.2) so decisions graduate straight into the deliverable rather than being rewritten.

At submission: this content becomes `REPORT.md`; `README.md` stays setup + demo path only (§6.1).

Each item is tagged **[DECIDED]** or **[OPEN]**. The table below tracks the decisions that were
blocking; all but one are now settled and the reasoning is recorded in place.

---

## Open decisions

| # | Decision | Blocks | Status |
|---|---|---|---|
| 1 | **Target application** | which goals, which error states can be triggered, whether failures can be injected for §6.3 | **DECIDED** — purpose-built local app (`targetapp/`), Next.js. A public sandbox cannot be made to emit "session expired" on demand, and §6.3 wants evidence of exactly that. Faults are injected at `/dev`; business outcomes are ordinary behaviour, kept deliberately separate. |
| 2 | **Run → artifact synthesis** — who decides `12345` was a parameter? | Artifact schema, discovery loop | **DECIDED** — three stages, deterministic first (`discovery/synthesize.py`). Prune → parameterize by exact match against the *declared run inputs* → model-assisted declaration of outputs/checkpoint/outcomes, every proposal validated against recorded frames. Nobody guesses which numbers look like ids; the caller declared them when they started the run. Stage 3 output is `draft` and needs human approval. |
| 3 | **Checkpoint + extraction mechanism** | Determinism story, replay result contract | **DECIDED** — PaddleOCR behind a `TextReader` protocol; scope regions located by anchor text, not fixed boxes; tolerance via the artifact's own recorded `Normalizer` list, so replay compares strings the way the recording did. Checkpoints authored at synthesis time and validated against the final frame. Known weakest link, stated as such. |
| 4 | **Handoff mechanics** | §3.6, container design | **DECIDED** — Xvfb + x11vnc + noVNC in the automation container; console embeds the noVNC *library* and toggles `viewOnly`. Playwright cannot see a manual click, so human actions are captured at the X layer instead (`escalation/watch.py`) — same code path for a future desktop surface, and no hole in the audit trail where a person touched regulated data. |
| 5 | **Stack** | — | **DECIDED** — Python 3.11+/uv backend, Next.js/pnpm for both frontends. Model via **LiteLLM**, so the provider is one env var and not an architectural commitment. Default `xai/grok-4`. |
| 6 | **"Agent Discovery"** | Scope | **RESOLVED** — it meant the discovery loop. The capability catalog stays a stretch goal, but `catalog/store.py::tool_manifest()` makes it nearly free: `list()` + `InputSpec`/`OutputSpec` is already a function-calling surface. |
| 7 | **Redaction boundary** | §3.4, honest-limits | **DECIDED (as a cut)** — seam built and wired at both call sites, implementation is a no-op. The tension is real and specific to a vision design: the screenshot is simultaneously the evidence and the model input, and a bank screen is PII by construction. Masking before the model would break the very tasks it is asked to do; real deployments rely on a zero-retention/BAA agreement instead. Documented in REPORT §7 rather than left as an implied capability. |

**Still open.** Cross-tenant override representation (§4 below) — the thinnest section, and the
only remaining decision that touches the schema.

---

## 1. Architecture

**[DECIDED] Perception: pure vision + Set-of-Marks.**
Screenshot + numbered overlay boxes on interactable candidates. LLM sees pixels, not markup.
Rationale: DOM/accessibility trees are unavailable or useless on legacy and desktop surfaces;
vision is the worst-case-safe path and the only one that generalizes to all three surface classes.
The brief biases the same way (§3.1: "bias toward an approach that would still work when the
surface has no clean DOM").

**[DECIDED] Action: Playwright as input engine, not as a DOM locator library.**
`page.mouse` / `page.keyboard` only. No `page.locator()`, no selectors. Playwright supplies a
browser and a way to inject input events; it is deliberately not doing element resolution.
Keeps a single code path that extends to desktop via the same primitives.

**[DECIDED] Reliability ordering:** vision → (future) accessibility tree / DOM.
The fallback direction is *toward* structure, never dependent on it.

**[DECIDED] Runtime shape:** headful browser, fixed viewport, so a human can take over the same
live session. Built to be wrappable in a container with x11vnc later for desktop surfaces.

**[DECIDED] Stack** (decision 5) and **target application** (decision 1) — see the table above.

---

## 2. Artifact schema

An artifact is a **capability contract**, not a click track. A human reviewer and a calling agent
must both be able to read what it does, what it needs, and what it returns.

**[DECIDED] Fields:**
- `id` + `version`
- goal template
- typed **inputs** (parameters the caller supplies)
- typed **outputs** (what the caller receives)
- ordered **steps** (with parameter substitution)
- **success checkpoint** (explicit, checkable)
- **business-outcome handlers** (small explicit set)
- **metadata** — surface, viewport, model used, `recorded_at`

**[DECIDED] Coordinates normalized 0–1, original viewport recorded, viewport held fixed.**

**[DECIDED] Every step carries semantic intent alongside its coordinate.**
```json
{
  "intent": "click the row for the searched member",
  "target_desc": "table row containing the member ID",
  "anchor_text": "{{member_id}}",
  "coord": [0.42, 0.71]
}
```
A bare coordinate cannot support three separate requirements: data-dependent targeting
(which row *is* member 12345?), risky-action policy checks (§3.4 — you cannot classify
`click(0.42,0.71)` as reversible or not), and cross-tenant reuse (§3.7 — different branding
moves every pixel). Storing the model's own description of the target solves all three at once,
without reintroducing a DOM dependency. Replay stays LLM-free: it resolves the anchor by OCR
and clicks relative to the matched box, falling back to the raw coordinate.

**[DECIDED] `find_and_act` is a first-class step type.**
Lists, scrolling, and pagination mean the target's position is data-dependent. Recording
`scroll, scroll, click(y)` is wrong four different ways: position drift, page drift, absence,
and ambiguity. Record the *predicate*, not the position:

```json
{
  "type": "find_and_act",
  "scope":     { "anchor_text": "Date | Description | Amount", "extent": "below" },
  "predicate": { "match": "row_contains_all",
                 "terms": ["{{merchant}}", "{{amount}}"],
                 "normalize": ["casefold", "strip_currency", "collapse_ws"] },
  "scan":      { "advance": "scroll", "overlap": 0.15,
                 "stop_when": "region_hash_unchanged", "max_advances": 10 },
  "on_found":     { "action": "click", "anchor": "matched_row", "offset": [0.86, 0.0] },
  "on_not_found": { "outcome": "transaction_not_found" },
  "on_multiple":  { "policy": "escalate" }
}
```
Still fully deterministic — a fixed loop (screenshot scope → OCR → test predicate → advance),
no model in it. Pagination is the same primitive with `advance: {click_anchor: "Next"}`.
Extraction ("last N transactions") is the same primitive with
`on_found: {action: "extract", collect: "all", limit: N}`.

**[DECIDED] The discovery action space contains exactly the primitives we want in artifacts.**
Give the discovery agent `find_and_click(predicate, scope)` as a callable action so it never
hand-scrolls. Then recordings are replayable *by construction*, instead of requiring fragile
post-hoc inference of intent from a transcript of scrolls.

**[DECIDED] Synthesis: how the run becomes the artifact** (decision 2). Earlier candidates —
LLM emits the artifact as a final synthesis pass over its own transcript; record-everything then
prune; or the agent tags each action keep/discard inline. Related: parameter identification.

---

## 3. Determinism & error handling

**[DECIDED] Replay never calls the LLM for decisions.** It re-executes recorded steps with
supplied inputs and evaluates the success checkpoint. Determinism means *same procedure, no
model decisions* — not *same pixels*.

**[DECIDED] Error & outcome taxonomy.** Three classes, never conflated:

| Class | Meaning | System behavior |
|---|---|---|
| Success | Checkpoint passed + outputs extracted | Return outputs |
| Business outcome | Expected legitimate result ("member not found") | Return typed outcome — **not** a failure |
| Recoverable | Known transient condition | Deterministic wait / dismiss / retry |
| Hard failure | Anything else | Stop immediately + rich evidence |

**[DECIDED] Detection under pure vision:** primary signal is checkpoint failure; known cases via
scoped, tolerant OCR pattern matching; **unknown states are hard failures** — intentional and
correct, not a gap.

**[DECIDED] No open-ended LLM self-healing on the replay path.** Hard failures escalate to a
human instead. Safer default for regulated financial data.

**[DECIDED] Scan-loop rules** (these decide whether `find_and_act` actually works):
- *Exhausted the list and no match* → business outcome. *Hit `max_advances` while content was
  still changing* → **hard failure** — we don't know whether the record is absent or we quit
  early, and conflating those is the mistake the brief's glossary calls out by name.
- Advance ~85% of region height, never 100%, or a row straddling the boundary is skipped and
  reports a false not-found.
- Ambiguity is first-class: two matches on a read task may be tolerable; on a write task
  (dispute/transfer) it must escalate — acting on the wrong record is unrecoverable.
- Normalize before comparing (`$1,234.56` vs `1234.56`, truncated `ACME Corp…`, date formats)
  and record the normalizer list in the artifact so replay is reproducible.
- Locate the scope region by anchor text (column-header row), not a fixed bbox.

### Position resolution & verification

**Framing:** the brief deliberately downgrades layout drift — "the hard part is *not* constant
drift" (§1), and §6.2 asks for runtime error handling "and, *secondarily*, any UI drift". So this
is not a drift-tolerance feature. It exists because a target's position varies **within the same
unchanged version of the app**, on essentially every run:

- a conditional banner renders (session-expiry warning, maintenance notice) → everything below shifts
- an **inline validation error** appears → the fields under it move (constant in form flows)
- variable-length data — a long member name or address wraps to two lines
- the list above the button has 12 rows today and 3 tomorrow
- **async reflow** — screenshot at t=0, a widget loads at t=200ms and re-lays out the page (a race, not drift)
- per-tenant branding: different logo height, same vendor app (§3.7)
- the human resizes the window during a handoff (real, given the headful/VNC design)

None of these are "the app changed." Anchor-relative targeting (§2) handles all of them, which is
the strongest justification for that decision.

**[DECIDED] A stable coordinate can still be the wrong click.** An unexpected modal moves nothing —
it lands *on top*. The recorded coordinate is still "correct" and the click hits the dialog. The
brief names unexpected confirmation dialogs as a runtime condition. Blind coordinate clicking has
no notion of *what it actually hit*; in a banking app that is a correctness and safety failure, not
a robustness one. The answer is therefore verification, not just better targeting:

1. **Pre-click assertion.** After resolving a target and before clicking, OCR the region and check
   it matches the step's recorded `target_desc` / label. Mismatch → do not click. Nearly free (the
   screenshot already exists), and converts "silently clicked the wrong thing" into a detected
   failure.
2. **Per-step checkpoints, not just a final one.** The brief's glossary defines a checkpoint as
   asserting "you actually reached the state you expected, rather than assuming the click worked."
   Per-step turns a wrong click at step 3 into a clean hard failure at step 3 instead of a garbage
   output at step 9.
3. **Settle before observe.** Wait until two consecutive frames hash-equal before resolving
   coordinates. Kills the async-reflow race deterministically — no `sleep()`.
4. **Overlay detection as a recoverable class.** Check for a modal before each step. Declared
   dismissal handler → recoverable. Undeclared dialog → hard failure / escalate. Never click
   through one.

**[DECIDED] Cut line — variance is handled, drift is detected but not repaired.**

| | Definition | Behavior |
|---|---|---|
| **Variance within a version** | Position differs run-to-run in an unchanged app (list above, banners, wrapping, reflow) | **Handled** — anchor-relative resolution + the four verifications above |
| **True drift across versions** | The app itself changed since recording | **Detected, not repaired** — fall back to recorded bbox, log a drift event; if pre-click verification fails, stop and escalate to a human who can re-record |

No LLM repair on the replay path — consistent with §3's no-self-healing decision and the right
default for regulated data.

**Free drift signal:** record which resolution tier satisfied each target (anchor → bbox fallback).
Anchor resolutions beginning to fail across runs is an early warning *before* a hard failure, and
it is the same mechanism as the per-tenant canary in §4.

**[DECIDED] Success verification:** explicit checkpoint stored in the artifact. Screenshots are
evidence, not the decision mechanism. Outputs extracted via OCR + parsing after the final step.

**[DECIDED] OCR engine and match tolerance** (decision 3).
**Known weakest link, to state plainly in the write-up:** OCR over a scrolling list is the least
deterministic part of an otherwise model-free path — truncation and format variance are real,
and a 10-screen scan costs 10 screenshot+OCR cycles (local and cheap, but not free).

---

## 4. Heterogeneity & multi-tenant

*Required §6.2 heading and currently the thinnest section — needs the most work.*

**[DECIDED] Surface abstraction seam:** perception is `observe() → screenshot + candidates`;
action is coordinate/anchor-based input. Swapping surfaces (modern web → legacy frameset →
desktop) swaps the candidate enumerator, not the artifact format. Vision-first means the seam
holds even where no structured tree exists.

**[DECIDED] Predicates and anchors are the portability story.** A `find_and_act` predicate
("the row containing this merchant") survives rebranding and layout differences across tenants
running the same vendor product; a coordinate does not. This step type is likely the strongest
answer to §3.7.

**[OPEN]** Base artifact vs. per-tenant override representation. How drift is detected across
tenants/versions. Route/value canonicalization (`/account/12345` → `/account/:id`).

---

## 5. Escalation & handoff

**[DECIDED] Control-transfer sequence:**
1. Detect stuck / risky / unrecoverable state
2. Emit `InterventionRequest` (goal, capability + step, screenshot, reason)
3. Pause the **same live session** — browser context stays alive
4. Human takes control of that session
5. Human signals resume or abort
6. System records the handoff, then continues or stops
7. Intervention evidence preserved

**[DECIDED] Detection triggers:** scan exhausts without resolution, policy denies a risky action,
discovery hits max steps with no checkpoint match, ambiguity on a write task, **pre-click
verification mismatch or an undeclared overlay** (§3), LLM emits `escalate`.

**[DECIDED] Mechanics** (decision 4) — VNC, for the reason that follows. Headful Playwright makes takeover trivial but the human's
actions **invisible** — §3.6 requires recording what they did, and Playwright can't observe manual
clicks without instrumentation. VNC captures input events cleanly at the X layer and matches the
desktop-extension story, which may make it the *simpler* path rather than the stretch goal.
Also unsettled: resume signal (HTTP endpoint is fine) and who holds the control token.

---

## 6. Safety

**[DECIDED] Configurable allowlist:** permitted domains / URL patterns / allowed action types.
Checked before every action, in both discovery and replay.

**[DECIDED] Explicit safe-reversible vs. risky-irreversible classification.** Risky actions are
blocked or require confirmation / escalation. In banking, latency beats a silently wrong transfer.
*Note:* this is only enforceable because steps carry `intent` labels (§2) — a coordinate alone
cannot be classified.

**[DECIDED] Never persist secrets, credentials, or raw PII** into artifacts or logs. Redact.

**[DECIDED, as a cut] Redaction boundary** (decision 7). Real tension specific to a vision design: screenshots
are simultaneously the evidence *and* the model input, and bank screens are PII by construction.
A DOM-based design partly sidesteps this; we can't. Decide and state the limit honestly.

**Known limits to declare:** heuristic redaction; static per-app policy (no dynamic risk scoring);
no defense against prompt injection via page content read by the LLM.

---

## 7. Cuts

**[DECIDED] Deferred, each at a clean seam:**
- Accessibility-tree perception — future enhancement; vision path ships first
- Desktop surface — abstraction designed for it, only browser ships
- Operator console — minimal/mocked view; handoff *mechanism* is real
- Multi-tenant plumbing — schema-level only, no registry or vault
- LLM-assisted recovery on replay failure — deliberately excluded from the main path

**Evidence** — every run (discovery + replay) produces a structured step log, screenshots at key
points and on failure, the final artifact, and a `ReplayResult` with clear status. All under
`/evidence/`.

---

## Appendix A — candidate goals

Need to commit to ~2 (see decision 1). One read capability end-to-end, plus one that demonstrates
a business outcome; a write task is the best vehicle for the risky-action guardrail.

*From the brief:* look up member 12345 and read their savings balance · open a new sub-account and
reach the confirmation screen · add an item to the cart and reach checkout review.

*Read tasks:* current balance of account X · last N transactions of account X · find transaction
matching amount Y or merchant Z in account X.

*Write tasks:* transfer funds X → Y · pay a bill · request a loan.

## Appendix B — runtime error & exception cases to cover

*From the brief:* record not found · permission denials · unexpected confirmation dialogs ·
session/timeout expiry · transient slowness · outright app errors.

*Additional:* not logged in / timed out · needs a confirm/go press · needs scrolling ·
date format (MM-DD-YYYY) handling · missing transactions or wrong date range · account info not
found for X or Y · network / server error · verifying a transaction actually happened at the
correct amount.

## Appendix C — build tasks

Scaffolding (done):
- [x] Repo layout, packaging (uv / pnpm), Dockerfiles, compose
- [x] Exact artifact schema, pinned and tested — `backend/src/cua/schema/`
- [x] Result + intervention contracts (error taxonomy, control-transfer states)
- [x] Target application with injectable faults — `targetapp/`
- [x] Guardrail config — `backend/policies/targetapp.yaml`
- [x] Operator console shell + noVNC client — `console/`

Core (typed stubs with documented contracts; not implemented):
- [ ] Perception: display capture, OmniParser detect, PaddleOCR read, merge
- [ ] Set-of-Marks overlay: candidate enumeration + numbered boxes
- [ ] Resolver ladder + pre-click / post-action verification
- [ ] Discovery loop with `find_and_act` in the action space
- [ ] Synthesis: prune → parameterize → declare
- [ ] Replay engine + scan loop + outcome classification
- [ ] Policy enforcement (allowlist, risk, recoveries)
- [ ] Evidence writer
- [ ] Handoff: pause / take control / resume / record human actions
- [ ] Two capabilities recorded end to end, with evidence
