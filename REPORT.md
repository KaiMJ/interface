# Design write-up

A system that lets an LLM discover how to complete a goal in an application with no
API, records what it learned as a typed capability artifact, and replays that
artifact deterministically with no model in the decision loop.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the AI agent invokes it in production.

`README.md` runs it. `evidence/README.md` indexes the runs that prove it.

**The decisions, at a glance.** Everything below is the argument for one of these.

| Decision | Why | Cost accepted |
|---|---|---|
| Vision-first perception; Playwright as an input engine, never a locator library | the only path that generalizes to legacy web and desktop — no DOM assumption anywhere | ~0.9s per observation on the GPU, 98% of it OCR (2.6s on a CPU) |
| The X display is the one coordinate space | the model, the resolver and the human operator argue about the same picture | screenshots are of the whole display, not the viewport |
| The discovery action space **is** the artifact's step vocabulary | recordings are replayable by construction, not by inference over a transcript | the model cannot express anything replay cannot do |
| Replay is *handed* collaborators that cannot reach a model | determinism is checkable at construction, not promised in a doc | a gated-LLM recovery tier has to stay unbuilt (§7) |
| `expect` on every discovery action, checked immediately | every checkpoint in a saved artifact has already passed once | a step whose expectation fails is recorded without one, or discarded |
| Waiting is polling, at **both** ends of a step | "the row is not there" and "not there *yet*" are the same picture whether you are arriving at a screen or leaving one | a genuinely missing target costs a step timeout before it is reported |
| Retry is gated on `risk`, and only *after* acting | reads can always be repeated; a submit whose checkpoint we could not read cannot be | a flaky risky step is an escalation, never an automatic second attempt |
| The deployment decides which install a run acts on, not the recording | an allowlist that spans tenants — which is what makes an artifact reusable — cannot also be what stops a run from driving the wrong one | one origin per app per deployment is assumed (§4) |
| Business outcomes are *falsified*, then learned by demonstration | a detector for a screen nobody has seen is a guess | a fresh recording often declares no outcomes until taught |
| Parameterize by exact match against declared inputs | nobody guesses which numbers are ids — the caller declared them | a value the caller did not declare stays a literal |
| Guardrails per **application**, in one YAML file, not per capability | a session interstitial interrupts every flow; duplicating it guarantees drift | a second app is a file, and `tests/test_apps.py` proves it |
| Control transfer is a token with one holder, and `NOBODY` is a real state | "the agent clicked while I was typing" becomes impossible, not unlikely | the automation parks rather than degrading |
| Redaction: declared values real, pattern masking a seam | the guarantee that can't miss is implemented; the one that can is named | §6 states the limit rather than implying coverage |
| OCR is not trusted to be right, only required to be *checkably wrong* | every read is asserted against a checkpoint or a declared bound, so a misread stops the run instead of answering it | a misread that is still type-valid *and* still passes its bound is undetectable (§3) |
| Business-outcome detectors are owned by the **app**, declarations by the capability | the wording is shared by every flow that meets the screen; whether a flow may return it is not | a capability inheriting an outcome the app does not declare fails at run start |
| Healing is a **proposal**, never a repair — `cua diagnose` runs after the run is terminal | replay stays a path with no model on it; a model that could edit a guardrail mid-run is not a guardrail | an unforeseen screen still costs one escalation, once |

---

## 1. Architecture

**Vision-first, everywhere.** Perception is a screenshot of the X display through
an icon detector (OmniParser) and OCR (PP-OCR under ONNX Runtime), merged into one
`Element` list. There is no `page.locator()` anywhere in the codebase and there
should not be. Playwright is used as an *input engine* — it moves a mouse and
presses keys on a display — and never as a locator library.

It costs ~0.9s per observation against ~50ms for a DOM query, and the shape of
that cost is worth stating exactly, because it is the whole of this system's
latency. Measured on the dense member-profile screen at 1440×900 (145 elements),
five trials, `scripts/bench_perception.py`:

| | |
|---|---|
| text recognition (PP-OCRv6, GPU) | **886 ms** |
| control detection (OmniParser YOLO, GPU) | 37 ms |
| merge | 5 ms |

Text recognition is ~95% of an observation, an observation is ~95% of a replay's
wall clock, and everything the engine itself does — resolving, policy, the
driver's round trip — is single-digit milliseconds. There is one number to
optimise and it is OCR.

Three things follow, and all three are in. **OCR runs on the GPU** through the
torch backend of the same PP-OCR models (2646 ms → 886 ms, identical output):
`onnxruntime-gpu` is not the route, because it ships CUDA 12 wheels while the
torch this image already needs is CUDA 13, so its CUDA provider loads and then
registers no device — a failure that looks like success. **A step does not
photograph the same screen twice**: the frame a step's verification settles on is
the frame the next step starts from, since nothing acts in between, which halves
the observations per step. **A poll compares hashes, not text**: a checkpoint that
has not come true yet is re-checked against a frame digest, and a byte-identical
frame cannot classify differently, so waiting on a slow page costs a screen grab
instead of a full read.

Every step records its own split (`StepResult.phases`), so this stays a
measurement rather than a belief and a regression shows up in the console.

**What accuracy is being assumed, and what is not.** Two things make OCR a
defensible foundation here, and only the second is a design decision. Enterprise
back-office applications are the *easy* case for text recognition rather than the
hard one — system fonts at a fixed zoom on a display whose geometry we control,
high contrast, no perspective, no compression, nothing handwritten. This is not
scanned-document OCR. But accuracy is not what the design rests on: every read is
either asserted against a checkpoint the recording already passed once, or bounded
by a declared output constraint (§3). A misread does not become a wrong answer, it
becomes `CHECKPOINT_FAILED` or `OUTPUT_REJECTED`, naming the step and what was on
screen instead. The guarantee is not "OCR is right"; it is **"when OCR is wrong,
the run stops and says so."** The residual — a misread that is still type-valid and
still inside its declared bound — is stated as a limit in §3 rather than covered.

It buys the property that matters here: **nothing above perception knows what kind
of surface it is looking at.** A legacy frameset, a Java desktop app and a modern
React app all arrive as the same `Element` list. A DOM-based design would be faster
today and would need rewriting from the resolver up the first time it met an app
without one — which §1 of the brief says is the common case.

**One coordinate space.** The screenshot is of the whole display, not the browser
viewport, so the model, the resolver, the input layer and the operator watching over
VNC all argue about the same picture. Coordinate translation between layers is a
category of bug this design does not have. (It cost a day to get there — see
`PLAN.md` §4.)

