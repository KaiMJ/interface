# Working notes — state, traps, and what is next

The internal companion to the three documents a reviewer reads. Not a deliverable.

| | |
|---|---|
| [`README.md`](README.md) | how to run it |
| [`REPORT.md`](REPORT.md) | the design and why it is shaped that way — the arguments live there, not here |
| [`evidence/README.md`](evidence/README.md) | which run shows what |
| **this file** | current state, the traps that already cost time, and the ordered next steps |

Branch `dev`. 213 tests, ruff and mypy strict clean, `make test` needs no browser,
no display, no model and no target app.

---

## 1. Where it is

**The whole thread works end to end against the live application.** A real xAI run
records a capability; replay executes it deterministically and returns typed outputs;
the same artifact returns a business outcome for a member that does not exist; a risky
step parks and hands the live session to a human; every run leaves evidence.

| Area | State |
|---|---|
| `schema/`, `config.py`, `calibration.py`, `clock.py` | real, tested; artifacts validate their own references at construction |
| `perception/` | real, measured against the app; settles on pixels, falls back to text |
| `action/` | browser + offline real; desktop is a documented seam |
| `resolve/` | real, tested — ladder, normalizers, templates, verification |
| `replay/` | real, ~50 tests over the taxonomy, ambiguity, geometry and output bounds |
| `discovery/` | real, exercised by a live model |
| `catalog/`, `policy/`, `evidence/`, `escalation/`, `runtime/`, `api/`, `cli.py` | real |
| `console/` | runs, per-step frames, policy, handoff |
| `targetapp/` | real, all flows and faults |
| Per-app configuration | `policies/<app>.yaml` + `--app`; `tests/test_apps.py` stands up a second app from YAML |
| `diagnose.py` | real, 8 tests — one model call over a terminal run's evidence, proposing a policy entry a human applies |
| **Screens** | declared and enforced, never derived — see item 1 below |

## 2. What has been measured

| | |
|---|---|
| perception, warm | ~0.9s per observation on the GPU (2.6s on a CPU); OCR is ~95% of it |
| a dense member page | 143 elements — 8 detector boxes, 135 OCR lines |
| replay, 5-step capability | ~10s end to end |
| discovery run | 5 model calls, ~90s, `xai/grok-4.3` |

Two facts worth carrying:

1. **OCR dominates.** If perception ever needs to be faster, that is the only place
   worth looking.
2. **On this surface OCR supplies 135 signals to the detector's 8.** Enterprise
   back-office screens are dense text with few icons. The merge step is where the
   value is.

---

## 3. Next, in order

Ordered by what a reviewer would ask about, then by what an unseen site would need.

### 1. `cua learn-screens` — the one real gap

`Screen` is in the schema and replay asserts it. Nothing produces one, so every
capability declares none. Deriving from a single run named the member profile
`riverside_004`, after the member's *branch* — data, not a screen, so it would have
refused to run for anybody else.

Two runs with different inputs separate them: identical across both is the
application, differing is the record. The comparison `catalog/learn.py` already
performs, asking about sameness instead of difference.

*Shape.* `cua learn-screens <cap> --input <alternate values>`: replay twice,
intersect the frames step by step, name each screen from the longest invariant line
the other screens do not show. Emit a new draft version, exactly as `learn-outcome`
does.

*Why it matters beyond correctness.* What separates chrome from data across two runs
separates a vendor product from one tenant's branding across two institutions, and a
per-tenant override then attaches to a screen — one reviewable diff — rather than to
every artifact passing through it.

### 2. A recorded write capability

`cap_transfer_funds` exists only as a hand-written stand-in
(`backend/scripts/smoke_capabilities/transfer_funds.json`).
The transfer form is three `<select>` elements, which the discovery loop has not
been pointed at — a real perception question (a dropdown's options are not on
screen until it is open), not a gap in the plumbing.

### 3. Multi-run stability, gating approval

Replay N times, report a flakiness signal, gate `draft → approved` on it. The
approval gate exists; today a human is the only evidence behind it.

### 4. The prompt experiment

Strip the rules from the discovery system prompt, re-run the same goal, compare
steps taken and discards. Five model calls; would replace an opinion in REPORT with
a number.

### 5. Bring your own session

`BrowserDriver.start` launches a fresh profile, so every run begins logged out and the
only way in is the `sign_on` recipe. On an unseen site the first login may be SSO, MFA,
or a consent screen — writing a recipe before seeing it is guesswork.

*Change.* Two more ways in: `launch_persistent_context` against a profile directory (a
human signs in once over VNC, later runs inherit it), and `storage_state`
import/export. Both belong in `Session`. Neither touches the schema — a capability
starts from an authenticated state and says nothing about how it got there, which is
why no artifact references a credential.

*Also:* an SSO redirect leaves the allowlist, and `check_url` runs after every
click, so it would deny the login itself. The allowlist has to name the auth
origins for that site — configuration, not code.

### 6. Re-measure calibration, do not re-guess it

`calibration.py` holds every perceptual threshold with the measurement that set it, all
taken on one surface. Two are surface-dependent enough to expect trouble:

- `container_frame_area = 0.15` — above this a detection is treated as a container
  and does not absorb text. A site with large cards will exceed it and lose labels
  the same way the sign-on panel did before it existed.
- `row_tolerance = 0.008` — ~7px at 900px tall. A site with taller line spacing
  merges rows; one with tighter spacing splits them.

`ocr_det_side_len` is already a setting and should be reviewed for text size.

