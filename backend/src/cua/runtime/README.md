# Runtime

The composition root, and the object a run borrows. This is the only place that knows
how the pieces fit together — everything else takes its collaborators as constructor
arguments, which is what makes the seams testable rather than described.

```
  session.py     the display + browser + perceiver, which a run borrows
  wiring.py      what gets handed to a discovery run, and to a replay
```

---

## 1) The session (session.py)

- Session (owns the display, the browser process, and the perceiver/driver pair)
    - authenticate (sign in once, before any capability runs — idempotent, so it is
      safe to call before every run rather than tracking whether the cookie is good)
    - observe (session-level perception, into scratch rather than evidence: signing
      in is not part of any capability's audit trail)
    - vnc_url (where an operator connects to take over this exact session)
- AuthenticationFailed (terminal for the session, not for a run)
- SessionPool (one session per display — single-display in v1, so: one session)
- WrongApp (asked for one application on a display already holding another)

A run *borrows* the session; it does not own it. That split is what lets an escalation
hand a live session to a human without either tearing down the browser they are
supposed to take over, or keeping a finished run alive just to hold a process open.

Sign-on is deliberately not a recorded capability — it is the one flow where a secret
is typed, and keeping it out of the artifact system means no artifact ever needs to
reference one. Its failure message is deliberately vague: the screen may say
"Sign-on failed", and repeating that into an exception that ends up in a log is how a
bad credential becomes a logged credential.

Two applications cannot share a display. The X display *is* the coordinate space, so a
second browser on it would put two apps in one picture and every resolved coordinate
would be ambiguous.

## 2) Wiring (wiring.py)

- build_perceiver (the one perceiver, shared by both paths)
- build_policy (the guardrails for one application, cached by path)
- build_session / build_session_pool
- build_discovery (a real model client)
- build_replay (no model client at all)
- build_offline_replay (the same engine against recorded frames — no browser, no
  display, no model)
- entry_url (this deployment's override, then what the app's policy declares)
- REGISTRY (one control registry per process)

Discovery and replay get the *same* perceiver on purpose. If replay saw the screen
through a different pipeline than the recording did, an artifact would be a description
of what some other system saw, and every checkpoint written at discovery time would be
a guess about replay.

---

## The two runners

```
                      Settings + Session
                    (display, browser, perceiver)
                              │
              ┌───────────────┴───────────────┐
              ▼                               ▼
       build_discovery                  build_replay
       ─────────────────                ─────────────
       Perceiver     (same)             Perceiver     (same)
       Driver        (same)             Driver        (same)
       Policy        (same)             Policy        (same)
       EvidenceWriter(same)             EvidenceWriter(same)
       LLMClient     ◄── the only       NoLLM         ◄── raises on
                         difference                       every call
```

Replay is not *asked* to avoid the model; it is handed a collaborator that raises if
it tries. A test asserts determinism by construction rather than by reading the code
for the absence of a call.

That the two differ in exactly one collaborator is also the guardrail argument: the
policy object is the same object, so an allowlist that bound only the unattended path
would have a hole shaped exactly like a recording session.

## Swapping the surface

```
   live      XDisplayScreen + BrowserDriver     a real browser on a real display
   offline   ImageFileScreen + OfflineDriver    a previous run's PNGs
```

`build_offline_replay` is the same engine, the same resolver, the same policy and the
same evidence writer — two collaborators swapped. That the substitution is this small
is the argument that the perception and action seams are real rather than described:
nothing above them knows the difference.
