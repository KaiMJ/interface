"use client";

/**
 * One page, three columns, no routing.
 *
 * Left is what happened: runs, the intervention queue, and the step log. Centre
 * is what the agent saw when it happened — the frame for the selected step, with
 * a toggle between the clean capture an operator sees over VNC and the numbered
 * overlay the model was actually shown. Right is the live session plus the two
 * contracts that govern it: the capability being invoked and the policy in force.
 *
 * The earlier version had a separate /operator/[run_id] route. It was cut: the
 * debug view and the operator console show the same thing, and the only
 * difference is whether the operator may touch it. Splitting them would mean
 * someone handling an escalation has to navigate away to see why it happened.
 */

import { useCallback, useEffect, useState } from "react";
import { NoVncScreen } from "@/components/NoVncScreen";
import {
  NOVNC,
  api,
  evidenceUrl,
  type Capability,
  type Evidence,
  type Intervention,
  type Policy,
  type Run,
  type RunSummary,
  type StepRow,
} from "@/lib/api";

const OPERATOR = "reviewer";

export default function Console() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [interventions, setInterventions] = useState<Intervention[]>([]);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [capability, setCapability] = useState<Capability | null>(null);
  const [step, setStep] = useState<number | null>(null);
  const [marks, setMarks] = useState(true);
  const [hasControl, setHasControl] = useState(false);
  const [online, setOnline] = useState<boolean | null>(null);

  const poll = useCallback(async () => {
    setOnline((await api.health()) !== null);
    setRuns((await api.runs()) ?? []);
    setInterventions((await api.interventions()) ?? []);
  }, []);

  useEffect(() => {
    void poll();
    const t = setInterval(() => void poll(), 1500);
    return () => clearInterval(t);
  }, [poll]);

  useEffect(() => {
    void api.policy().then(setPolicy);
  }, []);

  // The list carries summaries; a run's steps and evidence come from its own
  // endpoints. Fetching them together keeps the frame and the step row in sync.
  const current = selectedRun ?? runs[0]?.run_id ?? null;
  useEffect(() => {
    if (!current) return;
    let live = true;
    void (async () => {
      const [detail, files] = await Promise.all([api.run(current), api.evidence(current)]);
      if (!live) return;
      setRun(detail);
      setEvidence(files);
      setStep((previous) =>
        previous !== null && files?.steps.some((s) => s.step_id === previous)
          ? previous
          : (files?.steps.at(-1)?.step_id ?? null),
      );
    })();
    return () => {
      live = false;
    };
  }, [current, runs.length, interventions.length]);

  // A discovery run leaves its capability in evidence; a replay run only names
  // the one it executed, so that one is fetched from the catalog. Either way the
  // contract being run is on screen beside the run.
  useEffect(() => {
    const inEvidence = evidence?.capability ?? null;
    if (inEvidence) {
      setCapability(inEvidence);
      return;
    }
    const ref = run?.capability ?? run?.capability_ref ?? null;
    if (!ref) {
      setCapability(null);
      return;
    }
    void api.capability(ref).then(setCapability);
  }, [evidence, run]);

  const open = interventions.find((i) => i.state === "pending" || i.state === "human_control");
  const frame = evidence?.steps.find((s) => s.step_id === step) ?? null;
  const stepRow = run?.steps.find((s) => s.step_id === step) ?? null;

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
        {policy ? (
          <span className="mono ml-auto text-[11px] text-[var(--muted)]">
            policy: {policy.app} · {policy.allowed_actions.length} actions · risky:{" "}
            {policy.risky_disposition}
          </span>
        ) : null}
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="flex w-[360px] shrink-0 flex-col gap-3 overflow-y-auto border-r border-[var(--rule)] p-3">
          <Runs runs={runs} selected={current} onSelect={setSelectedRun} />
          <Escalation
            intervention={open}
            hasControl={hasControl}
            onTake={take}
            onResume={() => hand("resume")}
            onAbort={() => hand("abort")}
          />
          <Steps run={run} selected={step} onSelect={setStep} />
        </aside>

        <section className="flex min-w-0 flex-1 flex-col gap-3 p-3">
          <Frame
            runId={current}
            frame={frame}
            step={stepRow}
            marks={marks}
            onToggle={() => setMarks((m) => !m)}
          />
          <Result run={run} evidence={evidence} />
        </section>

        <aside className="flex w-[380px] shrink-0 flex-col gap-3 overflow-y-auto border-l border-[var(--rule)] p-3">
          <div className="panel">
            <div className="panel-hd flex items-center justify-between">
              <span>Live Session</span>
              <span className="mono normal-case">{hasControl ? "you" : "automation"}</span>
            </div>
            <div className="h-[240px]">
              <NoVncScreen url={open?.vnc_url ?? NOVNC} viewOnly={!hasControl} />
            </div>
          </div>
          <CapabilityCard capability={capability} />
          <SynthesisCard evidence={evidence} />
          <PolicyCard policy={policy} />
        </aside>
      </div>
    </div>
  );
}