**Layers, and what each one is forbidden to know.**

| Layer | Knows | Must not know |
|---|---|---|
| `schema/` | typed contracts | anything; it imports nothing |
| `perception/` | pixels → `Element` | what a capability is |
| `action/` | `Point` → input event | what an element means |
| `resolve/` | `Target` + `Observation` → coordinate | how the coordinate is used |
| `replay/` | artifact → result | that a model exists |
| `discovery/` | goal → recording | how replay executes |
| `policy/` | what is permitted | which path is asking |
| `diagnose` | a finished run's evidence → a proposal | anything live; it never holds a session |

The two runners are built by one composition root (`runtime/wiring.py`) that
differs in exactly one collaborator:

```
build_discovery()   real LLM client, resolver with allow_vlm=True
build_replay()      no LLM at all,   resolver with allow_vlm=False
```

Replay is not *asked* to avoid the model; it is handed collaborators that raise if
it tries. `unset XAI_API_KEY` and replay behaves identically. Determinism is a
construction-time property a test can assert, not a claim in a document.

**The action space *is* the artifact's step vocabulary.** The discovery agent can
only express things replay can execute (`Primitive`: navigate, click, type, key,
scroll, extract, wait, assert — plus `find_and_act`). Recordings are therefore
replayable *by construction*, not by post-hoc inference over a model transcript.
This is the single decision that most reduced the surface area of everything else.

**Trade-offs taken.** Single process, no queue, one browser on one display; the
concurrency limit is made visible in `SessionPool` rather than discovered at
runtime — a second run is refused with a 409 naming the run that holds the session,
because an operator console makes starting one a single mis-click away. Async
driver with perception in a thread, because Playwright's sync API cannot run inside
a live event loop and OCR is CPU-bound.

**Every decision the system makes is recorded, including the boring ones.** Each
`StepResult` carries, beside its outcome, the three judgements that produced it:

| Record | What it answers |
|---|---|
| `policy` | what the guardrail decided *before* the step ran — allow as well as deny, which rule decided, and whether a step the recording declared `safe` was promoted to `risky` and on which pattern |
| `resolution_trace` | every rung of the resolver ladder and why each missed, not only the tier that won |
| `model_turn` | discovery only: which mark the model chose, which element that mark measurably *was*, how many candidates it was shown and how many were truncated, and what the loop then did with the answer (`kept`, `kept_without_checkpoint`, `discarded`, `rejected`) |

None of these are new computations — the system already made all three and threw
them away once it had acted on them. Keeping them is what separates evidence that
shows *that* a step happened from evidence that shows *why*, and it is what the
console renders. The failure mode this closes is specific: with only refusals
recorded, the evidence that a transfer was permitted is the absence of an entry.

### Against the original design sketch

`diagram.png` is the design drawn before any code. Five things differ, all
deliberately:

| Sketch | Built | Why |
|---|---|---|
| A **Router** deciding "catalog hit → replay, miss → discovery" | Cut | The agent-facing product decides *what* to do; this system is how it does it. `/capabilities/manifest` is our side of that line — a router here is someone else's component. |
| Resolver rung 4: **`vlm (gated)`** | Enum value + a method that raises | Discovery never needs it: the model picks from an enumerated set of marks, so it is never asked "where is this thing". On replay it must never exist. Naming the tier and leaving it unreachable is what makes "replay never calls a model" checkable rather than asserted. |
| `dom / ax` as a **default-off perception source** | `ElementSource.DOM`/`AX` exist; no implementation | It would make the easy surface easier and teach nothing about the hard one. The seam is one class. |
| `handle_login` as an **artifact** | A recipe in app policy, never an artifact | The sketch would have put a credential reference inside a versioned, shareable file. Sign-on is a precondition of every capability and a capability of none. |
| `Screenshot` as a primitive | Dropped; `KEY` added | Every step captures a frame anyway. `KEY` was needed the first time a form wanted Enter. |

Everything else — the step lifecycle (verify permission → resolve → verify target
→ execute → verify effect), the perception pipeline, the R-tree spatial index, the
five-way exit taxonomy, `find_and_act` with a `cell_equals` predicate and a named
extract column — is built as drawn.

---

## 2. Artifact schema

`backend/src/cua/schema/artifact.py`. Three readers have to be served at once, and
the shape falls out of that: a **calling agent** needs typed inputs, typed outputs
and a declared set of legitimate outcomes to branch on; a **human reviewer** has to
approve it without watching a video; the **replay engine** has to execute it with
no model present.

```jsonc
{
  "id": "cap_get_savings_balance", "version": 2, "status": "approved",
  "app": { "name": "targetapp", "vendor": null,
           "base_url_pattern": "^http://targetapp:8080(/.*)?$" },

  "inputs":  [{ "name": "member_id", "type": "string", "example": "12345",
                "sensitive": false, "constraints": { "pattern": "^[0-9]{5}$" } }],
  "outputs": [{ "name": "balance", "type": "number", "from_step": 4,
                "normalize": ["collapse_ws", "strip_currency"],
                "constraints": { "min": -1e6, "max": 1e8 } }],   // read it; is it possible?

  "steps": [{
    "kind": "act", "id": 4, "action": "extract", "extract_as": "balance",
    "risk": "safe", "screen": null,
    "target": { "intent": "read the balance beside the account",
                "target_desc": "the current balance cell in the account's row",
                "anchor_text": "{{account_nickname}}",       // tier 1: portable
                "relation": "right_of", "relation_index": 1, // the value has no text of its own
                "role": null, "name": null,                  // tier 2
                "bbox": { "x": 0.44, "y": 0.31, ... } },     // tier 3: logs drift when used
    "checkpoint": { "kind": "text_present", "value": "Available Balance" }
  }],

  "success": { "kind": "text_present", "value": "{{account_nickname}}" },
  "business_outcomes": [{ "name": "member_not_found" }],       // detector inherited from app policy
  "screens": [],                                             // declared, enforced, not yet derived
  "recording": { "run_id": "discover-e3d4f6b7", "model": "xai/grok-4.3",
                 "viewport": { "width": 1440, "height": 900 }, "step_count": 5 }
}
```

