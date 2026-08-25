"use client";

/**
 * One step, taken apart — one stage at a time, in the order the step went through
 * them: decide, permit, resolve, verify. A tab with nothing to say is disabled
 * rather than hidden, because "this step consulted no resolver" is itself
 * information, and a strip that changes shape per step is one you re-read every
 * time.
 *
 * Everything here is a record the system already made and used to discard.
 */

import { useState } from "react";
import { Tabs } from "./Shell";
import { Chip, Empty, Field, Json, ms, statusColor } from "./ui";
import type { StepRow } from "@/lib/api";

const TABS = ["decision", "guardrail", "resolution", "verify", "cost"] as const;
type Tab = (typeof TABS)[number];

export function Inspector({ step }: { step: StepRow | null }) {
  const [tab, setTab] = useState<Tab>("decision");
  // Collapsible because vertical space is the real constraint on a laptop: the
  // frame and the inspector are both trying to be the main thing, and which one
  // is depends on whether you are watching a run or debugging one.
  const [open, setOpen] = useState(true);

  if (!step) {
    return (
      <div className="border-t border-[var(--rule)]">
        <Empty>Select a step to see what the agent perceived, decided and verified.</Empty>
      </div>
    );
  }

  const turn = step.model_turn;
  const policy = step.policy;
  const trace = step.resolution_trace;
  // A replay step has no model turn; landing on an empty tab every time you click
  // through a replay is the kind of small friction that makes a tool tiring.
  const active: Tab = tab === "decision" && !turn ? "guardrail" : tab;

  return (
    <div
      className={`flex shrink-0 flex-col border-t border-[var(--rule)] ${
        open ? "max-h-[38vh] min-h-[150px]" : ""
      }`}
    >
      <div className="flex items-center">
        <div className="min-w-0 flex-1 overflow-x-auto">
          <Tabs
            tabs={TABS}
            active={active}
            onSelect={(t) => {
              setTab(t);
              setOpen(true);
            }}
          />
        </div>
        <button
          className="mono shrink-0 border-b border-[var(--rule)] px-3 py-1.5 text-[11px] text-[var(--muted)]"
          onClick={() => setOpen((o) => !o)}
          title={open ? "collapse — give the frame the room" : "expand the inspector"}
        >
          {open ? "▾" : "▸"}
        </button>
      </div>
      <div
        className="min-h-0 flex-1 space-y-1 overflow-y-auto p-3 text-[12px]"
        hidden={!open}
      >
        {active === "decision" ? (
          turn ? (
            <>
              {/* The turn in the order it happened — shown, thought, answered —
                  above the fields that summarise it. These were at the bottom of a
                  scrolling panel, which on a laptop meant nobody ever saw them. */}
              {turn.prompt ? (
                <Detail summary="what it was shown" chars={turn.prompt.length}>
                  {turn.prompt}
                </Detail>
              ) : null}
              {turn.reasoning ? (
                <Detail summary="how it got there" chars={turn.reasoning.length} open>
                  {turn.reasoning}
                </Detail>
              ) : null}

              <Field label="tool call" value={turn.call} mono />
              <Field label="intent" value={turn.intent || "—"} />
              {/* Proposed and recorded, the same pair the anchor gets below. A
                  refuted expectation is still shown — a step whose checkpoint was
                  dropped reads as verified otherwise. */}
              <Field
                label="expected"
                title="the model proposes an expectation; code refutes it before it becomes a checkpoint"
                value={
                  !turn.expect ? (
                    "—"
                  ) : turn.expect_recorded ? (
                    turn.expect_recorded
                  ) : (
                    <>
                      <span className="line-through">{turn.expect}</span>
                      <span className="text-[var(--muted)]">
                        {" "}
                        (this record&rsquo;s data — no checkpoint recorded)
                      </span>
                    </>
                  )
                }
              />
              <Field
                label="chose mark"
                title="the element behind the mark, measured — not the model's description of what it thought it was clicking"
                value={
                  turn.mark === null || turn.mark === undefined ? (
                    "—"
                  ) : (
                    <>
                      <span className="mono">#{turn.mark}</span>
                      {turn.element_id ? (
                        <span className="text-[var(--muted)]">
                          {" "}
                          → {turn.element_id} “{turn.element_label}”
                        </span>
                      ) : null}
                    </>
                  )
                }
              />
              {turn.anchor_proposed || turn.anchor_recorded ? (
                <Field
                  label="anchor"
                  title="the model proposes a durable anchor; code tries to refute it before it is recorded"
                  value={
                    <>
                      <span className="mono">{turn.anchor_recorded ?? "none"}</span>
                      {turn.anchor_proposed && turn.anchor_proposed !== turn.anchor_recorded ? (
                        <span className="text-[var(--muted)]">
                          {" "}
                          (proposed “{turn.anchor_proposed}”, refuted)
                        </span>
                      ) : null}
                    </>
                  }
                />
              ) : null}
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Chip tone={turn.verdict}>{turn.verdict || "—"}</Chip>
                <Chip title="candidates the model was shown; the rest were truncated in reading order">
                  {turn.candidates_shown} shown
                  {turn.candidates_truncated ? ` · ${turn.candidates_truncated} cut` : ""}
                </Chip>
                <Chip>{ms(turn.latency_ms)}</Chip>
              </div>
              {turn.detail ? (
                <p className="pt-1 text-[11px] text-[var(--muted)]">{turn.detail}</p>
              ) : null}

              {/* The model's own words, and the answer it actually emitted.
                  `intent` above is its gloss on itself, written for the artifact;
                  these are what it said — which is what you want open when the
                  gloss and the behaviour disagree. */}
              {turn.text ? (
                <div className="mt-2">
                  <div className="mono pb-1 text-[10px] tracking-wider text-[var(--muted)] uppercase">
                    what the model said
                  </div>
                  <p className="rounded border border-[var(--rule)] bg-[#10151a] p-2 whitespace-pre-wrap">
                    {turn.text}
                  </p>
                </div>
              ) : null}
              {turn.arguments && Object.keys(turn.arguments).length > 0 ? (
                <Json value={turn.arguments} label={`${turn.call}(…) as emitted`} />
              ) : null}
            </>
          ) : (
            <Empty>
              No model was involved. Replay constructs no model client at all — this step ran
              from the recorded artifact.
            </Empty>
          )
        ) : null}

        {active === "guardrail" ? (
          policy ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Chip tone={policy.disposition}>{policy.disposition}</Chip>
                <Chip>{policy.action}</Chip>
                <Chip
                  tone={policy.promoted_from ? "recovered" : undefined}
                  title={
                    policy.promoted_from
                      ? "policy raised a step the recording declared safe — one-directional, on purpose"
                      : undefined
                  }
                >
                  risk: {policy.declared_risk}
                  {policy.promoted_from ? ` → ${policy.effective_risk}` : ""}
                </Chip>
                {policy.rule ? <Chip>rule: {policy.rule}</Chip> : null}
              </div>
              {policy.detail ? <Field label="because" value={policy.detail} /> : null}
              <Field label="classified" value={policy.intent || "—"} />
            </>
          ) : (
            <Empty>
              This step never reached the guardrail — a rejected tool call, or an entry
              navigation.
            </Empty>
          )
        ) : null}

        {active === "resolution" ? (
          trace ? (
            <>
              <Field label="target" value={trace.target_desc} />
              <Field label="anchor" value={trace.anchor_text ?? "—"} mono />
              {trace.relation !== "self" ? (
                <Field
                  label="relation"
                  value={trace.relation}
                  title="the control acted on is often not the thing with the words on it"
                />
              ) : null}
              <table className="mono mt-2 w-full text-[11px]">
                <tbody>
                  {trace.attempts.map((a, i) => (
                    <tr key={i} className="border-t border-[var(--rule)]">
                      <td
                        className="w-[110px] py-1 align-top"
                        style={{ color: statusColor(a.outcome) }}
                      >
                        {a.tier}
                      </td>
                      <td className="w-[56px] py-1 align-top text-[var(--muted)]">{a.outcome}</td>
                      <td className="py-1 align-top">
                        {a.matched_text ? `“${a.matched_text}”` : ""}
                        {a.candidates > 1 ? (
                          <span style={{ color: "var(--warn)" }}> ×{a.candidates}</span>
                        ) : null}
                        {a.detail ? <div className="text-[var(--muted)]">{a.detail}</div> : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="flex flex-wrap items-center gap-2 pt-2">
                <Chip tone={trace.drift ? "recovered" : "matched"}>resolved by {trace.tier}</Chip>
                {trace.drift ? (
                  <Chip
                    tone="recovered"
                    title="fell through to the recorded position — a drift event"
                  >
                    drift
                  </Chip>
                ) : null}
                {trace.candidates > 1 ? (
                  <Chip tone="recovered">{trace.candidates} candidates</Chip>
                ) : null}
              </div>
            </>
          ) : turn ? (
            // Discovery does not resolve. The model picks from an enumerated set
            // of marks, so there is never a "where is this thing" question — which
            // is also why the VLM tier is unreachable on the replay path.
            <Empty>
              Nothing to resolve: the model chose a numbered mark, and the element behind it
              supplied the anchor and box this step recorded. Replay is what resolves them.
            </Empty>
          ) : (
            <Empty>
              This step resolved no target — a navigate, a wait, or a step that stopped before
              the resolver was reached.
            </Empty>
          )
        ) : null}

        {active === "cost" ? <Cost step={step} /> : null}

        {active === "verify" ? (
          <>
            <Field label="expected" value={step.expected ?? "—"} />
            <Field label="observed" value={step.observed ?? "—"} />
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <Chip tone={step.status}>{step.status}</Chip>
              <Chip tone={step.settled_by === "text" ? "recovered" : undefined}>
                settled by {step.settled_by ?? "unset"}
              </Chip>
              <Chip>{ms(step.duration_ms)}</Chip>
              {step.recovery_applied ? (
                <Chip tone="recovered">recovery: {step.recovery_applied}</Chip>
              ) : null}
            </div>
            <Json value={step} label="raw step record" />
          </>
        ) : null}
      </div>
    </div>
  );
}


/**
 * Where the step's time went.
 *
 * Perception dominates and nothing else is close — on a dense back-office screen
 * an observation is ~2.4s of text recognition against ~30ms of GPU detection and
 * single-digit milliseconds of everything else. The bar exists so that stays a
 * measurement rather than a belief, and so the two changes that attack it are
 * visible in the data: `observations` per step (the frame a step ends on is the
 * next step's starting frame, so it should read 1 rather than 2) and the drop
 * when text recognition moves onto the GPU.
 */
function Cost({ step }: { step: StepRow }) {
  const p = step.phases;
  if (!p) {
    return <Empty>No timing recorded for this step.</Empty>;
  }
  const parts = [
    { label: "observe", ms: p.observe_ms, colour: "var(--accent)" },
    { label: "verify", ms: p.verify_ms, colour: "var(--warn)" },
    { label: "act", ms: p.act_ms, colour: "var(--ok)" },
    { label: "resolve", ms: p.resolve_ms, colour: "#a06fd0" },
  ].filter((s) => s.ms > 0);
  const total = Math.max(1, step.duration_ms);

  return (
    <>
      <div className="flex h-[10px] w-full overflow-hidden rounded-sm bg-[#232b34]">
        {parts.map((s) => (
          <span
            key={s.label}
            title={`${s.label}: ${ms(s.ms)}`}
            style={{ width: `${(s.ms / total) * 100}%`, background: s.colour }}
          />
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-2 pt-2">
        {parts.map((s) => (
          <Chip key={s.label} title={`${Math.round((s.ms / total) * 100)}% of the step`}>
            <span style={{ color: s.colour }}>■</span> {s.label} {ms(s.ms)}
          </Chip>
        ))}
        <Chip>total {ms(step.duration_ms)}</Chip>
      </div>
      <Field
        label="perceptions"
        value={String(p.observations)}
        title="full observations this step paid for — detection, text recognition and merge. One is the floor; two means the screen was read twice."
      />
      {p.observations > 1 ? (
        <p className="text-[11px] text-[var(--muted)]">
          More than one: a recovery fired, the step was re-executed, or it waited for its
          screen to arrive.
        </p>
      ) : null}
    </>
  );
}

/**
 * A long record, collapsed to one line until asked for.
 *
 * A turn prompt is thousands of characters — the whole candidate list off the
 * frame, plus the run's history — and the inspector is the shortest panel on the
 * screen. Shown open it buries every field below it; shown as a summary with its
 * size it stays one click away and says how much there is.
 */
function Detail({
  summary,
  chars,
  open,
  children,
}: {
  summary: string;
  chars: number;
  open?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details open={open} className="mb-1 rounded border border-[var(--rule)] bg-[#10151a]">
      <summary className="mono cursor-pointer px-2 py-1 text-[10px] tracking-wider text-[var(--muted)] uppercase">
        {summary}
        <span className="ml-2 normal-case opacity-60">{chars.toLocaleString()} chars</span>
      </summary>
      <p className="max-h-56 overflow-y-auto border-t border-[var(--rule)] px-2 py-1.5 text-[11px] whitespace-pre-wrap">
        {children}
      </p>
    </details>
  );
}
