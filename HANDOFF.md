# Handoff

Written for a fresh session picking this up cold. `ASSIGNMENT.md` is the brief,
`PLAN.md` the design reasoning, `README.md` how to run it. This file is *state*.

Branch `dev`. 105 tests, ruff and mypy strict clean.

---

## 1. Where the project actually is

**The whole thread works end to end against the live application.**

A real xAI run records a capability; replay executes it deterministically and
returns typed outputs; the same artifact returns a typed business outcome for a
member that does not exist; a risky step parks the run and hands the live session
to a human who can act and give it back; every run leaves evidence the console can
show.

| Area | State |
|---|---|
| `schema/`, `config.py`, `calibration.py`, `clock.py` | real, tested |
| `perception/` — capture, detect, OCR, merge, set-of-marks, spatial index | real, measured against the app |
| `action/` — browser (Playwright as an input engine), offline, desktop | browser + offline real; desktop is a documented seam |
| `resolve/` — ladder, normalizers, templates, verification | real, tested |
| `replay/` — engine, scan loop, outcome classification | real, 17 tests over the taxonomy |
| `discovery/` — LLM client, action space, prompts, loop, synthesis | real, exercised by a live model |
| `catalog/` — store, tool manifest, outcome learning | real |
| `policy/`, `evidence/`, `escalation/`, `runtime/`, `api/`, `cli.py` | real |
| `console/` — runs, per-step frames, policy, handoff | real |
| `targetapp/` | real, all flows and faults |
| **`REPORT.md`** | **missing — mandated deliverable (§6.2)** |

---

## 2. Running it

```bash
cp .env.example .env          # XAI_API_KEY for discovery; replay needs no key
docker compose up --build
docker compose exec desktop python3 scripts/fetch_models.py    # one-time, ~270MB
make test                     # 105 tests, no browser, no key
```

The demo path is in `README.md`. Four smoke scripts answer one question each, in
the container:

```bash
python3 scripts/smoke_perception.py   # do the weights load at all?
python3 scripts/smoke_observe.py      # does perception see the app?
python3 scripts/smoke_drive.py        # does a click resolved from text land on it?
python3 scripts/smoke_replay.py       # one artifact, three result classes
python3 scripts/smoke_escalate.py     # the handoff, on the live session
```

**Source changes need the image rebuilt** (`docker compose build desktop`) or
copied in (`docker compose cp backend/src desktop:/app/`). `docker compose up -d
desktop` recreates from the image and silently discards a `cp`, which has cost
time twice.

---

## 3. What has been measured

| | |
|---|---|
| perception, warm | 1.6–2.3s per observation; OCR is ~95% of it |
| a dense member page | 143 elements — 8 detector boxes, 135 OCR lines |
| replay, 5-step capability | ~10s end to end |
| discovery run | 5 model calls, ~90s, xai/grok-4.3 |

Two facts worth carrying:

1. **OCR dominates.** If perception ever needs to be faster, that is the only
   place worth looking.
2. **On this surface OCR supplies 135 signals to the detector's 8.** Enterprise
   back-office screens are dense text with few icons. The merge step is where the
   value is.

---

## 4. What is left

1. **`REPORT.md`** — the seven mandated headings. `PLAN.md` is organized under
   them, and `PLAN.md` Appendix D holds the newest design decisions.
2. **A recorded write capability.** `cap_transfer_funds` exists only as a fixture
   (`backend/fixtures/transfer_funds.json`). The transfer form is three `<select>`
   elements, which the discovery loop has not been pointed at — a real perception
   question, not a gap in the plumbing.
3. **Multi-tenant (§3.7)** is schema-level only: `AppRef.base_url_pattern` plus
   the argument that anchors and predicates are the portability story. REPORT §4
   is the thinnest section.
4. **The prompt experiment.** Strip the rules from the system prompt, re-run the
   same goal, compare steps taken and discards. Five model calls; would replace an
   opinion with a number in the write-up.

---

## 5. Decisions already made — do not relitigate without new evidence

Reasoning is in `PLAN.md` and in the module docstrings.