**Two shapes were rejected.** A raw model transcript fails all three readers. A
bare click track (`click(0.42, 0.71)`) fails the reviewer, fails risk classification
— a coordinate cannot be judged reversible — and fails cross-tenant reuse, because
rebranding moves every pixel. Every step therefore carries its *semantic intent*
alongside its coordinate.

**Targeting is a ladder, most portable first**, and the tier that succeeded is
recorded on every step result:

1. `anchor_text` — visible text at or near the target. Survives rebranding and
   relayout. May contain `{{param}}`, which is what makes data-dependent targeting
   possible ("the row for member `{{member_id}}`").
2. `role` + `name` — semantic match against detected elements.
3. `bbox` — the recorded position. Correct until something above it changes height;
   using it logs a drift event.

`relation` is what makes vision-first targeting work at all. There is no `for=`
attribute to follow: a form field is an empty box beside a label, a balance is a
cell to the right of "Available Balance". Recording *the relationship* is more
portable than a coordinate or a role, because rebranding rarely moves a value out
from beside its label.

**`find_and_act` is a first-class step type**, not sugar. A target's position in a
list is a function of the data, not the layout. Recording `scroll, scroll, click(y)`
is wrong four ways — the position drifts, the page drifts, the record may be absent,
the match may be ambiguous — and recording the *predicate* is right in all four
while staying deterministic: a fixed loop of (observe scope, evaluate predicate,
advance) with no model in it. It also carries `on_found_extract_column`, because a
row is not a value: a caller asking for an amount wants a number, not
`"08/10/2026 PACIFIC WIRELESS Telecom ($441.56)"`.

**Outputs carry constraints, and they are not decoration.** `Constraints` is the
same class the inputs use, pointed the other way, and it closes the one gap a
vision-first design has that a DOM-based one does not. A checkpoint says *where we
are*; it says nothing about whether the characters read out of the screen are a
number this capability may return. `18204.55` misread as `1820455` coerces to a
valid float, passes every assertion — the screen is the right screen — and is a
balance a downstream agent will quote to a member. A declared range turns that into
`OUTPUT_REJECTED`, which is a different answer from `EXTRACTION_FAILED` and sends
an operator somewhere else entirely: one means we could not read it, the other
means we read it and it cannot be right. Authored at review time rather than
derived, because the recording only ever saw one value and a bound inferred from
one observation is either that value or a guess.

**A business outcome's detector may be inherited from the application.** The two
halves belong to different owners, and it took a second capability on the same app
to see it: *what the screen says* is a property of the application — every flow
that searches for a member meets the same "no member matches" wording — while
*whether this flow can return that answer* is a property of the capability, and
only the capability can say it. So `policies/<app>.yaml` owns the detector and the
artifact declares `{"name": "member_not_found"}` to opt in. Copying the phrase into
each artifact instead would guarantee the copies drift, which is the same argument
that put recoveries in policy. Opting in stays explicit rather than automatic:
this list is the contract a calling agent branches on, and a capability
advertising an outcome it cannot reach is lying about the shapes it may return. An
inherited name the app does not declare fails at run start, before anything is
touched — the alternative is silent, because an unresolved detector does not error,
it simply never matches.

**Parameterization is by exact match against declared inputs.** The caller says
`member_id=12345`; synthesis turns every recorded literal `12345` into
`{{member_id}}`, longest value first — and only at token boundaries, because a bare
string replace also rewrites the account number `9912345` into `99{{member_id}}`,
producing an artifact that navigates somewhere perfectly valid that exists for
nobody. Nobody guesses which numbers are ids.

**The artifact validates its own references at construction**
(`Capability._referentially_intact`): unique step ids, `from_step` naming a step
that exists *and* actually extracts, every `{{placeholder}}` naming a declared
input, every `screen` declared, `base_url_pattern` compiling. Each is otherwise an
artifact that loads fine, passes review, and fails halfway through a run against a
member's account. It deliberately does *not* check whether an anchor exists on
screen — that is a claim about the application, answerable only by running it.

Versioned and gated: `draft → approved → deprecated`. `Catalog.save` refuses to
overwrite an existing `(id, version)`, so re-recording produces v(n+1) in draft and
leaves what production is calling untouched. Drafts never appear in
`/capabilities/manifest`: an agent able to call an unreviewed recording is running
unapproved automation against member accounts.

---

## 3. Determinism & error handling

**Determinism.** Replay constructs no model client (§1), and the resolver it is
handed raises rather than reaching the `VLM_GATED` tier — the tier exists in the
enum so that "replay never calls a model" is checkable by construction rather than
by reading the code for the absence of a call. Beyond that:

- **No `sleep()` on the replay path**, with exactly one exception: a recovery the
  policy explicitly declares as a wait. Everywhere else, waiting is `poll until the
  step's declared timeout` — "not true yet" and "not true" are the same picture, so
  they get the same loop. **Both ends of the step**, which was the correction: the
  engine polled the checkpoint it was *leaving* a screen on and gave the target it
  was *arriving* at one attempt. The asymmetry was invisible until a page took four
  seconds to render, and then it surfaced as `target_mismatch` — which reads as UI
  drift and is not. The gap is widest exactly where it is least visible: a step
  recorded without a checkpoint imposes no wait at all, so all of the previous
  step's latency lands on the next one.
- **Settle before resolving**, so a coordinate is never resolved against a page
  still laying out. Two tests in order: identical pixels between consecutive frames
  (cheap; what fires on a static enterprise screen), then, if that never converges,
  two consecutive *observations* whose readable text and boxes match. The fallback
  exists because a blinking caret, a spinner or a live clock means no two frames are
  ever byte-identical, and on such a surface the pixel test alone fails every step.
  Which one fired is recorded on the step result.
- **Normalizers are recorded in the artifact, not hardcoded**, so a replay compares
  strings exactly the way the recording did (`$1,234.56` vs `1234.56`).
- **A checkpoint per step, not only at the end.** A wrong click at step 3 fails at
  step 3 with a legible diff, rather than a plausible-looking wrong output at step 9.
  And every checkpoint in a saved artifact has already passed once — on the run that
  wrote it, because discovery requires an `expect` with every action and verifies it
  immediately.

**The error taxonomy is the point.** Four terminal classes, never conflated:

| Class | Meaning | Caller does |
|---|---|---|
| `success` | checkpoint held, declared outputs extracted | uses `outputs` |
| `business_outcome` | a legitimate answer: "no such member", "permission denied" | branches on `outcome.name` |
| `escalated` | stopped, handed to a human, may resume | waits or gives up |
| `failure` | something we do not understand | pages someone, with evidence |

