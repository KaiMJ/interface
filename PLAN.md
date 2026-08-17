# Plan — running against a site we have not seen

Written for a fresh context. It assumes nothing about the conversation that
produced the current code.

**What exists.** An LLM drives a real UI to complete a goal, the run is recorded
as a typed capability artifact, and the artifact replays deterministically with no
model in the loop — plus policy guardrails, an error taxonomy, evidence, and a
human handoff that takes over the same live browser session. `README.md` runs it,
`HANDOFF.md` is the current state and the traps, and the design reasoning lives in
the module docstrings and the commit history. This file is only about what comes
next.

**What comes next.** Point it at a live site nobody has looked at, with no
target-app knowledge available. Everything below is ordered by that.

---

## What "ready for an unseen site" means

Concrete acceptance, in the order you would hit them:

1. `cua probe --url <site>` reports what perception sees on a page nobody
   calibrated it for, and says which of its assumptions do not hold there.
2. A human signs in once by hand; the automation reuses that session.
3. `cua discover --app <name> --goal "..."` records a capability against that
   site, with that site's policy — no edits to Python.
4. The capability replays cold and returns typed outputs.
5. A bad input produces a declared business outcome rather than a failure.
6. Nothing the agent does leaves the allowlist, and nothing irreversible happens
   without a human.

Anything that has to be edited in `backend/src/` to reach one of those is a bug in
the architecture, not a configuration step.

---

## P0 — will stop it working at all

### 1. Settle on content, not on pixels

`XDisplayScreen.capture` hashes the raw pixel buffer, and `Perceiver.settle` waits
for two consecutive identical hashes. On the target app that converges because
nothing on the page moves. On a real site, a blinking text caret, a spinner, a
carousel, a live clock or an animated banner means **no two frames are ever
identical**, and every step dies with `Unsettled`.

This is the single most likely reason a first run against a new site produces
nothing at all.

*Change.* Keep the pixel hash as the fast path — when the screen truly is static
it is the cheapest correct answer. When it does not converge within its budget,
fall back to comparing what the frame *says*: two full observations whose readable
text and control boxes match are settled, even if their pixels differ. "The words
stopped changing" is the property the resolver actually depends on; identical
pixels was only ever a cheap proxy for it.

Costs two extra observations (~4s) on animated pages and nothing on static ones.
Record which path settled, in the step result — a run that settles by text on
every step is telling you something about the surface.

### 2. Bring your own session

`BrowserDriver.start` launches Chromium with a fresh temporary profile, so every
run begins logged out, and the only way in is the `sign_on` recipe in policy. On
an unseen site the first login may be SSO, may be MFA, may be a consent screen
before the form. Writing a recipe for that before you have seen it is guesswork.

*Change.* Two supported ways in, and the recipe stays for the case where it fits:

- `launch_persistent_context` against a profile directory, so a human can sign in
  once — over VNC, on the same display — and every later run inherits it.
- `storage_state` import/export, so a session can be captured elsewhere and
  handed over.

Both belong in `Session`, which already owns "the thing that outlives a run".
Neither should touch the artifact schema: a capability starts from an
authenticated state and says nothing about how it got there. That property is
worth protecting — it is why no artifact references a credential.

*Also:* an SSO redirect leaves the allowlist. `check_url` runs after every click,
so it will deny the login itself. The allowlist needs to name the auth origins for
that site, which is configuration, not code (see 3).

### 3. One app per configuration, selected per run

`Settings.policy_file` is a single global path and `policies/targetapp.yaml` is a
single file. Two applications cannot coexist.

*Change.* `policies/<app>.yaml` per application; `--app <name>` on `discover`,
`replay` and `invoke`, defaulting to the capability's own `AppRef.name`. The
capability already records which app it belongs to and nothing reads it — this is
the reader it was waiting for. `Catalog.list(app=...)` follows for free.

A new site is then: one YAML file, one `--app` flag. That is the test of whether
the overfitting is really gone.

