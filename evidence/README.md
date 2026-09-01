# Evidence

Every run writes a directory here, whatever its outcome. The discovery run, a replay of
what it recorded, and a replay that hits an exceptional state are produced by the
commands in the root [`README.md`](../README.md) against the live application.

## What is in each directory

```
run.json           the ReplayResult / DiscoveryResult contract, as the caller receives it
steps.jsonl        one line per step, appended before the step runs
frames/            step-NN.png — the display, plus the set-of-marks overlay
observations/      step-NN.json — every element perception found, with boxes
capability.json    discovery only: the artifact that was synthesized
synthesis.json     discovery only: what the model proposed, and what was rejected
diagnosis.json     if `cua diagnose` was run on it: the proposal, or why one was refused
intervention/      the request, handoff and handback frames, what the operator did
```

`frames/` is the display, not the browser viewport — the same image an operator sees
over VNC, in the same coordinate space the input layer clicks in.

## The runs in this directory

Generated from the runs themselves, not maintained by hand:

```bash
python3 backend/scripts/index_evidence.py            # regenerate this table
python3 backend/scripts/index_evidence.py --check    # fail if this file names a run that is gone
```

<!-- BEGIN INDEX -->
| Run | Capability | Result | Steps | What it produced | Handoff |
|---|---|---|---|---|---|
| `discover-balance-flow` | `cap_get_account_balance@v1` | **success** | 5 | Opened member 12345 profile and read Primary Savings balance of $18,204.55 |  |
| `escalate-29fdd5` | `fix_transfer_funds@v1` | **success** | 2 |  | yes |
| `offline-frames` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 18204.55 |  |
| `recover-expired-7f3870` | `cap_get_account_balance@v1` | **success** | 6 | `balance` = 18204.55 |  |
| `recover-modal-3f93e1` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 18204.55 |  |
| `recover-slow-8977f3` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 18204.55 |  |
| `replay-baseline` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 18204.55 |  |
| `replay-basic-checking` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 95.12 |  |
| `replay-everyday-checking` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 4820.19 |  |
| `replay-fault-banner` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 18204.55 |  |
| `replay-fault-denied` | `cap_get_account_balance@v1` | **business_outcome** | 4 | outcome `permission_denied` |  |
| `replay-fault-error500` | `cap_get_account_balance@v1` | **failure** | 4 | `app_error` at step 4 |  |
| `replay-free-checking` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 712.04 |  |
| `replay-joint-checking` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 3311.87 |  |
| `replay-member-not-found` | `cap_get_account_balance@v1` | **business_outcome** | 3 | outcome `member_not_found` |  |
| `replay-no-savings-account` | `cap_get_account_balance@v1` | **failure** | 5 | `resolution_exhausted` at step 5 |  |
| `replay-permission-denied` | `cap_get_account_balance@v1` | **business_outcome** | 4 | outcome `permission_denied` |  |
| `replay-rainy-day` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 2050.0 |  |
| `replay-unknown-account` | `cap_get_account_balance@v1` | **failure** | 5 | `resolution_exhausted` at step 5 |  |
| `replay-vacation-fund` | `cap_get_account_balance@v1` | **success** | 5 | `balance` = 15.0 |  |
| `scan-ambiguous-3da38b` | `fix_find_transaction@v1` | **success** | 2 | `amount` = -108.22 |  |
| `scan-escalating-7eb33a` | `fix_find_transaction@v1` | **escalated** | 2 |  | yes |
| `scan-missing-c2673f` | `fix_find_transaction@v1` | **business_outcome** | 2 | outcome `transaction_not_found` |  |
| `scan-unique-bbf336` | `fix_find_transaction@v1` | **success** | 2 | `amount` = -441.56 |  |
<!-- END INDEX -->

## What each kind of run shows

Run ids change whenever the set is regenerated; what matters is that one of each kind
is present.

