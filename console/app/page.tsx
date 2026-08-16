"use client";

/**
 * One page, two modes.
 *
 * The earlier plan had a separate /operator/[run_id] route. It was cut: the
 * debug view and the operator console show the same thing — a run, its steps, and
 * the live screen — and the only difference is whether the operator may touch it.
 * Splitting them would mean an operator handling an escalation has to navigate to
 * a different page to see why it happened.
 *
 * So: the run log and the live session are always visible, and taking control
 * flips one flag.
 */

import { useCallback, useEffect, useState } from "react";
import { NoVncScreen } from "@/components/NoVncScreen";
import { NOVNC, api, type Intervention, type Run } from "@/lib/api";

const OPERATOR = "reviewer";

export default function Console() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [interventions, setInterventions] = useState<Intervention[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [hasControl, setHasControl] = useState(false);
  const [online, setOnline] = useState<boolean | null>(null);

  const poll = useCallback(async () => {
    const health = await api.health();
    setOnline(health !== null);
    setRuns((await api.runs()) ?? []);
    setInterventions((await api.interventions()) ?? []);
  }, []);

  useEffect(() => {
    void poll();
    const t = setInterval(() => void poll(), 1500);
    return () => clearInterval(t);
  }, [poll]);

  const run = runs.find((r) => r.run_id === selected) ?? runs[0] ?? null;
  const open = interventions.find(
    (i) => i.state === "pending" || i.state === "human_control",
  );

  async function take() {
    if (!open) return;
    await api.take(open.id, OPERATOR);
    setHasControl(true);
  }

  async function hand(outcome: "resume" | "abort") {
    if (!open) return;
    await api.resolve(open.id, outcome, OPERATOR);
    setHasControl(false);
  }

  return (
    <div className="flex h-screen flex-col">
      <header className="flex items-center gap-3 border-b border-[var(--rule)] px-4 py-2">
        <span className="text-[13px] font-semibold tracking-wide">AUTOMATION CONSOLE</span>
        <span className="mono text-[11px] text-[var(--muted)]">
          {online === null ? "…" : online ? "control plane: up" : "control plane: unreachable"}
        </span>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* Left: runs, escalation, step log */}
        <aside className="flex w-[420px] shrink-0 flex-col gap-3 overflow-y-auto border-r border-[var(--rule)] p-3">
          <Runs runs={runs} selected={run?.run_id ?? null} onSelect={setSelected} />
          <Escalation
            intervention={open}
            hasControl={hasControl}
            onTake={take}
            onResume={() => hand("resume")}
            onAbort={() => hand("abort")}
          />
          <Steps run={run} />
        </aside>

        {/* Right: the live session */}
        <section className="min-w-0 flex-1 p-3">
          <div className="panel flex h-full flex-col">
            <div className="panel-hd flex items-center justify-between">
              <span>Live Session</span>
              <span className="mono normal-case">
                {open?.vnc_url ?? NOVNC}
              </span>
            </div>
            <div className="min-h-0 flex-1">
              <NoVncScreen url={open?.vnc_url ?? NOVNC} viewOnly={!hasControl} />
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}