*The file should carry, per app:* allowlist patterns (including auth origins),
permitted primitives, risky disposition, the sign-on recipe if one fits, the
surface description handed to the model, declared recoveries, app errors and
escalations. All of these exist today; they just live in one file with one name.

**Start a new site with `risky_disposition: block`.** You do not know what mutates
there yet. Loosen to `confirm` once you do.

### 4. A first-contact diagnostic

`scripts/smoke_observe.py` is the right tool and it asserts `"dolores"`,
`"12345"`, `"savings"` — it can only ever pass on the app it was written for.

*Change.* Promote it to `cua probe --url <url> [--expect "..."]`, and have it
report rather than assert:

- how many elements, split by detector versus OCR, and how long a frame takes
- how many detected controls carry text — **zero is the signal that the merge
  thresholds do not fit this surface**
- whether rows reconstruct: cluster into rows, print the widest few, and flag any
  row that spans more than one visual line
- whether the page settles by pixels or only by text (item 1)
- any `--expect` strings that are not readable, which is how you check the anchors
  a capability would need before recording one
- the annotated set-of-marks frame, written out, because a human looking at it
  answers most of these faster than any assertion

This is what you run first on a new site, and its output is what tells you whether
to touch calibration (item 6) before recording anything.

---

## P1 — will make it wrong rather than stuck

### 5. Referential integrity in the schema

`Capability` has no validators. Nothing checks that:

- step ids are unique
- `OutputSpec.from_step` names a step that exists, and one that extracts
- `step.screen` names a declared screen
- every `{{placeholder}}` in a step, checkpoint, predicate or detector names a
  declared input
- `AppRef.base_url_pattern` compiles as a regex
- `Constraints.not_equal_to` names another declared input

Every one of those is a recording that loads fine and fails mid-run against a
member's account. On a surface we know, synthesis happens to produce consistent
artifacts. On one we do not, recordings will be rougher, and the cheapest place to
catch that is the moment the artifact is constructed.

*Change.* A `model_validator` on `Capability` covering the list. It is the same
argument as `--frozen` on a lockfile: fail where it is cheap, not where it is
expensive.

### 6. Re-measure calibration, do not re-guess it

`calibration.py` holds every perceptual threshold with the measurement that set
it, all taken on one surface. Two are surface-dependent enough to expect trouble:

- `container_frame_area = 0.15` — above this a detection is treated as a container
  and does not absorb text. A site with large cards will exceed it and lose labels
  the same way the sign-on panel did before it existed.
- `row_tolerance = 0.008` — ~7px at 900px tall. A site with taller line spacing
  merges rows; one with tighter spacing splits them.

`ocr_det_side_len` is already a setting and should be reviewed for text size.

*Change.* Nothing structural — run `cua probe` (item 4) on three or four pages of
the new site, read the row and label numbers, and adjust with the measurement
recorded in the docstring the way the existing ones are. Resist adding a
threshold; prefer deleting one. The last one deleted (`neighbour_max_gap`) had
been quietly wrong for days.

### 7. Screens derived from two runs

`Screen` exists, steps may declare one, and replay asserts it and fails
`WRONG_SCREEN` naming where it actually is. Nothing produces them: deriving from a
single run was tried and named the member profile `riverside_004`, after the
member's *branch* — data, not a screen, so the capability would have refused to
run for anybody else.

Two runs with different inputs separate the two: text identical across both is the
application, text that differs is the record. That is the comparison
`catalog/learn.py` already performs for outcome detectors, asking about sameness
instead of difference.

*Change.* `cua learn-screens <cap> --input <alternate values>`: replay twice,
intersect the frames step by step, and name each screen from the longest invariant
line that the other screens do not show. Emit a new draft version, exactly as
`learn-outcome` does.

*Why it matters beyond correctness.* This is the multi-tenant answer made
concrete. What separates chrome from data across two runs separates a vendor
product from one tenant's branding across two institutions, and a per-tenant
override then attaches to a screen — one reviewable diff — instead of to every
artifact that passes through it.

---

## P2 — the console, and the cases it does not cover

