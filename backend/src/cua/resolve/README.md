# Resolve

Turns a recorded `Target` into a coordinate on the current screen, and asserts — before
and after the action — that it was the right one.

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

## 1) Normalizers (normalize.py)

- casefold / collapse_ws (OCR line boxes carry column padding as spaces)
- strip_currency (`$1,234.56` → `1234.56`, including `($441.56)` as a negative — but only
  when the parentheses actually wrap a number, or "(see reverse)" becomes "-see reverse")
- strip_punct (blunt, for labels: "Member ID:" vs "Member ID" — it deletes the decimal
  point, so it must come after strip_currency)
- strip_ellipsis (truncated cell text is the most common reason a correct predicate
  fails to match)
- digits_only / date_iso (US back-office apps mix three date formats on one screen;
  unparseable input is returned unchanged, because a normalizer must not invent data)
- apply (run them in the order the artifact declared)

## 2) Templates — the one place a caller's inputs enter a recorded string (template.py)

- render (`{{member_id}}` → `12345`)
- unrender (the inverse, and how a recording becomes reusable: `12345` →
  `{{member_id}}`, not `123` → `{{member_id}}45`)
- placeholders (what a template needs — checked before a run touches the application)

## 3) The ladder — Target → coordinate (resolver.py)

- Resolver (stateless: an observation and a target in, a coordinate out)
- Resolution (where it landed, which rung won, how many candidates there were, and
  whether the choice was ambiguous)
- ResolutionTrace (every rung and what it did — not just the winner)
- Unresolvable (no rung produced a coordinate; always terminal for the step)
- point_in (the offset within a resolved box — how a step targets the "View" button at
  the right edge of a matched row without a second resolution pass)

## 4) Verification — the checks that wrap every action (verify.py)

- verify_target (before acting — two distinct failures, because they call for different
  responses)
    - TARGET_MISMATCH (the region resolved, but does not read as the recorded label)
    - UNEXPECTED_OVERLAY (something is stacked on top of it — never click through one)
- verify_effect (after acting — the step's declared checkpoint)
- evaluate (one checkpoint against one observation, no waiting; shared with business
  outcome and recoverable detectors, which are the same shape)
- region_text (everything readable inside a region — also what extraction uses, so a
  read and a checkpoint cannot have two definitions of "what it says there")

---

## The ladder

```
    anchor_text hit ────────────────► matched bbox + recorded offset
        │ miss
    role + name match ──────────────► element bbox
        │ miss
    recorded bbox ──────────────────► recorded bbox   (+ drift event)
```

## Data-dependent targets do not fall through

```
   anchor "Transfer"        misses  ──► try role+name, then the recorded box
   anchor "{{member_id}}"   misses  ──► stop. Not on this screen.
```

## Following a relation

```
   anchor: "User ID"  ──right_of──►  the empty box beside it   ← what we type into
   anchor: "29883"    ──column──►    the cell under "Balance"  ← what we read
```
