"use client";

/**
 * The run, as a bar and a rail.
 *
 * `RunBar` is one line of identity and one of result. Everything else a run
 * carries — inputs, model, duration, the raw record — is a detail you open, not
 * a detail you are shown, because you read it once per run and the rest of the
 * time it is noise between you and the frame.
 *
 * `StepRail` replaces a whole column. A run is five to thirty steps; laid out
 * horizontally with the status in the colour and the detail in the tooltip, it
 * costs one row instead of a third of the screen, and the shape of the run — how
 * long each step took, where it went wrong — is legible at a glance in a way a
 * vertical list of six chips per row never was.
 */

import { Chip, Field, Json, StatusDot, ms, statusColor } from "./ui";
import type { Evidence, Run, StepRow } from "@/lib/api";

export function RunBar({
  run,
  evidence,
  onOpenDetails,
  onOpenContracts,
}: {
  run: Run | null;
  evidence: Evidence | null;
  onOpenDetails: () => void;
  onOpenContracts: () => void;
}) {
  if (!run) {
    return (
      <div className="flex items-center gap-2 border-b border-[var(--rule)] px-3 py-2 text-[12px] text-[var(--muted)]">
        No run selected.
      </div>
    );
  }
  const actions = evidence?.human_actions ?? [];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--rule)] px-3 py-2 text-[12px]">
      <StatusDot status={run.status} />
      <span className="mono">{run.run_id}</span>
      {run.capability || run.capability_ref ? (
        <span className="mono text-[var(--muted)]">{run.capability ?? run.capability_ref}</span>
      ) : null}

      {/* The result, in the words the caller gets. One of these, never two. */}
      {run.outputs && Object.keys(run.outputs).length > 0 ? (
        <span className="mono" style={{ color: "var(--ok)" }}>
          {JSON.stringify(run.outputs)}
        </span>
      ) : null}
      {run.outcome ? (
        <span className="mono" style={{ color: "var(--accent)" }}>
          {run.outcome.name}
          {run.outcome.step_id ? ` @${run.outcome.step_id}` : ""}
        </span>
      ) : null}
      {run.failure ? (
        <span className="mono truncate" style={{ color: "var(--err)" }} title={run.failure.message}>
          {run.failure.kind}
          {run.failure.step_id ? ` @${run.failure.step_id}` : ""}
        </span>
      ) : null}
      {actions.length > 0 ? (
        <Chip tone="recovered" title="captured at the X layer during a handoff">
          operator ×{actions.length}
        </Chip>
      ) : null}

      <span className="ml-auto flex items-center gap-2">
        {run.duration_ms ? (
          <span className="mono text-[11px] text-[var(--muted)]">{ms(run.duration_ms)}</span>
        ) : null}
        <button className="btn" onClick={onOpenDetails}>
          details
        </button>
        <button className="btn" onClick={onOpenContracts}>
          contracts
        </button>
      </span>
    </div>
  );
}

/** The full record, in the drawer. Read once per run, not kept on screen. */
export function RunDetails({ run, evidence }: { run: Run | null; evidence: Evidence | null }) {
  if (!run) return null;
  return (
    <div className="space-y-1 text-[12px]">
      <Field label="run" value={run.run_id} mono />
      {run.goal ? <Field label="goal" value={run.goal} /> : null}
      {run.inputs && Object.keys(run.inputs).length > 0 ? (
        <Field
          label="inputs"
          value={JSON.stringify(run.inputs)}
          mono
          title="as the caller sent them, minus anything declared sensitive"
        />
      ) : null}
      {run.outputs && Object.keys(run.outputs).length > 0 ? (
        <Field label="outputs" value={JSON.stringify(run.outputs)} mono />
      ) : null}
      {run.outcome ? (
        <>
          <Field label="outcome" value={run.outcome.name} />
          <Field label="fields" value={JSON.stringify(run.outcome.fields ?? {})} mono />
        </>
      ) : null}
      {run.failure ? (
        <>
          <Field label="failure" value={run.failure.kind} />
          <Field label="at step" value={String(run.failure.step_id ?? "—")} />
          <Field label="message" value={run.failure.message} />
          <Field label="expected" value={run.failure.expected} />
          <Field label="observed" value={run.failure.observed} />
        </>
      ) : null}
      {run.stop_reason ? <Field label="stopped" value={run.stop_reason} /> : null}
      <Field label="app" value={run.app ?? "—"} />
      <Field label="steps" value={String(run.steps.length)} />
      <Field label="duration" value={ms(run.duration_ms)} />
      {run.model ? <Field label="model" value={run.model} /> : null}
      {run.llm_calls !== undefined ? (
        <Field
          label="model calls"
          value={String(run.llm_calls)}
          title="replay constructs no model client at all; a non-zero count here can only be a discovery run"
        />
      ) : null}
      <Field label="started" value={run.started_at?.replace("T", " ").slice(0, 19)} />
      <Json value={run} label="raw run.json" />
      {evidence?.human_actions?.length ? (
        <Json value={evidence.human_actions} label="operator actions" />
      ) : null}
    </div>
  );
}

export function StepRail({
  run,
  selected,
  follow,
  onSelect,
  onFollow,
}: {
  run: Run | null;
  selected: number | null;
  follow: boolean;
  onSelect: (index: number) => void;
  onFollow: (on: boolean) => void;
}) {
  const steps = run?.steps ?? [];
  const longest = Math.max(1, ...steps.map((s) => s.duration_ms || 0));
  return (
    <div className="flex items-center gap-1 overflow-x-auto border-b border-[var(--rule)] px-3 py-1.5">
      <button
        className="btn shrink-0"
        style={follow ? { borderColor: "var(--accent)" } : { color: "var(--muted)" }}
        onClick={() => onFollow(!follow)}
        title="follow the live edge of the run, or pin a step to debug it"
      >
        {follow ? "live" : "pinned"}
      </button>
      {steps.length === 0 ? (
        <span className="mono px-2 text-[11px] text-[var(--muted)]">no steps yet</span>
      ) : null}
      {steps.map((s, i) => (
        <button
          key={`${s.step_id}-${i}`}
          onClick={() => onSelect(i)}
          title={`${s.step_id}. ${s.intent}\n${s.status} · ${s.resolution} · ${ms(s.duration_ms)}`}
          className="mono flex shrink-0 flex-col items-center rounded-sm px-2 py-0.5 text-[11px]"
          style={{
            background: i === selected ? "#2b3540" : "transparent",
            border: `1px solid ${i === selected ? "var(--accent)" : "var(--rule)"}`,
            color: statusColor(s.status),
          }}
        >
          {s.step_id}
          {/* Where the time went. A step that is most of a run's wall clock is
              usually waiting on a checkpoint nobody looks at. */}
          <span
            className="mt-0.5 block h-[2px] w-5"
            style={{
              background: statusColor(s.status),
              opacity: 0.25 + 0.75 * ((s.duration_ms || 0) / longest),
            }}
          />
        </button>
      ))}
    </div>
  );
}

/** The one-line summary of the selected step, above the frame. */
export function StepLine({ step }: { step: StepRow | null }) {
  if (!step) return null;
  return (
    <span className="flex min-w-0 items-center gap-2">
      <span className="mono text-[var(--muted)]">{step.step_id}</span>
      <span className="min-w-0 truncate normal-case">{step.intent}</span>
    </span>
  );
}