Conflating the first two is the mistake the brief names outright, so a business
outcome exits 0 from the CLI and returns 200 from the API. `FailureKind` has 13
members and the test of whether it is real is that **each one maps to a different
operator action** — if two would prompt the same response they should be one entry.

**Detector evaluation order at each step is where the taxonomy is enforced**
(`replay/outcomes.py`, one function so the ordering cannot be re-litigated per call
site — the usual way a clean error model rots):

1. **Declared business outcomes** (from the *artifact*) — first, always. If the
   checkpoint ran first, "no such member" would be reported as a failed assertion
   rather than as the answer the caller asked for.
2. **Declared recoverable conditions** (from *policy*) — apply the handler,
   re-observe, and re-execute the step if it is safe to; count against
   `max_per_run`. Evaluated **before the step acts as well as after**, which is not
   a detail: the demo app's dialog deliberately does not move the page, so a
   recorded coordinate still resolves to the control underneath and a click issued
   into it is eaten. Detecting it only afterwards costs a step timeout and then
   reports the symptom. Past the cap it becomes a hard failure: dismissing the same
   modal eleven times means the dismissal is not working, and "eleven successful
   recoveries" is the wrong description of that.
3. **Declared conditions with no handler** (from *policy*), split by who can clear
   them — `app_errors` → `APP_ERROR`, `escalations` → park for a human. Before the
   checkpoint on purpose: both would otherwise arrive as "the checkpoint did not
   hold", which describes the symptom and hides the cause.
4. **The step's checkpoint** — the expected path.
5. **Anything else** — hard failure. Under vision we cannot enumerate every screen
   an enterprise app can produce, and guessing in a banking application is worse
   than stopping.

Note where each detector *lives*: business outcomes are per-capability, because
they are answers a particular flow can return. Recoveries, app errors and
escalations are per-application, because a session interstitial can interrupt every
flow on the app.

**Session expiry is a recovery on a read and an escalation on a write**, and the
line between them is one the system already draws. The original rule was "always
escalate", justified by not wanting the automation to hold a credential — an
argument that does not survive reading the code, since it already holds one and
types it at session start. What a mid-flow expiry actually destroys is not the
secret but the run's *place*: signing back in lands on the application's landing
page, several screens from where it was. So the only honest handler is to sign in
and run the capability again, and that is available exactly while nothing
irreversible has happened — which is what `risk: safe` on every executed step
means. A balance read at 3am re-runs itself; a transfer whose confirm step has
already fired parks for a person, because whether its first half committed is not
a question an engine may answer by doing the whole thing over. One restart per
run: a session that dies twice inside ninety seconds is not transient.

**Retry, and why the gate is `risk`.** A step is executed more than once in exactly
two situations, and the same question decides both.

| Grant | Budget | Why it exists |
|---|---|---|
| `on_error: retry` | `retries`, from the artifact | the recording knew this step meets transients the app has no detector for |
| a declared recovery fired and the checkpoint still did not hold | one, from the engine | that is the signature of an action an interstitial ate, and the recording cannot have declared a budget for a dialog the app grew after it was written |

Both are gated on **`risk`**, which is already the artifact's declaration of
reversibility — that is what the field is *for*, so a second declaration would be a
second thing to keep in sync. A step policy considers safe may run twice; a risky
one never does, at any budget, because re-clicking a submit whose checkpoint we
could not read is how one transfer becomes two. The gate uses policy's **effective**
risk, so a step promoted to risky from its intent is excluded too, and
`on_error: retry` on a risky step is refused at load time rather than at run time.

Order matters as much as the gate: the checkpoint is polled to its full deadline
*before* any re-execution, so a step whose action did land and was merely obscured
is never run twice. Re-execution is what happens when the evidence says nothing
happened. And the read/write line is what makes the whole thing safe to reason
about — waiting for a target to appear is a *read* and needs no gate at all, which
is why the poll-before-acting above is unconditional and the retry-after-acting is
not.

**Two bounds, because parking and recovering are both cheap enough to do forever.**
A recovery past `max_per_run` is a hard failure naming the condition — dismissing
the same modal eleven times means the dismissal is not working. A declared
escalation an operator resumes without clearing stops after two interventions, for
the same reason in a different currency: parking holds the only session, and a
queue that keeps re-issuing the same request teaches operators to ignore it.

Scan termination is the hardest case, because getting it wrong produces exactly the
confusion the brief warns about:

- exhausted the list with no match → **business outcome**. A legitimate answer.
- hit `max_advances` while the region was still changing → **hard failure**
  (`SCAN_INCONCLUSIVE`). We do not know whether the record is absent or we quit
  early, and "not found" here would be a confidently wrong answer.

Every failure reports the step, what was expected and what was observed. On the
target app, replaying member `99999` returns `member_not_found`; `44100` returns
`permission_denied`; toggling `error500` returns `app_error` — same artifact, same
code path, three different result classes.

**Ambiguity is a stopping condition on a write, and only on a write.** The
resolver reports how many elements a target matched; `_pick` then takes the one
nearest the recorded position, which is a good guess and is exactly as good as a
guess. On a read that is fine — a wrong choice fails its own checkpoint. On a
write, three rows whose button reads "View" are three different members, and the
recorded position is not evidence either, because which row is where is a function
of the data. `find_and_act` has defaulted to escalating on ambiguity from the
start because it is the obviously data-dependent case; the same rule now applies to
an ordinary click, gated on policy's *effective* risk so a mislabelled recording is
covered too. A plain click that lands here on a write is a recording that should
have been a `find_and_act`, and escalating says so to a person who can fix it.

The count that decides this is not the raw match count, and that distinction is
the whole of why it works. The default match mode is `contains` — it has to be, a
balance lives inside `"Available Balance: $18,204.55"` — so anchoring on "Search"
matches the button *and* the heading "Member Search", on a screen where a human
sees no ambiguity at all. Acting on the raw count would park a risky step on nearly
every screen, and a queue full of false escalations is how operators learn to
ignore the queue. So an element whose *whole label* is the anchor beats one that
merely contains it, and what survives is the ambiguity worth stopping for.

