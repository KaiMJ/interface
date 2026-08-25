# Replay

Executes a recorded capability with no model in the decision loop. This is the
production path — what an AI agent invokes.

```
  contract.py     inputs in, typed outputs out    (never looks at a screen)
       │
       ▼
  engine.py       one step at a time
       │
       ├──► outcomes.py    what is this frame?
       ├──► scan.py        find the row, then act on it
       └──► tenant.py      which institution's install a recorded URL means
```

---

## 1) The contract — answerable without touching the app (contract.py)

- validate_inputs (coerce and check against the declared InputSpecs, before a
  browser opens — a type error should be a rejected call, not a run that gets four
  steps in and types "None" into an amount field)
- extract_outputs (read the declared outputs back out: coerce → normalize → bounds)
- ContractError (a structured rejection the engine turns into a terminal result)

A blank string is treated as missing, not as a value. `""` passes a plain `in` check,
renders `{{account_nickname}}` to nothing, and the ladder then returns the first row's
balance as a success.

## 2) The engine — one step at a time (engine.py)

- ReplayEngine (the lifecycle below)
- RunContext (what one run accumulates, including what the *current* step decided —
  held here rather than returned, because a step that ends the run unwinds through
  an exception and that is the step whose evidence matters most)
- the four ways a run ends early
    - _Business (a legitimate answer — the caller branches on it)
    - _Escalated (parked, handed to a human on the same live session)
    - _Failed (something we do not understand)
    - _Restart (session expired, nothing irreversible had happened yet, so the
      capability starts over rather than resuming somewhere it cannot verify)

## 3) Classification — what is this frame? (outcomes.py)

- classify (one function, so the order below cannot be re-litigated per call site)
- effective_outcomes (fill in the detectors a capability inherits from app policy,
  at run start — an unresolved detector does not error, it simply never matches)
- conditions (declared app states; evaluable without a step, so the engine can also
  ask before one acts)

## 4) Scanning — when position is a function of the data (scan.py)

- Scanner (observe scope → group rows → test predicate → advance, bounded)
- ScanResult (the matches, and whether the list was *exhausted* or we merely ran out
  of budget — see below)
- Untestable (the predicate cannot be answered, which is not the same as "no match":
  equality against a cell the app truncated is unanswerable)

## 5) Tenancy (tenant.py)

- rebase (a recorded URL, pointed at this deployment's install — without it a
  capability recorded at one credit union and replayed from another navigates to the
  first, passes the allowlist, and reports success about the wrong member)

---

## One step

```
   clear the way      a declared interstitial already up? clear it first — one that
        │             is up *before* the click is issued absorbs it
        ▼
   verify permission  policy checks the declared intent: this action, this risk
        │             (`confirm` parks the run for a human here)
        ▼
   wait until ready   poll until this step's screen is in front of us
        │             ("the row is not there" and "not there yet" look identical)
        ▼
   resolve            Target → coordinate, via the ladder in resolve/
        │
        ▼
   verify target      does the region say what the recording said it said?
        │             is something stacked on top of it?
        ▼
   execute            one primitive
        │
        ▼
   verify effect      poll the checkpoint to its own deadline, then classify
```

Waiting happens at both ends of a step, and there is no unconditional `sleep()` on
this path — waiting is polling to a declared deadline. The only two `sleep` calls are
the poll interval itself and a `wait` action a policy declared with a number.

## What is this frame — the order is the point

```
   1. a declared business outcome?     → run stops cleanly, caller branches on it
   2. a condition policy can clear?    → recover, re-observe, maybe re-execute
   3. an app error, or a human's job?  → stop, or hand over the live session
   4. does the step's checkpoint hold? → ok
   5. anything else                    → failure
```

Business outcomes are asked first. Evaluated after the checkpoint, "no member matches"
arrives as a failed assertion — a layout problem an operator goes looking for and
never finds — instead of the answer the caller asked for.

## When a step runs twice

```
   a recovery fired and the checkpoint     ┐
   still did not hold                      │
   (the signature of a modal that ate      ├──► gated on policy's effective risk:
    the click)                             │    a risky step is never re-executed,
                                           │    at any budget
   on_error: retry, within its budget      ┘
```

The gate reads policy's verdict rather than the artifact's own `risk` field, so a step
recorded `safe` and promoted by an intent pattern is excluded too. And the checkpoint
is polled to its full deadline before any re-execution, so a step whose action landed
and was merely obscured is never run a second time.

## Exhausted is not out-of-budget

A scan that saw the whole list and found nothing is a business outcome. A scan that
hit `max_advances` while the region was still changing is a hard failure — "not found"
there would be a confidently wrong answer, which is the distinction the whole error
taxonomy exists to keep.