| Decision | Why |
|---|---|
| Local Next.js target app | §6.3 wants evidence of a replay hitting a runtime error; you cannot make a public sandbox return "session expired" on demand |
| Vision-first perception (OmniParser + PP-OCR over a screenshot) | the only path that generalizes to legacy and desktop surfaces |
| Playwright as an *input engine*, never a locator library | no `page.locator()` anywhere; keeps one code path that extends to desktop |
| The X display is the coordinate space | one image, one space: the model and the operator argue about the same picture |
| Async driver, sync perception in a thread | Playwright's sync API cannot run inside a live loop; perception is CPU-bound |
| The action space *is* the artifact's step vocabulary | recordings are replayable by construction rather than by post-hoc inference |
| `expect` on every action | a step whose stated expectation did not come true is not recorded with a checkpoint |
| Parameterization by exact match on declared inputs | nobody guesses which numbers are ids; the caller declared them |
| Outcomes are falsified, then learned by demonstration | a detector for an unseen screen is a guess; see `catalog/learn.py` |
| VNC handoff, X-layer capture | Playwright cannot observe a click it did not issue |
| Redaction: declared values real, pattern masking a seam | stated as a cut in the module docstring |

---

## 6. Traps — things that already cost time

**Coordinates**
- `viewport=None` does *not* disable Playwright's viewport emulation; the flag is
  `no_viewport=True`. The page rendered at 1280x720 inside a 1440x900 window while
  we photographed the display: every coordinate wrong by a scale factor.
- Chromium ignores `--window-size` and `--kiosk` under Playwright. The window is
  sized and fullscreened over CDP. `start()` refuses a session whose page and
  display disagree on size.
- Browser chrome is not always on top. The first origin formula assumed it was and
  clicks landed 85px high — the password went into empty space and every step
  reported success.

**Perception**
- The OCR detector's default input size downscales a 1440px frame far enough to
  lose small coloured text. `ocr_det_side_len=1600` costs ~20% and reads it.
- Detector boxes are tighter than OCR lines (29x16 vs 41x23 on the same button),
  so text-inside-control alone leaves every button anonymous.
- Adjacent OCR line boxes touch. "Overlaps at all" put two table rows in one band
  and returned the neighbouring account's balance.

**Discovery**
- A discarded step may already have changed the screen. Dropping it leaves the
  recording missing a state transition and replay starts on a screen it never
  reaches.
- The entry navigation must be *recorded*, not just performed.
- `prune` renumbering steps cut the link between an extraction and its output.
- Models answer `expect` with descriptions ("the profile is shown") unless the
  tool says, with examples, that it is matched as a substring of screen text.

**Docker / build**
- `docker compose exec` resets `HOME` for a uid with no passwd entry; the XDG vars
  are repeated in compose so exec sessions are quiet.
- paddlepaddle-gpu breaks torch (nccl cu12 vs cu13 on one path). PP-OCR runs under
  ONNX Runtime instead: 1.7s vs 33s on the same frame.
- Next 16 does not carry a server action's closure. The transfer confirm action
  failed with `ReferenceError: memberId is not defined` only when pressed.

---

## 7. Repo map

```
backend/
  src/cua/
    schema/       typed contracts — depends on nothing
    calibration.py  every tuned perceptual threshold, with its measurement
    perception/   screen, detect, ocr, merge, som, index
    action/       base protocol, browser, offline (recorded frames), desktop (seam)
    resolve/      resolver ladder, normalizers, templates, verification
    discovery/    llm, actions (the action space), prompts, loop, synthesize
    replay/       engine, scan, outcomes
    policy/       allowlist + risk + declared conditions, redaction seam
    escalation/   control token, X-layer capture
    evidence/     writer, structured log
    catalog/      store, tool manifest, learn (outcome by demonstration)
    runtime/      session lifecycle, composition root
    api/, cli.py  control plane + demo path
  fixtures/     hand-written capabilities the smoke scripts run
  policies/     guardrail config
  scripts/      five smoke checks
targetapp/    mock credit-union back office + /dev fault panel
console/      operator UI: runs, per-step frames, policy, handoff
artifacts/    recorded capabilities (v1, and v2 with a learned outcome)
evidence/     per-run frames, observations, step logs, results
```
