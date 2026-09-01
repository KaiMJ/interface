"use client";

/**
 * One rail, three lists, one at a time — what is this run doing, what has this system
 * done, what can it do. Tabs rather than three columns, since you never ask two at once.
 *
 * The horizontal rail above the frame is a scrubber: shape and timing at a glance,
 * deliberately saying nothing about what each step *is*.
 */

import { useEffect, useState } from "react";
import { Tabs } from "./Shell";
import { Chip, Empty, StatusDot, ms } from "./ui";
import {
  api,
  isFailed,
  type CapabilitySummary,
  type Run,
  type RunSummary,
  type Thinking,
} from "@/lib/api";

const TABS = ["capabilities", "runs", "steps"] as const;
type Tab = (typeof TABS)[number];

export function Navigator({
  run,
  pending,
  step,
  onSelectStep,
  runs,
  capabilities,
  selectedRun,
  selectedCapability,
  filter,
  operator,
  onSelectRun,
  onSelectCapability,
  onFilter,
  onChanged,
  onReplay,
}: {
  /** The selected run, whose steps are the first thing an operator wants. */
  run: Run | null;
  /** The step being decided right now, if the run is waiting on the model. */
  pending: Thinking | null;
  step: number | null;
  onSelectStep: (index: number) => void;
  runs: RunSummary[];
  capabilities: CapabilitySummary[];
  selectedRun: string | null;
  selectedCapability: string | null;
  filter: string;
  operator: string;
  onSelectRun: (id: string) => void;
  onSelectCapability: (ref: string) => void;
  onFilter: (capabilityId: string) => void;
  onChanged: () => void;
  onReplay: (ref: string) => void;
}) {
  const [tab, setTab] = useState<Tab>("steps");
  const [error, setError] = useState<string | null>(null);

  const shown = filter
    ? runs.filter((r) => (r.capability ?? "").startsWith(filter))
    : runs;

  async function approve(c: CapabilitySummary) {
    const result = await api.approve(c.id, c.version, operator);
    setError(isFailed(result) ? result.error : null);
    if (!isFailed(result)) onChanged();
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <Tabs
        tabs={TABS}
        active={tab}
        onSelect={setTab}
        counts={{
          steps: run?.steps.length ?? 0,
          runs: shown.length,
          capabilities: capabilities.length,
        }}
      />

      {tab === "runs" && filter ? (
        <button
          className="mono border-b border-[var(--rule)] px-3 py-1 text-left text-[11px] text-[var(--accent)]"
          onClick={() => onFilter("")}
        >
          {filter} · clear filter ×
        </button>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "steps" ? (
          !run || (run.steps.length === 0 && !pending) ? (
            <Empty>
              No steps yet. They appear here as the run writes them — the console tails the
              run's own evidence, so a run started from the CLI streams here too.
            </Empty>
          ) : (
            run.steps.map((s, i) => (
              <button
                key={`${s.step_id}-${i}`}
                onClick={() => onSelectStep(i)}
                className="block w-full border-b border-[var(--rule)] px-3 py-2 text-left last:border-0 hover:bg-[#222a33]"
                style={{ background: i === step ? "#222a33" : undefined }}
              >
                <div className="flex items-baseline gap-2">
                  <StatusDot status={s.status} label={false} />
                  <span className="mono text-[11px] text-[var(--muted)]">{s.step_id}</span>
                  <span className="min-w-0 flex-1 text-[12px] break-words">{s.intent}</span>
                </div>
                <div className="flex flex-wrap items-center gap-1 pt-1 pl-[14px]">
                  {/* What this step is, in the three facts that distinguish one
                      from the next: how its target was found, what the model did
                      with its own answer, and what it cost. */}
                  <Chip
                    tone={s.resolution === "recorded_bbox" ? "recovered" : undefined}
                    title="which tier of the resolver ladder produced the coordinate"
                  >
                    {s.resolution}
                  </Chip>
                  {s.model_turn?.verdict && s.model_turn.verdict !== "kept" ? (
                    <Chip tone={s.model_turn.verdict}>{s.model_turn.verdict}</Chip>
                  ) : null}
                  {s.policy?.promoted_from ? <Chip tone="recovered">↑risky</Chip> : null}
                  {s.recovery_applied ? (
                    <Chip tone="recovered">{s.recovery_applied}</Chip>
                  ) : null}
                  <Chip>{ms(s.duration_ms)}</Chip>
                </div>
                {/* Whatever the model actually produced. A forced tool call leaves
                    `text` empty on a reasoning model, so the chain of thought is the
                    only thing there is to preview. */}
                {s.model_turn?.text || s.model_turn?.reasoning ? (
                  <p className="truncate pt-1 pl-[14px] text-[11px] text-[var(--muted)]">
                    “{s.model_turn.text || s.model_turn.reasoning}”
                  </p>
                ) : null}
              </button>
            ))
          )
        ) : tab === "runs" ? (
          shown.length === 0 ? (
            <Empty>No runs yet. Start one from “New run”.</Empty>
          ) : (
            shown.map((r) => (
              <Row
                key={r.run_id}
                selected={r.run_id === selectedRun}
                onClick={() => onSelectRun(r.run_id)}
                title={r.run_id}
                status={r.status}
                // What distinguishes this run from the next: what it ran, or — for a
                // discovery run, which has no capability yet — what it was asked to do.
                detail={`${r.capability ?? r.goal ?? r.kind}`}
                meta={r.duration_ms ? ms(r.duration_ms) : ""}
              />
            ))
          )
        ) : capabilities.length === 0 ? (
          <Empty>
            Nothing recorded yet. A successful discovery run emits one and it appears here.
          </Empty>
        ) : (
          capabilities.map((c) => (
            <div
              key={c.ref}
              className="border-b border-[var(--rule)] last:border-0"
              style={{ background: c.ref === selectedCapability ? "#222a33" : undefined }}
            >
              <Row
                selected={false}
                onClick={() => onSelectCapability(c.ref)}
                title={c.ref}
                status={c.status}
                detail={c.goal}
                meta={`${c.steps} steps`}
                flat
              />
              <div className="flex gap-1 px-3 pb-2">
                <button className="btn" onClick={() => onReplay(c.ref)}>
                  replay
                </button>
                <button className="btn" onClick={() => onFilter(c.id)}>
                  runs
                </button>
                {c.status === "draft" ? (
                  <button
                    className="btn"
                    onClick={() => approve(c)}
                    title="draft → approved. The gate on unattended replay; an agent may only call approved capabilities."
                  >
                    approve
                  </button>
                ) : null}
              </div>
            </div>
          ))
        )}
        {tab === "steps" && pending ? <Pending pending={pending} /> : null}
        {error ? <p className="px-3 py-2 text-[11px] text-[var(--err)]">{error}</p> : null}
      </div>
    </div>
  );
}

function Row({
  title,
  detail,
  meta,
  status,
  selected,
  onClick,
  flat,
}: {
  title: string;
  detail: string;
  meta?: string;
  status: string;
  selected: boolean;
  onClick: () => void;
  flat?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      className={`block w-full px-3 py-1.5 text-left hover:bg-[#222a33] ${
        flat ? "" : "border-b border-[var(--rule)] last:border-0"
      }`}
      style={{ background: selected ? "#222a33" : undefined }}
    >
      <div className="flex items-baseline gap-2">
        <StatusDot status={status} label={false} />
        <span className="mono min-w-0 flex-1 truncate text-[12px]">{title}</span>
        {meta ? <span className="mono text-[10px] text-[var(--muted)]">{meta}</span> : null}
      </div>
      <div className="truncate pl-[14px] text-[11px] text-[var(--muted)]">{detail}</div>
    </button>
  );
}

/**
 * The live edge of a running discovery: the step being decided, before there is any
 * outcome to record. Discovery spends roughly half its wall clock waiting on the model,
 * and without this a live run is indistinguishable from a hung one.
 *
 * The elapsed seconds are counted here rather than sent — the server has nothing new to
 * say each second.
 */
function Pending({ pending }: { pending: Thinking }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setNow(Date.now());
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [pending.step_id, pending.since]);

  const since = Date.parse(pending.since);
  const elapsed = Number.isNaN(since) ? null : Math.max(0, Math.round((now - since) / 1000));

  return (
    <div className="border-b border-[var(--rule)] px-3 py-2 last:border-0">
      <div className="flex items-baseline gap-2">
        <span className="h-[6px] w-[6px] animate-pulse rounded-full bg-[var(--muted)]" />
        <span className="mono text-[11px] text-[var(--muted)]">{pending.step_id}</span>
        <span className="min-w-0 flex-1 text-[12px] text-[var(--muted)] italic">
          deciding what to do next…
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-1 pt-1 pl-[14px]">
        <Chip title="the model this run is waiting on">{pending.model || "model"}</Chip>
        {elapsed === null ? null : <Chip>{elapsed}s</Chip>}
      </div>
    </div>
  );
}