**Geometry is part of the contract, and now actually checked.** Coordinates are
normalized 0..1, so a display that is larger or smaller in the same proportions
changes nothing — every recorded box still covers the same fraction of the same
content. A different *aspect ratio* is a different matter: the application reflows,
and the recorded-bbox tier starts returning coordinates that are precise and wrong.
`recording.viewport` had been in every artifact since the first one and nothing
compared it to anything, which made "the recording viewport is part of the
contract" a sentence in a config file. Checked once per run on the first frame: a
scale change is a note, a shape change stops the run.

**Waiting has a failure mode that is not waiting long enough.** Settling tries
identical pixels first, then two consecutive observations that read the same. A
session countdown defeats both at once — `14:59` becoming `14:58` changes the
pixels and changes the text — so the page never settles by either measure and every
step burns two full timeouts before failing on a screen that was ready throughout.
This is not exotic: an application that declares session expiry as a condition is
very likely to render the countdown for it. `volatile_text` in app policy is how an
application names the lines that tick, and they are excluded from the settling
comparison and from nothing else. A countdown is still read, still perceivable,
still available to a checkpoint; it is just no longer evidence that the page is
moving.

**Drift, secondarily.** The resolution tier is recorded on every step, so anchor
resolutions decaying into `recorded_bbox` fallbacks is a free early-warning signal
across runs, long before a hard failure. `settled_by` is the same signal for a
surface that has started to animate.

**The known weakest link is OCR inside `find_and_act`.** Everything else on the
replay path is either exact (a URL, a recorded coordinate) or an assertion that fails
loudly; scanning a list is neither, and a predicate is only as good as the characters
that came back. Truncation (`ACME Corporat…`), currency formatting and column bleed
are all real. Three things contain it rather than pretend it away: recorded
normalizers, `cell_equals` against a truncated cell raising `Untestable` rather than
returning `False` (unanswerable is not the same as no), and ambiguity defaulting to
escalate. Output constraints are the fourth, and the one that covers extraction rather than
scanning: a value outside its declared range is `OUTPUT_REJECTED` rather than an
answer. What none of them cover is a misread that is *both* type-valid and inside
its bound — `18204.55` read as `18204.56`. That is the honest residual of a
vision-first design, it is the first place I would look if a replay returned a
wrong-looking number, and the answer to it is a second read compared against the
first, which is §7.

---

## 4. Heterogeneity & multi-tenant

**The surface seam is `Perceiver` + `Driver`.** Everything above them consumes
`Observation` and `Point` and knows nothing about screenshots, OmniParser, OCR, DOM
or accessibility APIs. Extending to a legacy frameset app or a native desktop
application means a new `Screen`/`Detector` pair and a new `Driver`; the artifact
schema, resolver ladder and replay engine are untouched. Demonstrated twice, not
just claimed:

- `action/offline.py` — replay against a previous run's recorded PNGs, no browser
  and no display. Same engine, same resolver, same policy, two collaborators
  swapped: `cua replay --frames evidence/replay-dd1bbee1/frames` re-derives every
  decision from pixels alone, and returns the same `18204.55`.
- `action/desktop.py` — a documented seam with the same protocol, showing what a
  native surface fills in (X input rather than CDP; the perceiver does not change
  at all, because it was never looking at a browser).

Framesets need no special handling *because* nothing reads a DOM — a frameset is one
picture like any other. That is what the ~2s observation cost buys.

**Multi-tenant reuse.** The unit of sharing is the **application**, and it is
configuration, not code:

```
policies/<app>.yaml          allowlist, permitted primitives, risky disposition,
                             recoveries, app errors, escalations, sign-on recipe,
                             identity (app, vendor, base_url_pattern), entry_url
```

`--app <name>` selects one. Replay defaults it to the capability's own
`app.name`, so an artifact is always executed under the guardrails of the
application it was recorded against and cannot be run under another's by accident.
Nothing in `backend/src` knows `targetapp` exists — `tests/test_apps.py` proves it
by standing up a second application (a fictional `coreview`, vendor `fiserv`) from
a YAML file in a temp directory and asserting the two do not leak into each other.

The tenant split is one line further down:

- **`base_url_pattern`** is a *pattern*, so one artifact is valid against
  `coreview.riverside.example` and `coreview.lakeside.example` alike.
- **`entry_url`** is the one per-institution fact, overridable by
  `CUA_TARGET_BASE_URL`.
- **A recorded URL is rebased onto it before every navigate.** The artifact
  contributes the *path* — a fact about the vendor product — and the deployment
  contributes the *origin* — a fact about the tenant.

So a second institution on the same vendor product is one environment variable, not
a re-recording. That is the mechanism that exists today, and it is deliberately the
cheapest thing that is actually true.

The third bullet is the one that turns the first two from a claim into a mechanism,
and its absence was a quiet hazard rather than a loud one. A capability records
absolute URLs, because that is what it navigated to; an allowlist is a pattern that
spans tenants, because that is what makes the artifact reusable. Compose those and
`cap_open_member`, recorded at riverside and replayed from lakeside's deployment,
navigates to riverside, passes the allowlist — it was written to match — and returns
a balance. Nothing fails. The answer is simply about the wrong credit union's
member, and it is reported as success. Rebasing means the deployment decides which
install a run acts on, and the allowlist is checked against what is actually
navigated to.

The limit, stated: this assumes one origin per app per deployment. A capability that
legitimately spans two hosts — an SSO bounce, a reporting subdomain — would need the
artifact to say which of its URLs are tenant-relative. That is a schema field, not a
redesign, and no capability recorded so far needs it.

**What makes it hold as tenants diverge** is that a step is identified by what a
human reads on the screen. Rebranding changes colours, logos and pixel positions; it
rarely changes the words "Available Balance" or moves the value out from beside them.
An anchor-plus-relation target survives what a coordinate or a CSS selector does not
— and the resolution-tier signal (§3) says *which* tenant has begun to drift, per
capability, before anything fails.

**Where that stops, stated plainly, because it is the part most easily overclaimed.**
Two institutions on the same vendor product do not only re-skin it; they configure
it. Riverside's screen says "Available Balance" and Lakeside's says "Current
Balance", because a bank changed a label. Trace that through the ladder: the anchor
misses, role/name misses, the recorded box is returned with `drift=True`, and
pre-click verification reads that region, finds it does not say what the recording
said, and stops. So the honest claim is: **same vendor product, same configuration,
a different install → one environment variable and no re-recording. A different
*configuration* → detected and refused, not repaired.** Failing loudly there is the
right behaviour and the harder half to get right — most designs would click
something. But it is not reuse, and the mechanism that would make it reuse is the
per-tenant override attaching to a derived `Screen`, which is next (§7) rather than
built.

