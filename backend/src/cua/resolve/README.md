# Resolve

Turns a recorded `Target` into a coordinate on the current screen, and asserts —
before and after the action — that it was the right one.

```
  normalize.py    how two pieces of text are compared
  template.py     {{param}} in, and back out again
       │
       ▼
  resolver.py     Target → coordinate, via the ladder
       │
       ▼
  verify.py       the assertions that wrap every action
```

---

## 1) Normalizers — how two pieces of text are compared (normalize.py)

- casefold / collapse_ws (OCR line boxes carry column padding as spaces)
- strip_currency (`$1,234.56` → `1234.56`, including `($441.56)` as a negative — but
  only when the parentheses actually wrap a number, or "(see reverse)" becomes
  "-see reverse")
- strip_punct (blunt, for labels: "Member ID:" vs "Member ID" — it deletes the
  decimal point, so it must come after strip_currency)
- strip_ellipsis (truncated cell text is the most common reason a correct predicate
  fails to match)
- digits_only / date_iso (US back-office apps mix three date formats on one screen;
  unparseable input is returned unchanged, because a normalizer must not invent data)
- apply (run them in the order the artifact declared)

The list lives in the artifact, not in this module's defaults. That is the difference
between "replay compares strings the way the recording did" and "the way whatever
version of the engine is deployed today does". Every function is total and idempotent
— one that could throw would turn a text comparison into a crash.

## 2) Templates — the one place a caller's inputs enter a recorded string (template.py)

- render (`{{member_id}}` → `12345`; an unknown placeholder raises, because rendering
  it to nothing turns "find the row containing 12345" into "find the row containing")
- unrender (the inverse, and how a recording becomes reusable: `12345` → `{{member_id}}`)
- placeholders (what a template needs — checked before a run touches the application)

`unrender` matches longest value first and only at token boundaries. Otherwise an
input of `123` eats the front of a recorded `12345`, and the account number `9912345`
becomes `99{{member_id}}` — an artifact that navigates somewhere that exists for nobody.

## 3) The ladder — Target → coordinate (resolver.py)

- Resolver (stateless: an observation and a target in, a coordinate out)
- Resolution (where it landed, which rung won, how many candidates there were, and
  whether the choice was ambiguous)
- ResolutionTrace (every rung and what it did — not just the winner)
- Unresolvable (no rung produced a coordinate; always terminal for the step)
- point_in (the offset within a resolved box — how a step targets the "View" button
  at the right edge of a matched row without a second resolution pass)

Ambiguity is counted *after* the anchor's own disambiguators run. `contains` is the
right default — a balance sits inside "Available Balance: $18,204.55" — but it makes
the raw match count useless: "Search" matches the button and the heading "Member
Search" every time. An element whose whole label is the anchor beats one that merely
contains it.

## 4) Verification — the checks that wrap every action (verify.py)

- verify_target (before acting — two distinct failures, because they call for
  different responses)
    - TARGET_MISMATCH (the region resolved, but does not read as the recorded label)
    - UNEXPECTED_OVERLAY (something is stacked on top of it — never click through one)
- verify_effect (after acting — the step's declared checkpoint)
- evaluate (one checkpoint against one observation, no waiting; shared with business
  outcome and recoverable detectors, which are the same shape)
- region_text (everything readable inside a region — also what extraction uses, so a
  read and a checkpoint cannot have two definitions of "what it says there")

A stable coordinate is not a right one. A modal moves nothing: it lands on top, the
recorded coordinate still resolves, and the click hits the dialog. No amount of better
targeting detects that — only an assertion does.

---

## The ladder

```
    anchor_text hit ────────────────► matched bbox + recorded offset
        │ miss
    role + name match ──────────────► element bbox
        │ miss
    recorded bbox ──────────────────► recorded bbox   (+ drift event)
```

Most portable first, and the winning rung is recorded on every step result. Aggregated
across runs that is a free drift signal: anchors decaying into `recorded_bbox` means
the application moved, long before anything actually fails.

## Data-dependent targets do not fall through

```
   anchor "Transfer"        misses  ──► try role+name, then the recorded box
   anchor "{{member_id}}"   misses  ──► stop. Not on this screen.
```

A `{{param}}` in the anchor makes the target a question about *data*; the lower rungs
answer a question about *position*. They are the right answer when an application
moved and the wrong one when a record is absent — the recorded box is where the
recording's row sat, so under anything that shifts the page it reads a neighbouring
record with full confidence. Not found here is information, not a reason to guess.

## Following a relation

```
   anchor: "User ID"  ──right_of──►  the empty box beside it   ← what we type into
   anchor: "29883"    ──column──►    the cell under "Balance"  ← what we read
```

The control a step acts on is usually not the thing with the words on it, and under
vision there is no `for=` attribute to follow. When the neighbour is not there, this
returns nothing rather than falling back to the anchor — typing into a label because
the field beside it could not be found is exactly the silent wrong action the system
exists to prevent.

A `column` is preferred over a neighbour index when both are recorded. An index counts
the cells that happened to be filled in on the row it was recorded from; a blank status
or an extra column shifts it silently onto the wrong cell. A header is what a person
reads, and it does not move when the row's contents do.
