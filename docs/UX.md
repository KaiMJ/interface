# UI / UX

Two audiences, one app:

1. **Developer / reviewer** — inspecting discovery runs, artifacts, and
   replays. This is 90% of the value; it makes the system debuggable and
   makes evidence for the take-home easy to produce.
2. **Operator** — the human who takes over during escalation. Minimal but
   real (see REPORT §5).

Stack: **Next.js 14 (app router) + Tailwind + shadcn/ui**. Talks to FastAPI
via typed fetch; live step events via WebSocket. No auth (single-user local
demo).

## Information architecture

```
/                      Dashboard — runs list, "New run" CTA
/runs/new              Form: pick mode (discover|replay), goal or artifact,
                       inputs, allowlist preview
/runs/[id]             Live run view (see below)
/artifacts             Capabilities catalog
/artifacts/[id]        Artifact detail — schema, versions, stability, edit
/artifacts/[id]/diff   Version-to-version semantic diff
/operator/[run_id]     Mock operator console (opened only when escalated)
/policies              View/edit YAML policies per app
```

## `/runs/[id]` — the money view

Layout: three columns.

```
┌──────────────────────────┬────────────────────────┬──────────────────┐
│  Timeline (left rail)    │  Screenshot + overlay  │  Step inspector  │
│                          │  (main)                │  (right rail)    │
│  ● Step 1 click "Trans-  │                        │                  │
│    fer Funds"            │  [big image w/         │  Action: click   │
│  ● Step 2 type "500" ..  │   numbered boxes,      │  Mark: #7        │
│  ● Step 3 select ...     │   hover = highlight]   │  Reason: "..."   │
│  ⋯                       │                        │                  │
│  ▶ Step 6 extract        │  Tabs:                 │  Descriptor:     │
│    checkpoint: PENDING   │  [overlay] [raw]       │   primary: ...   │
│                          │  [dom] [a11y] [llm]    │   fallbacks:     │
│  🟡 Escalated at step 5  │                        │    [1] text_near │
│                          │                        │    [2] dom_path  │
│  Tokens: 12,340          │                        │    [3] bbox(low) │
│  Cost: $0.08             │                        │                  │
│  Duration: 42s           │                        │  Checkpoint:     │
│                          │                        │   url ~ .../conf │
└──────────────────────────┴────────────────────────┴──────────────────┘
```

Key interactions:
- **Scrub the timeline** → main image + inspector update. Same view for
  discovery and replay; a mode badge distinguishes.
- **Overlay tab** shows what the LLM saw. **DOM** / **a11y** tabs show what
  the enumerator saw. **LLM** tab shows the exact request + response tokens
  for that step.
- **Drift banner** on any step whose descriptor resolved on a fallback tier
  → click to see which tier and why.
- **Business-outcome banner** in green if the run ended on a declared
  outcome; **failure banner** in red with step id + expected vs observed.

## `/artifacts/[id]` — reviewable capability

Rendered from Pydantic model, not raw JSON:

- Header: id, version, status (draft/approved/deprecated), stability chip
  (`11/12 successes, 1 outcome`), last-replayed timestamp.
- **Inputs / outputs** as typed cards with constraints visible.
- **Steps** as a vertical list, each collapsible. Descriptor rendered with
  primary + fallback tiers as chips.
- **Business outcomes** as their own section — hover for detector.
- **Policy ref** links to the YAML.
- Actions: `Replay with inputs` (opens `/runs/new` prefilled), `Diff vs
  previous version`, `Approve` (draft → approved), `Deprecate`.

## `/operator/[run_id]` — the mock console

Opened automatically on escalation (backend WebSocket pushes an event → UI
navigates in a new tab). This is the *only* human-facing screen during
handoff.

```
┌──────────────────────────────────────────────────────────┐
│  🟡 Intervention required                                 │
│  Run: run_a3f1  ·  Capability: cap_transfer_funds v3     │
│  Step: 3 / 6                                             │
│  Reason: Descriptor for "From Account" combobox could    │
│          not be resolved — no matching role+name.        │
│                                                          │
│  ▸ The Chromium window is live on your screen.           │
│    Complete the step directly in that window.            │
│                                                          │
│  ┌─────────────────────────────────────────┐             │
│  │ [last screenshot with overlay]          │             │
│  └─────────────────────────────────────────┘             │
│                                                          │
│  Recorded actions since pause:                           │
│    • click at (412, 318)                                 │
│    • keydown "Tab"                                       │
│    • click at (605, 402)                                 │
│                                                          │
│  Context bundle: [Download JSON]                         │
│                                                          │
│         [  ▶ Resume automation  ]  [ ✖ Abort ]           │
└──────────────────────────────────────────────────────────┘
```

- **No embedded browser stream.** The user is expected to look at the actual
  Chromium window (headed). Documented in REPORT §5.
- **Recorded actions** panel updates live as Playwright's listeners fire.
- **Resume** → POSTs `/runs/:id/resume` → backend re-observes and continues.
- **Abort** → POSTs `/runs/:id/abort` → clean shutdown, evidence finalized.

## UX principles applied

- **Every panel has an evidence artifact behind it.** Nothing rendered is
  synthesized in the UI; if you screen-record the UI, you've recorded truth.
- **Latency is visible.** Per-step LLM latency + token count in the timeline
  — makes cost regressions obvious.
- **Descriptors are first-class.** The descriptor inspector is the most
  important debugging surface. Fallback tier that resolved is highlighted.
- **No dark patterns on risky actions.** Replay confirmation prompts show
  the resolved action (e.g. *"About to click 'Confirm Transfer' — $500 from
  12345 to 54321"*) before executing when policy is `require_confirmation`.
- **Empty states are informative.** No runs yet → the dashboard shows the
  exact CLI commands from the demo path in README.

## Explicit non-goals

No auth. No live noVNC/CDP browser stream (documented seam). No inline descriptor editing — descriptor edits happen by re-running discovery, since hand-editing to hide a bug is the wrong incentive. No search/filters beyond a status chip.