**The long tail is the other half of this, and it is a mechanism.** No design
enumerates every screen an enterprise application can produce, so the question is
not how few unknown screens there are — it is whether an unknown screen costs an
escalation *once* or *every time*. `cua diagnose <run_id>` is the once. It reads a
terminal run's evidence, shows a model the lines that were on the failing screen
*numbered*, and asks which kind of condition it is and which line identifies it —
returning an **index**, never a phrase, so a detector it invented is not something
the interface can express. The answer is then falsified against every successful run
of the same capability: a line both runs read is chrome, and a detector on it would
report every success as that condition. What comes out is YAML for a person to
paste, not an edit — a model that could rewrite a guardrail is not a guardrail, and
a policy file whose every entry carries the argument for its own classification is
not something a program should regenerate.

Three rules bound it, and they are what make pointing a model at this defensible:
the proposal is produced **after** the run is terminal with no session open, so
replay still constructs no model client; the detector is **chosen** from the screen
rather than written; and a condition met on a step that **mutates** is never
proposed as recoverable, whatever the model concluded — downgraded to an escalation
in code, with the downgrade recorded rather than applied silently.

The reason this belongs in §4 rather than §3 is what it composes with. The patch
lands in `policies/<app>.yaml`, and the app is the unit of sharing — so the first
institution to meet a dormant-account interstitial pays for every capability at
every institution running that product, including the ones recorded next year. The
unknown-screen rate decays with use instead of staying flat, and the thing that
decays it is a human reviewing one YAML diff.

**Where per-tenant overrides should attach, and honestly do not yet.** `Screen` is
in the schema and enforced by replay (`WRONG_SCREEN` names where the flow actually
is), but nothing *derives* screens, so recorded capabilities declare none. This is
the thinnest part of the design and the reason is worth stating: deriving a screen
identity from a single run was tried and named the member-profile screen
`riverside_004`, after the member's *branch* — data, not a screen, so the
capability would have refused to run for anybody else. Separating chrome from data
needs two runs with different inputs and an intersection, which is
`cua learn-screens` in §7. It matters beyond correctness: what separates chrome
from data across two runs is what separates a vendor product from one tenant's
branding across two institutions, and a per-tenant override then attaches to a
*screen* — one reviewable diff — instead of to every artifact that passes through
it.

---

## 5. Escalation & handoff

**Detecting stuck.** Four routes, three of them declared in the artifact or policy
rather than inferred by a heuristic:

1. A step declares `on_error: escalate`, or `on_multiple: escalate` on a
   `find_and_act` — ambiguity on a write is unrecoverable, so escalate is the default
   and opting out is deliberate.
2. Policy classifies the action as risky and the app's disposition is `confirm`.
3. A declared `escalations` condition matches — an unexplained sign-on screen, or
   a session expiry on a run that has already executed a risky step.
4. Discovery detects a dead end — a repeated frame hash, the same tool call twice
   running, or the step budget — rather than burning the budget in a loop.

**Control is a token with exactly one holder**, and it is explicit state rather
than the implied consequence of nobody currently calling `click()`:

```
AUTOMATION ──escalate──► NOBODY ──take_control──► HUMAN
     ▲                                              │
     └──────────── resume ◄──── NOBODY ◄────release ┘
```

`NOBODY` is not ceremony. It is the interval between the automation stopping and
the operator connecting, and it is what makes "the agent clicked while I was
typing" impossible rather than unlikely. Control is surrendered *before* the
request is published, so there is no window in which an operator can see the
intervention and start clicking while the automation still believes it may act. The
check lives in the **driver**, before every input event — so an escalation path
that forgot to yield still cannot inject input while a human holds the token.

**The same live session, genuinely.** The run is parked on an `asyncio.Event`, not
unwound. What survives the transfer: the browser process, the X display, cookies
and session, the current page and any half-filled form, and the run's evidence
directory. The operator connects to `:6080` (noVNC on the same X display) and is
looking at the identical pixels the resolver was. The intervention request carries
what §3.6 asks for — capability, goal, current step, why it stopped, expected vs
observed, and the VNC URL — so an operator does not have to read a log to decide.

**Capturing what the human did happens at the X layer**, which is the part a
headful-Playwright design cannot do: Playwright observes the events it issues, and a
manual click is not one of them. JS listeners in the page would half-work — they miss
anything outside the page, break on a surface with no DOM, and put the audit trail
inside the thing being audited. An XRecord tap means the same code records a human
operating a browser and a human operating a desktop app. **Typed text is captured as
a keystroke count, never as content**: the operator may be entering a credential, and
an audit log of what someone typed into a password field is a worse liability than no
log. Screenshots at handoff and handback bracket it.

**Handing back re-observes rather than trusting the frame it parked on.** What
happens next depends on *when* the run stopped, and the two cases are genuinely
different:

- Parked *before* acting (a risky step awaiting confirmation): re-observe, then
  perform the step. The confirmation was the only thing missing.
- Parked *after* a failure (`on_error: escalate`): the human has unstuck it, so the
  step counts as satisfied and the run continues to the next one.

What it does **not** do is search forward for the first step whose checkpoint
already holds. It does not need to: the next step re-observes, asserts its declared
`screen`, and verifies its own checkpoint, so an operator who left the application
somewhere unexpected produces a loud `WRONG_SCREEN` or `CHECKPOINT_FAILED` naming
where it actually is — not a blind click. That is weaker than skip-forward and it
fails safe, which is the trade I would make again; skip-forward is worth building
once screens are derived (§4), because that is what would let it recognise where it
landed rather than guess.

`abort` ends the run as `escalated`, which is a distinct result class, not a
failure. And a step may park at most twice: an operator can resume without having
cleared the condition, and the run would then classify it again and park again —
forever, holding the only session. After two it stops with the condition named,
which is something a person can act on. A queue that keeps re-issuing the same
request is how operators learn to ignore the queue.

The queue is in-process, which is the deliberate mock: a control token that
outlives the session it coordinates is a token that lies, so persisting it would be
a worse answer here rather than a more complete one. Everything else is real. The
**mechanism** — token, park, capture, resume — is exercised on the live session by
`scripts/smoke_escalate.py` and by two tests
(`test_a_risky_action_waits_for_a_human_and_then_proceeds`,
`test_the_automation_cannot_act_while_a_human_holds_control`), and the operator
surface is the console: the queue, the context, take/resume/abort with a note, and
the same live display over noVNC with `viewOnly` bound to the token.

