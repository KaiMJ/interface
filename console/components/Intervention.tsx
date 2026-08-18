"use client";

/**
 * The escalation queue and the control transfer, in the console itself.
 *
 * There is no separate operator route. The debug view and the operator console
 * show the same thing, and the only difference is whether the operator may touch
 * it — splitting them would mean someone handling an escalation has to navigate
 * away from the evidence to see why it happened.
 *
 * The card carries the context §3.6 requires before acting: which capability and
 * goal, which step, why it stopped, and expected against observed. Below it, once
 * the transfer has happened, is the record of what the human did — the handoff and
 * handback frames and every input captured at the X layer. Keystrokes are counted
 * and named, never recorded: the operator may be typing a credential.
 */

import { useState } from "react";
import { Chip, Empty, Field, Panel, StatusDot } from "./ui";
import { api, evidenceUrl, isFailed, type Evidence, type Intervention } from "@/lib/api";

export function InterventionPanel({
  queue,
  operator,
  activeRunId,
  evidence,
  onSelectRun,
  onChanged,
}: {
  queue: Intervention[];
  operator: string;
  activeRunId: string | null;
  evidence: Evidence | null;
  onSelectRun: (runId: string) => void;
  onChanged: () => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const open = queue.find((i) => i.state === "pending" || i.state === "human_control") ?? null;
  const holding = open?.state === "human_control";

  async function act(kind: "take" | "resume" | "abort") {
    if (!open) return;
    setBusy(true);
    setError(null);
    const result =
      kind === "take"
        ? await api.take(open.id, operator)
        : await api.resolve(open.id, kind, operator, note);
    setBusy(false);
    if (isFailed(result)) {
      setError(result.error);
      return;
    }
    if (kind !== "take") setNote("");
    onChanged();
  }

  const handoff = evidence?.intervention;
  const actions = evidence?.human_actions ?? [];
  const past = queue.filter((i) => i.state === "resolved" || i.state === "aborted");
  const record = handoff && (handoff.handoff || handoff.resolution || actions.length > 0);

  // Nothing waiting and nothing to review: one muted line, not a panel. The
  // queue is empty almost all the time, and a card explaining that it is empty
  // is a card in the way of the thing you are actually looking at.
  if (!open && !record && past.length === 0) {
    return (
      <p className="border-t border-[var(--rule)] px-3 py-2 text-[11px] text-[var(--muted)]">
        No intervention waiting. A run that gets stuck, hits an undeclared dialog or reaches a
        risky action parks here and the session becomes controllable.
      </p>
    );
  }

  return (
    <Panel
      title={open ? "Intervention required" : "Handoff record"}
      tone={open ? "warn" : undefined}
      right={
        open ? (
          <button className="btn" onClick={() => onSelectRun(open.run_id)}>
            {open.run_id}
          </button>
        ) : null
      }
    >
      {open ? (
        <div className="space-y-2 p-3 text-[12px]">
          <div className="flex flex-wrap items-center gap-1">
            <Chip tone={holding ? "recovered" : "failed"}>{open.state}</Chip>
            <Chip>{open.mode}</Chip>
            <Chip>{open.reason}</Chip>
            {open.failure_kind ? <Chip tone="failed">{open.failure_kind}</Chip> : null}
          </div>
          <Field label="capability" value={open.capability ?? open.goal} />
          <Field label="step" value={`${open.step_id ?? "—"} · ${open.step_intent}`} />
          <Field label="why" value={open.message} />
          {open.expected ? <Field label="expected" value={open.expected} /> : null}
          {open.observed ? <Field label="observed" value={open.observed} /> : null}

          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            rows={2}
            placeholder="what you did, for the record — written into the run's evidence"
            className="w-full resize-y rounded border border-[var(--rule)] bg-[#10151a] p-2 text-[12px] outline-none focus:border-[var(--accent)]"
          />

          <div className="flex flex-wrap gap-2">
            <button
              className="btn btn-accent"
              onClick={() => act("take")}
              disabled={busy || holding}
            >
              Take control
            </button>
            <button className="btn" onClick={() => act("resume")} disabled={busy || !holding}>
              Hand back &amp; resume
            </button>
            <button
              className="btn btn-danger"
              onClick={() => act("abort")}
              disabled={busy || !holding}
            >
              Abort run
            </button>
          </div>
          {error ? <p className="text-[11px] text-[var(--err)]">{error}</p> : null}
          <p className="text-[11px] text-[var(--muted)]">
            Until you take it, nobody holds control — the automation has stopped and your clicks
            go nowhere. That interval is what makes “the agent clicked while I was typing”
            impossible rather than unlikely. On resume the runner re-observes rather than
            assuming which step you left it on.
          </p>
        </div>
      ) : null}

      {/* The record of a transfer that already happened, on the selected run. */}
      {record ? (
        <div className="space-y-2 border-t border-[var(--rule)] p-3 text-[12px]">
          {handoff.resolution ? (
            <>
              <Field
                label="outcome"
                value={
                  <>
                    <StatusDot
                      status={handoff.resolution.outcome === "abort" ? "failed" : "ok"}
                      label={false}
                    />{" "}
                    {handoff.resolution.outcome} by {handoff.resolution.operator}
                  </>
                }
              />
              {handoff.resolution.note ? (
                <Field label="note" value={handoff.resolution.note} />
              ) : null}
            </>
          ) : null}

          {activeRunId && (handoff.handoff || handoff.handback) ? (
            <div className="flex gap-2">
              {(["handoff", "handback"] as const).map((which) =>
                handoff[which] ? (
                  <figure key={which} className="min-w-0 flex-1">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={evidenceUrl(activeRunId, handoff[which]!)}
                      alt={which}
                      className="w-full border border-[var(--rule)]"
                    />
                    <figcaption className="mono pt-0.5 text-[10px] text-[var(--muted)]">
                      {which}
                    </figcaption>
                  </figure>
                ) : null,
              )}
            </div>
          ) : null}

          {actions.length > 0 ? (
            <div className="max-h-[160px] overflow-y-auto rounded border border-[var(--rule)]">
              <table className="mono w-full text-[11px]">
                <tbody>
                  {actions.map((a, i) => (
                    <tr key={i} className="border-b border-[var(--rule)] last:border-0">
                      <td className="px-2 py-0.5 text-[var(--muted)]">
                        {a.at.slice(11, 19)}
                      </td>
                      <td className="px-2 py-0.5">{a.kind}</td>
                      <td className="px-2 py-0.5 text-[var(--muted)]">
                        {a.x !== null && a.x !== undefined ? `${a.x},${a.y}` : ""}
                      </td>
                      <td className="px-2 py-0.5 break-all">{a.detail ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}

      {/* Everything already dealt with this session. */}
      {past.length > 0 ? (
        <div className="border-t border-[var(--rule)] p-3">
          <div className="mono pb-1 text-[10px] tracking-wider text-[var(--muted)] uppercase">
            Resolved this session
          </div>
          {past.map((i) => (
              <button
                key={i.id}
                onClick={() => onSelectRun(i.run_id)}
                className="mono block w-full py-0.5 text-left text-[11px] hover:text-[var(--accent)]"
            >
              <StatusDot status={i.state === "aborted" ? "failed" : "ok"} label={false} />{" "}
              {i.run_id} · {i.reason}
            </button>
          ))}
        </div>
      ) : null}
    </Panel>
  );
}