The console shows runs, per-step frames with the set-of-marks overlay, the
capability contract, the synthesis proposals and rejections, the policy, and the
intervention handoff. Four gaps, in order of how much they mislead:

### 8. A run in flight reads as `failure`

`ReplayResult` is constructed with `status=FAILURE` and written to `run.json`
before each step, so the console shows a running replay as failed until it
finishes. Add `RunStatus.RUNNING` as the initial value and set the real status at
the end. One line, removes a genuinely misleading display.

### 9. No catalog view

The catalog is the agent-facing surface and the console does not show it. Add a
panel listing capabilities with their contracts, `approve` as a button, and
**invoke with typed inputs** — the same call an agent makes, from the operator's
screen. That is also how a reviewer tries a capability on a new site without a
terminal.

### 10. Cases worth having a view for

- a discovery run in progress, following its steps as they land (the SSE stream
  exists and nothing consumes it)
- a capability with no declared outcomes, said explicitly rather than shown as an
  empty list
- a run that escalated and was aborted, distinguished from one that resumed
- more than one app, once item 3 lands

---

## P3 — tightening, once the above works

- `Predicate.match`, `Scan.stop_when` and `FindAndActStep.scope_extent` are bare
  `Literal` strings while everything comparable (`Relation`, `ScanAdvance`,
  `MultiplePolicy`) is an enum. Make them enums; the inconsistency is the kind
  that produces a typo nobody catches.
- `Target.offset` is an unbounded pair of floats and should be `0..1`.
- `Screen.signature` is a full `Checkpoint`, which permits a scoped screen
  signature — probably meaningless. Narrow it or say why not.
- `ScanAdvance.CLICK_ANCHOR` — pagination — has never run. Real lists paginate.

---

## Not doing, and why

- **A full UI map.** The right abstraction at N capabilities on one app, and the
  wrong thing to build at two. Item 7 is the seam it would grow from, and it is
  derived from artifacts rather than maintained beside them, so it cannot drift
  from what replay sees.
- **A router that picks a capability for a goal.** The agent-facing product
  decides *what* to do; this system is how it does it. `/capabilities/manifest`
  is our side of that line.
- **Prettier target app.** Its density is the realistic hard case. Making it
  nicer flatters the system in the one way that matters least.
- **LLM prompt tuning beyond what a measurement demands.** The loop self-corrects
  through the `expect` check. One experiment is worth running — strip the rules
  from the system prompt, re-run the same goal, compare steps taken and discards —
  because it replaces an opinion with a number in the write-up.

---

## Decisions already made — do not relitigate without new evidence

| Decision | Why |
|---|---|
| Vision-first perception (screenshot → detector + OCR → merged elements) | the only path that generalizes to legacy web and desktop; no DOM assumption anywhere |
| Playwright as an input engine, never a locator library | there is no `page.locator()` in the codebase and there should not be |
| The X display is the one coordinate space | the model and the operator argue about the same picture |
| The discovery action space *is* the artifact's step vocabulary | recordings are replayable by construction, not by post-hoc inference over a transcript |
| `expect` on every action, checked immediately | every checkpoint in a saved artifact has already passed once, on the run that wrote it |
| Parameterize by exact match against declared inputs | nobody guesses which numbers are ids; the caller declared them |
| Outcome detectors are falsified, then learned by demonstration | a detector for an unseen screen is a guess; measured, the model proposed a column header that appears on every screen |
| Replay constructs no model client at all | determinism is a construction-time property, checkable rather than promised |
| VNC handoff with X-layer capture | Playwright cannot observe a click it did not issue |
| Redaction: declared values real, pattern masking a seam | stated as a cut where it lives |

---

## Still owed as a deliverable

`REPORT.md`, seven mandated headings (`ASSIGNMENT.md` §6.2). Its material is in
the module docstrings, this file's decision table, and the commit messages — the
previous `PLAN.md` organized under those headings was deleted with this rewrite
and is recoverable from git history if the scaffold is wanted. §4 should be
written around item 7: the tenant story is now a mechanism, not an assertion.
