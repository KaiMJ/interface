# Policy

The guardrails, as configuration. One YAML file per application — nothing in
`backend/src` knows which applications exist.

```
  policies/<app>.yaml   what a human authors
       │
       ▼
  policy.py       the guardrail object, consulted on both paths
  redact.py       keeping sensitive values out of what we write down
```

---

## 1) What a policy declares (policy.py)

- identity (app / vendor / base_url_pattern — a *pattern*, so one artifact is valid at
  every institution running the same vendor product)
- entry_url (this deployment's install — the one per-institution fact, so a second
  tenant overrides this and nothing else)
- allowed_url_patterns (the allowlist — an unlisted URL is refused)
- allowed_actions (an unlisted primitive is denied, so a missing list denies everything)
- risky_disposition (allow / confirm / block — what to do with a step declared risky)
- risky_intent_patterns (phrases that force `risky` whatever the recording claimed)
- recoveries / app_errors / escalations (declared runtime conditions — see below)
- business_outcomes (AppOutcome: the *detector* for a legitimate alternative answer)
- volatile_text (lines that tick on their own, excluded from the settle comparison)
- sign_on (the recipe for reaching an authenticated state)
- max_restarts / max_escalations_per_step (the two budgets bounding the ways a run can
  decline to finish — here rather than in the engine, because both are judgements about
  an application)

## 2) The checks (policy.py)

- decide (the verdict, as a record — including the boring allows)
- check_action (the same verdict, raising)
- check_url (evaluated on navigate *and* after every action, because a click can
  navigate)
- promoting_pattern (promotion is one-directional — safe can become risky, never the
  reverse; it returns the pattern that decided, not a boolean, so a promotion is
  reported with its cause)

The same object is consulted by discovery and by replay. A guardrail that only guards
the LLM is not a guardrail: a buggy or tampered artifact submits the wrong transfer just
as well as a confused model does.

`decide` records the allow as well as the denial, so evidence for "this transfer was
permitted here" is an entry rather than the absence of one.

## 3) Declared conditions — split by who can clear it

- Recovery (we can clear it: a detector, ordered actions, and a cap)
- Condition → app_errors (nobody can fix it from here — stop with APP_ERROR)
- Condition → escalations (a human can fix it — park and hand over the live session)

`max_per_run` bounds a recovery: dismissing the same modal eleven times means the
dismissal is not working.

Without these, every one of these states arrives as "the checkpoint did not hold" — the
symptom rather than the cause.

## 4) Sign-on — a precondition of every capability and a capability of none

- SignOn / SignOnStep (described the way a capability describes anything: an action, a
  target named by what is on screen, a value)

Kept here so that no artifact ever references a credential. The mechanism is the same as
a recorded step; only the storage differs, and `{{password}}` is resolved in the action
layer, below the point where anything is serialized.

## 5) Redaction (redact.py)

- redact_mapping (declared-sensitive inputs — strictly stronger than a pattern, because
  `InputSpec.sensitive` is a declaration rather than a guess)
- redact_text (pattern masking for a log line or a result field — implemented, and not
  switched on in v1)
- redact_image (masking regions of a frame — a seam; v1 returns the frame untouched)

A screenshot is simultaneously the evidence *and* the model input, and a bank screen is
PII by construction. What matters in v1 is that the call sites exist.

---

## One action, through the guardrail

```
   is the primitive on the allowlist?  ──no──► denied
        │ yes
        ▼
   declared risk, raised if the intent reads as a mutation
        │
        ├─ safe                            ──► allow
        └─ risky ──► risky_disposition ──► allow | confirm | block
                                             │        │        │
                                          proceed   park    denied
                                                  for a
                                                  human
```

The intent is what gets classified, never the step's value: a navigate to
`/transfer/review` is not a transfer, and matching risk patterns against a URL would stop
before every page whose path contains a verb.

## Who owns what

```
   the application declares            the capability declares
   ────────────────────────            ───────────────────────
   what the screen says                which answers this flow can return
   "No member matches the              business_outcomes:
    search criteria entered."            - name: member_not_found
```

Wording is a fact about the app — every flow that searches for a member meets the same
screen, so the detector is taught once and inherited by name. Whether *this* flow can
return that answer is a fact about the capability, because that list is the contract its
caller branches on.