| Kind | Command | What it shows |
|---|---|---|
| **discovery** | `cua discover --goal …` | The LLM-driven run against the live surface. `synthesis.json` holds what the model proposed for the capability's contract and which of it was falsified against the run's own frames. |
| **replay, success** | `cua replay <cap> --input …` | The same flow deterministically, with no model client constructed. `resolution` on each step names the tier that produced the coordinate. |
| **replay, business outcome** | `… --input member_id=99999` | `member_not_found`, exit code 0 — an answer, not a crash. |
| **replay, hard failure** | `… --fault error500` | `app_error`, with the step, the expectation, and what was on screen instead. |
| **replay, recovered** | `… --fault modal` / `--fault slow` | An interstitial cleared before the step acted; a slow screen waited for rather than reported as drift. |
| **replay, escalated** | `… --fault expired`, or a risky capability | The session handed to a human on the same browser, and handed back. `intervention/` holds the request, the handoff and handback frames, what the operator did, and how they resolved it. |
| **learn / diagnose** | `cua learn-outcome`, `cua diagnose <run>` | An unexplained hard failure turned into a typed result the caller can branch on. |

## Producing the whole set

`--run-id` names a run's directory, which is why the table above reads as a description of
the system rather than a list of hashes. Without it a run takes `replay-<random>`.

```bash
docker compose exec desktop bash

cua discover --goal "open member 12345 and read the current balance of their Primary Savings account" \
             --input member_id=12345 --input account_nickname="Primary Savings" \
             --capability-id cap_get_account_balance

# the same artifact against every member and account the application has
r() { cua replay cap_get_account_balance --run-id "$1" --input member_id="$2" --input account_nickname="$3"; }
r baseline          12345 "Primary Savings"      # 18204.55 — the recorded pair
r everyday-checking 12345 "Everyday Checking"
r free-checking     22841 "Free Checking"
r rainy-day         22841 "Rainy Day"
r basic-checking    30992 "Basic Checking"
r joint-checking    57310 "Joint Checking"
r vacation-fund     57310 "Vacation Fund"        # a name that wraps to two lines

# answers, not errors
r member-not-found  99999 "Primary Savings"
r permission-denied 44100 "Business Reserve"

# hard failures: the application states neither condition, so there is nothing to detect
r unknown-account    12345 "Nonexistent Account"
r no-savings-account 30992 "Primary Savings"

# injected faults
f() { cua replay cap_get_account_balance --run-id "fault-$1" --fault "$1" \
        --input member_id=12345 --input account_nickname="Primary Savings"; }
f banner; f denied; f error500

python3 scripts/smoke_recover.py     # modal, slow, expired — the three runtime-condition tiers
python3 scripts/smoke_scan.py        # unique, missing, ambiguous, escalating
python3 scripts/smoke_escalate.py    # control transfer on the live session

# no browser, no model, no target app — the decision path re-derived from recorded pixels
cua replay cap_get_account_balance --run-id frames \
    --frames /data/evidence/replay-baseline/frames \
    --input member_id=12345 --input account_nickname="Primary Savings"

python3 scripts/index_evidence.py    # regenerate the table above
```

Run `smoke_escalate.py` last, or after it the balances read $25 apart from the recorded ones —
it posts a real transfer.

## Reading a step result

Four fields on every step are drift signals:

- **`resolution`** — which tier of the resolver ladder produced the coordinate.
  `anchor_text` is the portable one; anchors decaying into `recorded_bbox` across runs
  says the application moved, before anything fails.
- **`attempts`** — how many times the step was executed. `1` on the ordinary path.
  Above it means the artifact declared `on_error: retry`, or a declared recovery
  cleared and the checkpoint still had not held. A risky step is never re-executed.
- **`settled_by`** — `pixels` on a static screen; `text` means something on the page is
  animating.
- **`note`** — anything the engine did that the artifact does not say: an interstitial
  cleared before acting, a wait for a screen to arrive, a URL rebased onto this
  deployment, an anchor that matched more than one element, a retry and why.

Three more records say why rather than what: `policy` (what the guardrail decided
before the step ran, allow as well as deny), `resolution_trace` (every rung of the
ladder and why each missed), and `model_turn` (discovery only: which mark the model
chose and which element that mark was).
