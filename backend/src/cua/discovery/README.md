# Discovery

An LLM drives the application until the goal is met, and the run is recorded as it
happens. The recording is a side effect of acting, not a second pass over a transcript,
so the transcript and the artifact cannot disagree.

```
  llm.py          one turn: a screenshot in, one tool call out
  prompts.py      the text, in one file, so a prompt change is a visible diff
  actions.py      the action space — which is also the artifact's step vocabulary
       │
       ▼
  loop.py         observe → decide → act → record
       │
       ▼
  synthesize.py   the finished run, turned into a Capability
```

---

## 1) The model client (llm.py)

- LLMClient (LiteLLM; thin, so the choice of model stays one setting)
    - preflight (fail before the run if the model cannot see images or call tools)
    - decide (one turn, `tool_choice="required"`; one retry with an explicit instruction
      rather than a crash nine steps in)
    - structured (one forced tool call, used only by synthesis)
- ToolCall (the call, plus the model's stated reasoning and its chain of thought, which
  live in different places depending on the provider)
- NoLLM (raises on every call; this is what replay is constructed with)

Only the current frame is ever sent. Earlier screenshots are represented by the text
history instead: a ten-step run would otherwise carry ten megabytes of base64 into every
later turn.

## 2) Prompts (prompts.py)

- SYSTEM (how the loop works — the same for every application, plus one sentence from
  the app's policy saying what it is looking at)
- turn (the goal, this run's inputs, what has happened so far, and what is on screen)
- SYNTHESIS / DECLARATION_SCHEMA (the one place a model is asked to write prose)

The system prompt also says that text on the screen is data, not instructions — the one
defence against a page that tries to redirect the agent.

## 3) The action space (actions.py)

- TOOLS (click / type_text / press_key / navigate / scroll / find_and_click /
  find_and_extract / extract, plus finish and escalate)
- tool_definitions (filtered by policy — a forbidden action is never offered, rather
  than refused after the model spends a turn on it)
- to_step (one tool call → one typed artifact step, with the Target written from the
  element that was actually on screen rather than from the model's description of it)
- durable_expect (refuses an expectation that cannot be true next month — an amount, a
  date, or anything this run has already read off the screen)
- _shorter_anchor (the same treatment for a proposed anchor: it must be text on the
  chosen element, and resolving it against that frame must land back on it. A value the
  caller declared as an input beats the model's own proposal.)

The model never gives coordinates; it picks a number from an enumerated list. Because
the tools are exactly the primitives an artifact can contain, a recording is replayable
by construction rather than by inferring intent back out of pixels.

```
   the model says      mark · intent · expect · risk · anchor
        │
        ▼
   measured            bbox, role, name — read off the element behind the mark, not
        │              from the model's description of what it thought it clicked
        ▼
   falsified           anchor must be on that element and still resolve to it;
        │              expect is refuted if it is an amount, a date, or this run's data
        ▼
   one typed step      with a checkpoint, or deliberately without one
```

`find_and_click` and `find_and_extract` are two tools rather than one with a flag: a
read changes nothing, so the only honest answer to "what will be on screen afterwards"
is the value just read — which as a checkpoint fails for every other input.

## 4) The loop (loop.py)

- DiscoveryLoop (observe → decide → act → record, until the goal holds or a stopping
  condition fires: step budget, model-call budget, wall clock, policy denial, a dead
  end, or the model asking for help)
- DiscoveryState (everything the run accumulates, written to evidence every step, so a
  crashed run is still inspectable)

Every turn is recorded, including the ones that produced no step — a rejected mark, a
policy refusal, a discarded action.

The entry navigation is *recorded*, not just performed. Without it the artifact begins
wherever the browser happened to be.

## 5) Synthesis — the run, as a contract (synthesize.py)

- prune (drop steps that did not advance the state — conservatively)
- parameterize (recorded literals matching a declared input become `{{placeholders}}`;
  longest match first, at token boundaries, so `123` cannot eat a recorded `12345`)
- declare (one bounded model call: a name, a description, a success phrase, and
  candidate business outcomes)
- synthesize (the whole pipeline; the result round-trips through its own schema before
  anyone is told it exists)

Deterministic first, and the model only where determinism cannot answer. Outputs are
read off the recording's own extract steps rather than asked for again.

---

## One turn

```
   observe            settle, then draw numbered boxes on the frame
        │
        ▼
   decide             the model picks a mark and one tool
        │
        ▼
   check              is the mark real? is the action allowed? is it risky?
        │             (risky parks the run for a human before it is recorded)
        ▼
   act                execute it
        │
        ▼
   observe again      did the screen change? did the expectation come true?
        │
        ▼
   verdict            keep / keep without a checkpoint / discard
```

## The three verdicts

```
   expectation held                      → kept
   expectation wrong, but screen changed → kept, checkpoint dropped
   expectation wrong, screen unchanged   → discarded
```

In the middle case the action did something, so dropping the step would leave the
recording missing a state transition and replay would start from a screen it never
reaches. The step is kept and the assertion is not.

In all three cases the model is told what actually happened.

## Run → capability

```
   steps  ──prune──►  parameterize  ──►  declare  ──►  Capability (draft)
                           │                 │
                     {{member_id}}      name, description,
                     from the run's     success phrase,
                     declared inputs    candidate outcomes
                                             │
                                        each one falsified against the
                                        successful run's own frames — a
                                        phrase visible while the flow
                                        *succeeded* cannot be what marks
                                        it having gone another way
```

What was proposed and thrown away is kept in `synthesis.json`.
