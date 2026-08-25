# Escalation

Handing a live session to a human, and taking it back. The session outlives the
transfer: the same display, the same browser process, the same cookies, and the same
half-filled form.

```
  control.py     who holds the session — one token, exactly one holder
  watch.py       what the operator did while they held it
```

---

## 1) Control (control.py)

- Controller (automation / human / nobody)
- RunControl (one run's control state)
    - park (surrender control and publish the request — does not wait)
    - escalate (park, then wait for a human)
    - take_control (an operator claims it: nobody → human)
    - release (they hand it back, and the run wakes)
    - assert_automation (the enforcement point)
- ControlRegistry (all live runs, addressed by run id or by intervention id)
- ControlError (something acted without holding the token)

Who holds control is explicit state, not the implied consequence of "nobody is
currently calling `click()`". Without that, an operator clicking during a run races
the automation on the same display — on a banking screen.

`assert_automation` is called by the **driver**, before every input event, rather than
by the runner. An escalation path that forgot to yield still cannot inject input while
a human holds the token.

`escalate` never times out. A run abandoned mid-intervention is a real state an
operator has to clean up, not something to paper over by resuming on a screen nobody
looked at.

## 2) Watching (watch.py)

- HumanActionWatcher (records operator input for the life of one intervention)
    - moves are sampled, not recorded wholesale — a mouse crossing the screen emits
      hundreds of events and none of them are the audit trail
    - keystrokes are **counted, never stored**: the operator may be entering a
      credential, and a log of what someone typed into a password field is a worse
      liability than one without it
    - `unavailable` when there is no X server here — the handoff still happens, and
      the gap is reported rather than hidden

Capture is at the X layer rather than through the browser. Playwright observes the
events it issues, and a manual click is not one of them — so instrumenting the browser
would leave a hole in the audit trail exactly where a human touched regulated data.
It also means the same code records a human operating a browser and a human operating
a desktop app.

---

## The control token

```
    AUTOMATION ──escalate──► NOBODY ──take_control──► HUMAN
         ▲                                              │
         └──────────── resume ◄──── NOBODY ◄────release ┘
```

`NOBODY` is the interval between the automation stopping and the operator connecting.
It is what makes "the agent clicked while I was typing" impossible rather than
unlikely. Control is surrendered *before* the request is published, so there is no
window in which an operator can see an intervention while the automation may still act.

## What survives the handoff

```
   the run          parked on an event, not unwound
   the browser      same process, same cookies, same half-filled form
   the display      the operator connects over VNC to the identical pixels
                    the resolver was looking at
```

On resume the runner re-observes rather than trusting the frame it parked on — the
human may have advanced the application several screens. It does not search forward
for the first step that already looks satisfied, because it does not need to: the next
step asserts its own screen and verifies its own checkpoint, so an operator who left
the app somewhere unexpected produces a loud `WRONG_SCREEN` rather than a blind click.

The registry is in-process on purpose. What it coordinates is a browser on this
machine's display, so a control token that outlives the process holding the session is
a token that lies.