function Runs({
  runs,
  selected,
  onSelect,
}: {
  runs: RunSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="panel">
      <div className="panel-hd">Runs</div>
      <div className="max-h-[160px] overflow-y-auto">
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
                {r.kind} · {r.capability ?? "—"}
              </div>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/**
 * The frame for the selected step.
 *
 * Two images per step and the toggle matters: the clean capture is what the
 * operator sees over VNC, and the annotated one is what the model was shown, so
 * any argument about a decision it made is litigated against the second.
 */
function Frame({
  runId,
  frame,
  step,
  marks,
  onToggle,
}: {
  runId: string | null;
  frame: { step_id: number; frame: string; annotated: string | null } | null;
  step: StepRow | null;
  marks: boolean;
  onToggle: () => void;
}) {
  const showMarks = marks && !!frame?.annotated;
  return (
    <div className="panel flex min-h-0 flex-1 flex-col">
      <div className="panel-hd flex items-center justify-between">
        <span>
          {frame ? `Step ${frame.step_id}` : "Frame"}
          {step ? ` · ${step.intent}` : ""}
        </span>
        <span className="flex items-center gap-2">
          {frame?.annotated ? (
            <button className="btn" onClick={onToggle}>
              {showMarks ? "showing marks" : "showing capture"}
            </button>
          ) : (
            <span className="mono normal-case text-[var(--muted)]">no overlay</span>
          )}
        </span>
      </div>
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-black">
        {runId && frame ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={evidenceUrl(runId, showMarks && frame.annotated ? frame.annotated : frame.frame)}
            alt={`step ${frame.step_id}`}
            className="max-h-full max-w-full object-contain"
          />
        ) : (
          <p className="p-6 text-[12px] text-[var(--muted)]">
            No frame for this step. Evidence is written per step as a run proceeds.
          </p>
        )}
      </div>
      {step ? (
        <div className="border-t border-[var(--rule)] p-2 text-[11px]">
          <span className="mono text-[var(--muted)]">
            {step.resolution} · {step.duration_ms}ms ·{" "}
          </span>
          <StatusDot status={step.status} />
          {step.expected ? (
            <div className="mt-1">
              <span className="mono text-[var(--muted)]">expected </span>
              {step.expected}
              <span className="mono text-[var(--muted)]"> · observed </span>
              {step.observed ?? "—"}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** What the caller got back: outputs, a declared outcome, or a failure. */
function Result({ run, evidence }: { run: Run | null; evidence: Evidence | null }) {
  if (!run) return null;
  const actions = evidence?.human_actions ?? [];
  return (
    <div className="panel">
      <div className="panel-hd flex items-center justify-between">
        <span>Result</span>
        <StatusDot status={run.status} />
      </div>
      <div className="space-y-1 p-3 text-[12px]">
        {run.outputs && Object.keys(run.outputs).length > 0 ? (
          <Field label="outputs" value={JSON.stringify(run.outputs)} />
        ) : null}
        {run.outcome ? (
          <>
            <Field label="outcome" value={run.outcome.name} />
            <Field label="fields" value={JSON.stringify(run.outcome.fields ?? {})} />
          </>
        ) : null}
        {run.failure ? (
          <>
            <Field label="failure" value={run.failure.kind} />
            <Field label="at step" value={String(run.failure.step_id ?? "—")} />
            <Field label="expected" value={run.failure.expected} />
            <Field label="observed" value={run.failure.observed} />
          </>
        ) : null}
        {run.stop_reason ? <Field label="stopped" value={run.stop_reason} /> : null}
        {actions.length > 0 ? (
          <Field
            label="operator"
            // Captured at the X layer during a handoff. Keystrokes are counted,
            // never recorded: the operator may be typing a credential.
            value={`${actions.length} actions · ${[...new Set(actions.map((a) => a.kind))].join(", ")}`}
          />
        ) : null}
      </div>
    </div>
  );
}

function CapabilityCard({ capability }: { capability: Capability | null }) {
  const cap = capability;
  if (!cap) {
    return (
      <div className="panel">
        <div className="panel-hd">Capability</div>
        <p className="p-3 text-[12px] text-[var(--muted)]">
          Recorded capabilities appear here with the contract an agent calls them by.
        </p>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="panel-hd flex items-center justify-between">
        <span>Capability</span>
        <span className="mono normal-case">{cap.status}</span>
      </div>
      <div className="space-y-1 p-3 text-[12px]">
        <Field label="id" value={`${cap.id}@v${cap.version}`} />
        <Field label="goal" value={cap.goal} />
        <Field
          label="inputs"
          value={cap.inputs.map((i) => `${i.name}: ${i.type}`).join(", ") || "—"}
        />
        <Field
          label="outputs"
          value={cap.outputs.map((o) => `${o.name}: ${o.type}`).join(", ") || "—"}
        />
        <Field
          label="outcomes"
          value={cap.business_outcomes.map((o) => o.name).join(", ") || "none declared"}
        />
      </div>
    </div>
  );
}

/**
 * What synthesis proposed, and what was thrown away.
 *
 * The declaration is the one part of an artifact a model wrote freehand, and the
 * rejections are how a reviewer judges whether to trust the rest: both outcomes
 * the model proposed for this capability were phrases visible on the successful
 * run's own frames.
 */
function SynthesisCard({ evidence }: { evidence: Evidence | null }) {
  const note = evidence?.synthesis as
    | {
        success_text?: string;
        business_outcomes?: { name: string }[];
        business_outcomes_rejected?: { name: string; rejected_because?: string }[];
      }
    | null
    | undefined;
  if (!note) return null;
  const rejected = note.business_outcomes_rejected ?? [];
  return (
    <div className="panel">
      <div className="panel-hd">Synthesis · what the model proposed</div>
      <div className="space-y-1 p-3 text-[12px]">
        <Field label="success" value={note.success_text} />
        <Field
          label="accepted"
          value={(note.business_outcomes ?? []).map((o) => o.name).join(", ") || "none"}
        />
        {rejected.map((o) => (
          <Field key={o.name} label="rejected" value={`${o.name} — ${o.rejected_because ?? ""}`} />
        ))}
      </div>
    </div>
  );
}

/**
 * The guardrails, read-only.
 *
 * Editing policy from a debug console is a hole, not a feature. Seeing it is the
 * point: "what is this agent permitted to do" should not require reading a YAML
 * file inside a container.
 */
function PolicyCard({ policy }: { policy: Policy | null }) {
  if (!policy) return null;
  return (
    <div className="panel">
      <div className="panel-hd">Policy · read-only</div>
      <div className="space-y-1 p-3 text-[12px]">
        <Field label="allowlist" value={policy.allowed_url_patterns.join(" ")} />
        <Field label="actions" value={policy.allowed_actions.join(" ")} />
        <Field label="risky" value={policy.risky_disposition} />
        <Field
          label="recover"
          value={policy.recoveries.map((r) => `${r.name}×${r.max_per_run}`).join(", ") || "—"}
        />
        <Field
          label="escalate"
          value={policy.escalations.map((e) => e.name).join(", ") || "—"}
        />
        <Field label="app error" value={policy.app_errors.map((e) => e.name).join(", ") || "—"} />
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

        <div className="flex flex-wrap gap-2 pt-1">
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

function Steps({
  run,
  selected,
  onSelect,
}: {
  run: Run | null;
  selected: number | null;
  onSelect: (id: number) => void;
}) {
  return (
    <div className="panel min-h-[200px] flex-1">
      <div className="panel-hd">Steps</div>
      {!run || run.steps.length === 0 ? (
        <p className="p-3 text-[12px] text-[var(--muted)]">No steps recorded.</p>
      ) : (
        <table className="mono w-full">
          <tbody>
            {run.steps.map((s, i) => (
              <tr
                key={`${s.step_id}-${i}`}
                onClick={() => onSelect(s.step_id)}
                className="cursor-pointer border-b border-[var(--rule)] last:border-0 hover:bg-[#222a33]"
                style={{ background: s.step_id === selected ? "#222a33" : undefined }}
              >
                <td className="px-2 py-1 align-top text-[var(--muted)]">{s.step_id}</td>
                <td className="px-2 py-1 align-top">
                  <div>{s.intent}</div>
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
      <span className="min-w-0 break-words">{value ?? "—"}</span>
    </div>
  );
}

const COLORS: Record<string, string> = {
  ok: "var(--ok)",
  success: "var(--ok)",
  recovered: "var(--warn)",
  skipped: "var(--muted)",
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
