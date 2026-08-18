# Evidence

Every run writes a directory here, whatever its outcome. What `ASSIGNMENT.md` §6.3
asks for — the discovery run, a replay of what it recorded, and a replay that hits
an exceptional state — is produced by the commands in the root
[`README.md`](../README.md) against the live application.

## What is in each directory

```
run.json           the ReplayResult / DiscoveryResult contract, as the caller receives it
steps.jsonl        one line per step, appended before the step that might not return
frames/            step-NN.png — the display as it was, plus the set-of-marks overlay
observations/      step-NN.json — every element perception found, with boxes
capability.json    discovery only: the artifact that was synthesized
synthesis.json     discovery only: what the model proposed, and what was rejected
diagnosis.json     if `cua diagnose` was run on it: the proposal, or why one was refused
intervention/      the request, handoff and handback frames, what the operator did
```

`frames/` is the display, not the browser viewport — the same image an operator
sees over VNC, in the same coordinate space the input layer clicks in.

## The runs in this directory

Generated from the runs themselves, not maintained by hand:

```bash
python3 backend/scripts/index_evidence.py            # regenerate this table
python3 backend/scripts/index_evidence.py --check    # fail if this file names a run that is gone
```

<!-- BEGIN INDEX -->
_No runs in this directory yet._
<!-- END INDEX -->

An earlier index listed runs by hand and went stale the first time they were
regenerated, which is the reason for both the generator and the `--check`: an
index that names a directory nobody can open is worse than no index, because a
reviewer who follows it and finds nothing stops believing the rest.

## What each kind of run is here to show

The demonstration is a set of *results*, not a set of ids — the ids change every
time the runs are regenerated, and what matters is that one of each is present.

| Kind | Command | What it proves |
|---|---|---|
| **discovery** | `cua discover --goal …` | The LLM-driven run against the live surface. `synthesis.json` holds what the model proposed for the capability's contract and which of it was falsified against the run's own frames. This is §4's one non-negotiable. |
| **replay, success** | `cua replay <cap> --input …` | The same flow deterministically, no model client constructed. Check `resolution` on each step: `anchor_text` is the portable tier. |
| **replay, business outcome** | `… --input member_id=99999` | `member_not_found`, exit code 0. An answer, not a crash — the distinction the brief singles out. |
| **replay, hard failure** | `… --fault error500` | `app_error`, with the step, the expectation and what was on screen instead. |
| **replay, recovered** | `… --fault modal` / `--fault slow` | An interstitial cleared *before* the step acted, and a slow screen waited for rather than reported as drift. Two different ways of not failing. |
| **replay, escalated** | `… --fault expired`, or a risky capability | The session handed to a human on the same browser, and handed back. `intervention/` holds the request, the handoff and handback frames, what the operator did — with typed text recorded as a keystroke count, never as content — and how they resolved it. |
| **learn / diagnose** | `cua learn-outcome`, `cua diagnose <run>` | One screen going from an unexplained hard failure to a typed result the caller can branch on, without anyone hand-writing a detector. |

## Producing the whole set

```bash
docker compose exec desktop bash
cua discover --goal "open member 12345 and read the current balance of their Primary Savings account" \
             --input member_id=12345 --input account_nickname="Primary Savings" \
             --capability-id cap_get_savings_balance
cua replay cap_get_savings_balance --input member_id=12345 --input account_nickname="Primary Savings"
cua replay cap_get_savings_balance --input member_id=99999 --input account_nickname="Primary Savings"
python3 scripts/smoke_recover.py     # modal, slow, expired — the three runtime-condition tiers
python3 scripts/smoke_escalate.py    # control transfer on the live session
python3 scripts/index_evidence.py    # then paste the table above
```

## Reading a step result

Four fields on every step are free drift signals rather than decoration:

- **`resolution`** — which tier of the resolver ladder produced the coordinate.
  `anchor_text` is the portable one. Aggregated across runs, anchors decaying into
  `recorded_bbox` says the application moved, long before anything fails.
- **`attempts`** — how many times the step was executed. `1` on the ordinary path.
  Above it means either the artifact declared `on_error: retry` or a declared
  recovery cleared and the checkpoint still had not held — and in both cases the
  step was safe, because a risky step is never re-executed.
- **`settled_by`** — `pixels` on a static enterprise screen; `text` means something
  on the page is animating, which is the same early-warning signal in a different
  currency.
- **`note`** — anything the engine did that the artifact does not say: an
  interstitial cleared before acting, a wait for a screen to arrive, a URL rebased
  onto this deployment, an anchor that matched more than one element, a retry and
  why.

And three records say *why* rather than *what*: `policy` (what the guardrail
decided before the step ran, allow as well as deny), `resolution_trace` (every rung
of the ladder and why each missed), and `model_turn` (discovery only: which mark
the model chose and which element that mark measurably was).