function Runs({
  runs,
  selected,
  onSelect,
}: {
  runs: Run[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="panel">
      <div className="panel-hd">Runs</div>
      <div className="max-h-[180px] overflow-y-auto">
        {runs.length === 0 ? (
          <p className="p-3 text-[12px] text-[var(--muted)]">
            No runs yet. Start one with <code className="mono">cua discover</code> or by
            invoking a capability.
          </p>
        ) : (
          runs.map((r) => (
            <button
              key={r.run_id}
              onClick={() => onSelect(r.run_id)}
              className="block w-full border-b border-[var(--rule)] px-3 py-2 text-left last:border-0 hover:bg-[#222a33]"
              style={{ background: r.run_id === selected ? "#222a33" : undefined }}
            >
              <span className="mono">{r.run_id}</span>
              <StatusDot status={r.status} />
              <div className="text-[11px] text-[var(--muted)]">
                {r.mode} · {r.capability ?? r.goal ?? "—"}
              </div>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * The escalation card.
 *
 * Carries the context §3.6 requires an operator to have before acting: which
 * capability and goal, which step, why it stopped, and what was expected against
 * what was observed. An operator should not have to read a log to decide.
 */
function Escalation({
  intervention,
  hasControl,
  onTake,
  onResume,
  onAbort,
}: {
  intervention?: Intervention;
  hasControl: boolean;
  onTake: () => void;
  onResume: () => void;
  onAbort: () => void;
}) {
  if (!intervention) {
    return (
      <div className="panel">
        <div className="panel-hd">Intervention</div>
        <p className="p-3 text-[12px] text-[var(--muted)]">
          Nothing waiting. When a run gets stuck, hits an undeclared dialog, or reaches a
          risky action, it parks here and the live session becomes controllable.
        </p>
      </div>
    );
  }

  return (
    <div className="panel border-[var(--warn)]">
      <div className="panel-hd" style={{ color: "var(--warn)" }}>
        Intervention required
      </div>
      <div className="space-y-2 p-3 text-[12px]">
        <Field label="reason" value={intervention.reason} />
        <Field label="capability" value={intervention.capability ?? intervention.goal} />
        <Field
          label="step"
          value={`${intervention.step_id ?? "—"} · ${intervention.step_intent}`}
        />
        {intervention.expected ? <Field label="expected" value={intervention.expected} /> : null}
        {intervention.observed ? <Field label="observed" value={intervention.observed} /> : null}
        <p className="text-[var(--muted)]">{intervention.message}</p>

        <div className="flex gap-2 pt-1">
          <button className="btn btn-accent" onClick={onTake} disabled={hasControl}>
            Take control
          </button>
          <button className="btn" onClick={onResume} disabled={!hasControl}>
            Hand back &amp; resume
          </button>
          <button className="btn btn-danger" onClick={onAbort} disabled={!hasControl}>
            Abort run
          </button>
        </div>
        <p className="text-[11px] text-[var(--muted)]">
          Your actions during control are recorded to the run&apos;s evidence. On resume the
          runner re-observes rather than assuming which step you left it on.
        </p>
      </div>
    </div>
  );
}

function Steps({ run }: { run: Run | null }) {
  return (
    <div className="panel min-h-[200px] flex-1">
      <div className="panel-hd">Steps</div>
      {!run || run.steps.length === 0 ? (
        <p className="p-3 text-[12px] text-[var(--muted)]">No steps recorded.</p>
      ) : (
        <table className="mono w-full">
          <tbody>
            {run.steps.map((s) => (
              <tr key={s.step_id} className="border-b border-[var(--rule)] last:border-0">
                <td className="px-2 py-1 align-top text-[var(--muted)]">{s.step_id}</td>
                <td className="px-2 py-1 align-top">
                  <div>{s.intent}</div>
                  {s.expected ? (
                    <div className="text-[11px] text-[var(--err)]">
                      expected {s.expected} · observed {s.observed ?? "—"}
                    </div>
                  ) : null}
                  {/* Which resolver tier satisfied this target. A run whose steps
                      drift toward `recorded_bbox` is the early warning that the app
                      moved — visible here before it becomes a failure. */}
                  <div className="text-[11px] text-[var(--muted)]">
                    {s.resolution} · {s.duration_ms}ms
                  </div>
                </td>
                <td className="px-2 py-1 align-top">
                  <StatusDot status={s.status} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value?: string | null }) {
  return (
    <div className="flex gap-2">
      <span className="mono w-[80px] shrink-0 text-[var(--muted)]">{label}</span>
      <span>{value ?? "—"}</span>
    </div>
  );
}

const COLORS: Record<string, string> = {
  ok: "var(--ok)",
  success: "var(--ok)",
  recovered: "var(--warn)",
  business_outcome: "var(--accent)",
  escalated: "var(--warn)",
  running: "var(--muted)",
  failed: "var(--err)",
  failure: "var(--err)",
};

function StatusDot({ status }: { status: string }) {
  return (
    <span className="mono ml-2 text-[11px]" style={{ color: COLORS[status] ?? "var(--muted)" }}>
      ● {status}
    </span>
  );
}