*Change.* Nothing structural — run `smoke_observe.py --url <page>` on three or four
pages, read the row and label findings, adjust with the measurement recorded. Resist
adding a threshold; prefer deleting one. The last deleted (`neighbour_max_gap`) had
been quietly wrong for days.

### 7. Console gaps

- **A catalog panel.** The catalog is the agent-facing surface and the console does not
  show it. Capabilities with their contracts, `approve` as a button, and **invoke with
  typed inputs** — the same call an agent makes, from the operator's screen.
- **A discovery run in progress**, following its steps as they land. The SSE stream
  exists (`/runs/{id}/events`) and nothing consumes it; runs now write a `running`
  result before their first step, so the list is already correct.
- A capability with no declared outcomes, said explicitly rather than shown as an
  empty list.
- A run that escalated and was aborted, distinguished from one that resumed.

### 8. Tightening

- `ScanAdvance.CLICK_ANCHOR` — pagination — has never run. Real lists paginate.
- `Screen.signature` is a full `Checkpoint`, which permits a scoped screen
  signature — probably meaningless. Narrow it or say why not.

### 9. From the last review pass

Landed: `cua diagnose`, app-level business outcomes, output constraints, ambiguity
stopping a write, approval enforced on `/invoke`, the viewport check, `volatile_text`,
boundary-aware parameterization, and a generated evidence index. What that pass
turned up and did *not* close, in order:

- **A second read on every declared output.** Re-observe, re-extract, and disagreement
  between the two reads is `EXTRACTION_UNSTABLE`. One extra observation on the final
  step, and it is the only thing that reaches the residual constraints cannot: a
  misread that is type-valid *and* inside its bound.
- **`cua diff <cap> v1 v2`.** Semantic diff over data the artifact already carries.
  Approval without a diff is a rubber stamp and re-recording produces a version every
  time. ~80 lines, demoable in the console.
- **Reconciliation for the ambiguous write.** A risky capability naming a read-only
  one plus an expected delta, returning `committed | not_committed | indeterminate`.
  Designed in REPORT §7; the shape depends on the application, which is why it is
  written up rather than built.
- **A DOM/AX perception source.** Deliberately not built (REPORT §7 says why), but the
  cost is now stated rather than implied: one class emitting the same `Element` list
  plus a once-per-session coordinate offset, ~50ms against ~900ms on modern web.
- **The fault tiers in CI.** `smoke_recover.py` exercises them live and the unit tests
  cover the same tiers against fakes; what is missing is the middle — replaying
  committed frames from a real fault run through `--frames`. `modal` and `expired`
  are screen states and would replay faithfully; `slow` is a timing property and
  would not. Worth doing when the evidence set is regenerated.

---

## 4. Traps — things that already cost time

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
- Detector boxes are tighter than OCR lines (29x16 vs 41x23 on the same button), so
  text-inside-control alone leaves every button anonymous.
- Adjacent OCR line boxes touch. "Overlaps at all" put two table rows in one band
  and returned the neighbouring account's balance.

**Discovery**
- A discarded step may already have changed the screen. Dropping it leaves the
  recording missing a state transition and replay starts on a screen it never
  reaches.
- The entry navigation must be *recorded*, not just performed.
- `prune` renumbering steps cut the link between an extraction and its output.
- Models answer `expect` with descriptions ("the profile is shown") unless the tool
  says, with examples, that it is matched as a substring of screen text.
- …and once told that, they answer with the record's own data. `find_and_act` made
  `expect` required on a read, where the only text guaranteed to be on screen after
  the action is the value just read: a recording asserted `$18,204.55` and failed
  every replay for another member, having navigated perfectly. Fixed in three places,
  because a description alone is advice — the field is gone from the read tool
  (`find_and_extract`), an amount or a date is refuted before it becomes a checkpoint
  (`durable_expect`), and the model is told mid-run so the next step is better. A
  declared input is exempt: `parameterize` turns it into a placeholder that holds.
- The residual is stated rather than closed: `Marcus Webb` is data that looks like a
  heading, and one frame cannot tell them apart. Two runs with different inputs can —
  the same comparison item 1 needs for screens.

**Docker / build**
- `docker compose exec` resets `HOME` for a uid with no passwd entry; the XDG vars
  are repeated in compose so exec sessions are quiet.
- paddlepaddle-gpu breaks torch (nccl cu12 vs cu13 on one path). PP-OCR runs under
  ONNX Runtime instead: 1.7s vs 33s on the same frame.
- Next 16 does not carry a server action's closure. The transfer confirm action
  failed with `ReferenceError: memberId is not defined` only when pressed.
- **Source changes need the image rebuilt** (`docker compose build desktop`) or
  copied in. `docker compose up -d desktop` recreates from the image and silently
  discards a `cp`, which has cost time twice. Under Docker Desktop on WSL,
  `docker compose cp` into a container with bind mounts can fail while still
  exiting 0 — use `docker cp <container>:...` and check the output.

---

## 5. Not doing, and why

- **A full UI map.** The right abstraction at N capabilities on one app, and the
  wrong thing to build at two. Item 1 is the seam it would grow from, and it is
  derived from artifacts rather than maintained beside them, so it cannot drift
  from what replay sees.
- **A router that picks a capability for a goal.** The agent-facing product decides
  *what* to do; this system is how it does it. `/capabilities/manifest` is our side
  of that line.
- **Prettier target app.** Its density is the realistic hard case. Making it nicer
  flatters the system in the one way that matters least.
- **LLM prompt tuning beyond what a measurement demands.** The loop self-corrects
  through the `expect` check. Item 4 is the version worth running.