Interventions are **in the console's one page rather than behind an operator
route**. There was a `/operator/[run_id]` route and it was cut: a debug view and an
operator view of the same run differ only in whether you may touch it, and
splitting them means whoever is handling an escalation has to navigate away from
the evidence to find out why it happened.

---

## 6. Safety

Guardrails are checked in **one place on both paths** — discovery and replay call
the identical `Policy.check_action()` and `Policy.check_url()` before every action.
A guardrail that only guards the LLM is not a guardrail: a buggy or tampered
artifact is just as capable of submitting the wrong transfer as a confused model
is.

**Allowlist.** Permitted URL patterns and permitted primitives, per app, failing
closed — an unlisted primitive is denied, so an empty list denies everything. URLs
are checked on navigate *and after every action*, because a click can navigate. Note
what the shipped allowlist omits: the control plane (`:8000`), the console (`:3000`)
and noVNC (`:6080`). The agent runs on the same machine as its own operator surface,
so "localhost" would be a hole, not a convenience. A denial is a hard stop, never a
skip — an agent that silently continues past one produces a run whose result no
longer means what it says.

**Approval gates the unattended path, and only that one.** `/capabilities/{id}/invoke`
— the agent-facing route, where nobody is watching — refuses anything a human has
not approved, and refuses it twice: once in the route for a clean 403 without
opening a run directory, and once inside the engine so a call site added later
cannot bypass it. The console and the CLI deliberately do not, because replaying a
draft is *how* it gets reviewed. Absent from the manifest is not the same as
unreachable, and a caller that knows the id can name one.

**Risk.** Every step declares `safe` or `risky`; policy chooses `allow | confirm |
block`. Enforceable only because steps carry declared intent — `click(0.42, 0.71)`
cannot be judged reversible. Policy can also **promote** a step to risky when its
intent matches a mutation verb, one-directionally: a recording that mislabels a
submit as safe is the expensive failure, while a read misclassified as risky costs
one confirmation. In banking, latency is cheap and a silently wrong transfer is not.
A new application should start at `block` and be loosened once its mutations are
known.

**Secrets.** Credentials are resolved in the action layer, below the point where
anything is serialized, and typed with `secret=True` so the driver never logs them.
Sign-on is a recipe in policy and never an artifact — no artifact references a
credential, and `ValueType` has no `secret_ref` member for one to hide in. There is
no credential in the repo; `.env` is gitignored.

**Redaction, and where the line honestly is.** The tension is specific to a
vision-first design: a screenshot is simultaneously the evidence *and* the model
input, and a bank screen is PII by construction. A DOM-based system can redact fields
it knows about; we have pixels. So:

- **Declared** sensitive values are redacted for real. `InputSpec.sensitive` is a
  declaration, not a pattern guess, so `redact_mapping` cannot miss — and it runs
  before the *first* write, not the last, because the result is written to evidence
  after every step. Asserted end to end in
  `test_a_sensitive_input_is_never_written_anywhere`.
- **Pattern-based** masking of free text and screenshots is a **seam**: the patterns
  (SSN, PAN) load and the call sites are wired, but nothing is painted onto a frame.
  `Observation` carries text boxes precisely so it could be. The call sites existing
  is the part that matters — retrofitting a redaction point into code that already
  writes screenshots everywhere is the expensive version of this problem.
- Masking frames before the *model* sees them is in tension with the task itself: an
  agent asked to read a balance cannot do it if the balance is masked. A real
  deployment resolves that with a zero-retention/BAA agreement, not a mask, and
  should say so out loud.

**Limits, stated rather than papered over.**

- Risk classification is static per capability, authored at record time and
  reviewed by a human. There is no dynamic risk scoring.
- **There is no defence against prompt injection via page content** that the
  discovery model reads. The allowlist bounds the blast radius; it does not prevent
  the model being misled inside it. This is the guardrail model's real weak point,
  and the mitigations that matter — approval gating, `confirm` on risky steps, and
  the fact that production runs replay rather than discovery — reduce exposure
  rather than remove it.
- The control token is in-process. A durable store would be right for a real
  deployment and wrong here: a token that outlives the session it coordinates is a
  token that lies.
- **The control plane is unauthenticated and the operator identity is
  self-asserted** (`?operator=…`). Deliberate for a single-operator local
  deployment and wrong for anything else: `/capabilities/{id}/approve` and
  `/interventions/{id}/take` are the two states an auditor cares about, and both
  should sit behind the institution's SSO with the operator's identity coming from
  the session rather than from a query parameter. Named here because a reader who
  finds it should find it as a decision, not as an oversight.
- Evidence is written by the process being audited, to a local directory, unsigned.
  That makes it a debugging record that an audit trail could be built on — append-only
  storage and signing are the missing half — rather than one already.

---

## 7. Cuts

**Cut, with the seam left real.**

| Cut | Why, and what exists instead |
|---|---|
| Desktop surface | `action/desktop.py` implements the driver protocol and documents the X-input path. The perceiver needs no change at all, which is the claim worth testing and the reason it was not built. |
| DOM / accessibility perception | `ElementSource.DOM`/`AX` exist and nothing is behind them. The honest framing is that this is a *cost* decision deferred, not a capability that does not fit: a DOM or AX source is one class emitting the same `Element` list — a JS evaluation returning role, name and `getBoundingClientRect`, or `Accessibility.getFullAXTree` — plus a once-per-session offset to bring viewport coordinates into the display's space. It would cut an observation from ~900ms to ~50ms on the modern-web surfaces that are most of the fleet, with vision still carrying framesets, canvases and desktop. The seam is real and unused; building it would have made the easy surface easier while teaching nothing about the hard one, which is the wrong order for a project assessed on the hard one. |
| A production operator console | The console is real enough to run the system from — start a discovery or a replay, watch it step by step, inspect what the model saw and what policy decided, take over a parked session and hand it back. What it is not is multi-operator: no auth, no assignment, no queue that survives a restart. §3.6 permits a mock; the parts that had to be real — the control-transfer mechanism and the evidence behind a decision — are. |
| Pattern-based redaction | Seam, wired but inert (§6). |
| Gated single-step LLM recovery *inside* a replay | Named in the resolver ladder as `VLM_GATED` and unreachable by construction. `cua diagnose` is the same idea moved off the hot path — a model reading the evidence after the run has ended and proposing a declaration a human applies — which gets the long-tail benefit without the property it would cost. An in-run version belongs behind an explicit per-capability opt-in, never a global flag, and it is the one thing that would make "replay constructs no model" false. |
| A router that picks a capability for a goal | Not ours to build (§1). `/capabilities/manifest` is the boundary. |
| A full UI map | The right abstraction at *N* capabilities on one app, the wrong thing to build at two. `Screen` is the seam it grows from, derived from artifacts rather than maintained beside them so it cannot drift from what replay sees. |
| Queues, multi-tenant plumbing, clustering | Explicitly not rewarded by the brief, and the abstractions do not preclude them. |

**Stretch goals (§8), for the record.** Two landed because they fell out of the
core rather than being bolted on: the **agent-facing capability interface**
(`/capabilities/manifest` emits approved capabilities as function-calling tool
definitions with typed args, declared returns and declared outcomes;
`POST /capabilities/{id}/invoke` is the call) and **approval gating**
(`draft → approved`, with drafts excluded from the manifest). The *confidence*
half of that stretch goal did not — see next steps. Nothing else was attempted.

**Built after the second pass, because the fault harness made the gap obvious.**
The demo app was written with eight injectable faults and a table in the README
saying what each should do, and until `cua replay --fault` existed that table was an
intention rather than a test — faults live in a cookie, so arming one for the
automation means arming it *inside the automation's browser*, which no amount of
`curl` reaches. Driving the session through `/api/faults?set=…` before the run made
them runnable, and two of the eight then did not do what the table said. Both were
real:

- `slow` reported `target_mismatch` — the engine polled checkpoints and resolved
  targets exactly once, so a step recorded without a checkpoint imposed no wait and
  the previous step's latency landed on the next one as apparent drift.
- `modal` was diagnosed only after the click it had already eaten, because the
  pre-step interstitial check was a comment in the engine rather than a call. The
  same pass found `OnError.RETRY` and `StepBase.retries` in the schema with no
  reader anywhere — a retry contract an artifact could declare and the engine would
  silently ignore.

`/api/faults` and `/dev` are excluded from the app's allowlist as part of this: an
agent that can arm its own faults can disarm them.

**Built after the first pass, because the console made the gap obvious.** The
console originally showed the frames and the outcome and nothing between them,
which is enough to see *that* a run failed and not enough to see why. Closing that
meant recording the guardrail decision, the resolver's ladder walk and the model's
turn on every step (§1) rather than adding panels — the UI could only ever show
what the run had bothered to keep. Two things fell out of building it that were
bugs, not features: a discovery run reported `success` several seconds before
synthesis had written the artifact it was claiming, and a run accepted for
execution answered 404 until its first frame landed.

**Built in the last pass, and why each was worth it.** Every one closes a gap where
a document said something the code did not.

| | |
|---|---|
| **`cua diagnose`** | The long-tail mechanism (§4). One model call over a terminal run's evidence, a detector chosen by index rather than written, falsified against successful runs, downgraded to an escalation if the step mutates, emitted as YAML for a person to apply. Eight tests, no browser. |
| **App-level business outcomes** | The detector is the application's, the declaration is the capability's. What makes teaching scale from one capability to hundreds. |
| **Output constraints** | The gap a checkpoint cannot cover: a value that is type-valid, on the right screen, and wrong. `OUTPUT_REJECTED` is a distinct answer from `EXTRACTION_FAILED` because it sends an operator somewhere different. |
| **Ambiguity stops a write** | `find_and_act`'s discipline, applied to the ordinary click, where it was missing — and made usable by counting real candidates rather than `contains` substring matches. |
| **Approval enforced on `/invoke`** | The gate existed as a parameter no caller ever set. Absent from the manifest is not unreachable. |
| **Viewport checked; volatile text declared** | Two claims that were sentences in config files: geometry is part of the contract, and a screen with a clock on it can still settle. |
| **Boundary-aware parameterization** | `9912345` no longer becomes `99{{member_id}}`. |

**Not done, and it shows.**

- **A recorded *write* capability.** `cap_transfer_funds` exists as a hand-written
  fixture and its escalation path runs against the live app, but no discovery run
  has recorded a transfer. The transfer form is three `<select>` elements, which is
  a real perception question — a dropdown's options are not on screen until it is
  open — rather than a gap in the plumbing. It is the honest limit of what the
  discovery loop has been shown to do.
- **Screens are declared and enforced but never derived** (§4).

**What I would build next, in order.**

1. **`cua learn-screens <cap> --input <alternate values>`.** Replay a capability
   twice with different inputs, intersect the frames step by step, name each screen
   from the longest invariant line the other screens do not show, emit a new draft
   version — exactly as `learn-outcome` already does for business outcomes. This is
   the single change that turns §4's multi-tenant story from an argument into a
   mechanism, and it is also where per-tenant overrides get somewhere to attach.
2. **Record the write capability**, and with it the `<select>` interaction pattern.
3. **A second read on every declared output.** Re-observe and re-extract once, and
   disagreement between the two reads becomes `EXTRACTION_UNSTABLE`. It costs one
   observation on the final step and it closes the last residual in §3 — the
   misread that is type-valid *and* inside its bound — which constraints cannot
   reach. This is the cheapest remaining increment on correctness.
4. **Multi-run stability** (§8): replay N times, report a flakiness signal, and gate
   `draft → approved` on it. The approval gate exists; today a human is the only
   evidence behind it.
5. **`cua diff <cap> v1 v2`.** A semantic diff — steps added or removed, targets
   whose anchor changed, contract changes — over data the artifact already carries.
   Approval without a diff is a rubber stamp, and re-recording produces a new
   version every time.
6. **Reconciliation for the ambiguous write.** Post-verification answers "did the
   screen say it worked"; it cannot answer "did the money move" when the screen was
   unreadable, which is exactly the case that parks. A risky capability should be
   able to name a read-only capability plus an expected delta, and the engine should
   run it to return `committed | not_committed | indeterminate` — a third answer a
   person can act on rather than a screenshot. The companion is an idempotency
   token written into a memo or reference field the application already has, which
   turns "did my transfer go through" into a searchable question. Designed, not
   built: it is the piece I would want most in a real deployment and the one whose
   shape depends most on the application.
7. **The prompt experiment.** Strip the rules from the discovery system prompt,
   re-run the same goal, compare steps taken and discards. Five model calls, and it
   would replace an opinion in this document with a number.
