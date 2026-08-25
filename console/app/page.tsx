"use client";

/**
 * One page, three regions: the navigator (what exists), the run (what you are
 * looking at), and the live session (what is happening now).
 *
 * There is deliberately no separate /operator route — a debug view and an operator
 * view of the same run differ only in whether you may touch it, and splitting them
 * means whoever handles an escalation navigates away from the evidence.
 *
 * Live updates come from the run's own evidence stream (SSE over `steps.jsonl` and
 * `run.json`), so a CLI-started run is watchable here for free and what this shows
 * cannot disagree with the audit trail — it is reading it.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CapabilityCard } from "@/components/Catalog";
import { Frame } from "@/components/Frame";
import { Inspector } from "@/components/Inspector";
import { InterventionPanel } from "@/components/Intervention";
import { Launch } from "@/components/Launch";
import { Navigator } from "@/components/Navigator";
import { NoVncScreen } from "@/components/NoVncScreen";
import { PolicyCard } from "@/components/PolicyCard";
import { RunBar, RunDetails, StepRail } from "@/components/RunView";
import { FaultPanel } from "@/components/Faults";
import { Drawer, Modal } from "@/components/Shell";
import { Chip, Field, Panel } from "@/components/ui";
import {
  NOVNC,
  api,
  runEvents,
  type Capability,
  type CapabilityHistory,
  type CapabilitySummary,
  type Evidence,
  type Health,
  type Intervention,
  type Policy,
  type Run,
  type RunSummary,
  type Thinking,
} from "@/lib/api";

const OPERATOR = "reviewer";
type DrawerKind = "details" | "contracts" | null;

export default function Console() {
  const [health, setHealth] = useState<Health | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [capabilities, setCapabilities] = useState<CapabilitySummary[]>([]);
  const [interventions, setInterventions] = useState<Intervention[]>([]);

  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [policy, setPolicy] = useState<Policy | null>(null);
  const [capability, setCapability] = useState<Capability | null>(null);
  const [history, setHistory] = useState<CapabilityHistory | null>(null);
  const [selectedCapability, setSelectedCapability] = useState<string | null>(null);

  const [thinking, setThinking] = useState<Thinking | null>(null);
  const [stepIndex, setStepIndex] = useState<number | null>(null);
  const [follow, setFollow] = useState(true);
  const [filter, setFilter] = useState("");
  const [hasControl, setHasControl] = useState(false);
  const [drawer, setDrawer] = useState<DrawerKind>(null);
  const [launching, setLaunching] = useState(false);
  const [navOpen, setNavOpen] = useState(false);

  // --- the slow poll: things that change when something else starts a run ----
  //
  // Each result replaces state only when it actually differs. Re-seating three
  // arrays every two seconds re-rendered every panel below them on a new object
  // identity — including re-creating the ~150 absolutely-positioned boxes of the
  // element overlay, which is what made hovering it stutter.
  const poll = useCallback(async () => {
    const [up, listed, queue, catalog] = await Promise.all([
      api.health(),
      api.runs(),
      api.interventions(true),
      api.capabilities(),
    ]);
    setOnline(up !== null);
    setHealth((was) => (same(was, up) ? was : up));
    if (listed) setRuns((was) => (same(was, listed) ? was : listed));
    if (queue) setInterventions((was) => (same(was, queue) ? was : queue));
    if (catalog) setCapabilities((was) => (same(was, catalog) ? was : catalog));
  }, []);

  useEffect(() => {
    void poll();
    const t = setInterval(() => void poll(), 2000);
    return () => clearInterval(t);
  }, [poll]);

  // A run started anywhere becomes the selected one. The operator's attention
  // should follow the session, not have to chase it.
  const active = health?.active_run ?? null;
  // Anything holding the display, run or not. Arming a fault claims it the same
  // way; the difference is that a run is something this console can open.
  const busy = health?.session_busy ? (active ?? "the session") : null;
  useEffect(() => {
    if (active) setSelectedRun(active);
  }, [active]);

  const current = selectedRun ?? runs[0]?.run_id ?? null;

  // --- deep link: a finding in this console is a URL someone else can open ---
  const started = useRef(false);
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const params = new URLSearchParams(window.location.search);
    const run = params.get("run");
    const step = params.get("step");
    if (run) setSelectedRun(run);
    if (step) {
      setStepIndex(Number(step));
      setFollow(false);
    }
  }, []);

  useEffect(() => {
    if (!current) return;
    const params = new URLSearchParams();
    params.set("run", current);
    if (stepIndex !== null) params.set("step", String(stepIndex));
    window.history.replaceState(null, "", `?${params}`);
  }, [current, stepIndex]);

  // --- the selected run: fetched once, then streamed --------------------------
  const load = useCallback(async (runId: string) => {
    const [detail, files] = await Promise.all([api.run(runId), api.evidence(runId)]);
    setRun(detail);
    setEvidence(files);
  }, []);

  useEffect(() => {
    if (!current) return;
    setStepIndex(null);
    setFollow(true);
    setThinking(null);
    void load(current);
  }, [current, load]);

  useEffect(() => {
    if (!current) return;
    let frames = 0;
    const stop = runEvents(current, {
      thinking: setThinking,
      run: (fresh) => {
        setRun(fresh);
        // A new step means a new frame on disk. The manifest maps step ids to
        // files, so it has to be re-read — but only when the count moved, not on
        // every heartbeat.
        if (fresh.steps.length !== frames) {
          frames = fresh.steps.length;
          void api.evidence(current).then((e) => e && setEvidence(e));
        }
      },
    });
    return stop;
  }, [current]);

  // Follow the live edge unless a step is pinned. A run being watched should show
  // its newest frame; a run being debugged must not move under the reader.
  const steps = run?.steps ?? [];
  useEffect(() => {
    if (follow) setStepIndex(steps.length ? steps.length - 1 : null);
  }, [follow, steps.length]);

  // Nothing rewrites the heartbeat when the model answers, so the step it names
  // arriving is what retires it — which is also the only signal that survives a
  // reload or a reconnect in the middle of a call.
  const pending = useMemo(
    () =>
      thinking && run?.status === "running" && !steps.some((s) => s.step_id === thinking.step_id)
        ? thinking
        : null,
    [thinking, run?.status, steps],
  );

  const step = stepIndex !== null ? (steps[stepIndex] ?? null) : null;
  // Only when the manifest in hand is the selected run's. Switching runs replaces
  // `current` before the new evidence lands, and pairing the new run id with the
  // old run's file paths asks the control plane for a frame that does not exist.
  const frame = useMemo(
    () =>
      (evidence?.run_id === current
        ? evidence.steps.find((s) => s.step_id === step?.step_id)
        : null) ?? null,
    [evidence, current, step?.step_id],
  );

  // --- the two contracts beside the run --------------------------------------
  useEffect(() => {
    // The selected run's application, not the deployment default. Showing one
    // app's allowlist beside another app's run is a confident wrong answer.
    void api.policy(run?.app ?? null).then(setPolicy);
  }, [run?.app]);

  const capabilityRef = selectedCapability ?? run?.capability ?? run?.capability_ref ?? null;

  useEffect(() => {
    const inEvidence = evidence?.capability ?? null;
    if (!selectedCapability && inEvidence) {
      setCapability(inEvidence);
      return;
    }
    if (!capabilityRef) {
      setCapability(null);
      return;
    }
    void api.capability(capabilityRef).then(setCapability);
  }, [evidence, capabilityRef, selectedCapability]);

  useEffect(() => {
    const id = capabilityRef?.split("@v")[0];
    if (!id) {
      setHistory(null);
      return;
    }
    void api.history(id).then(setHistory);
  }, [capabilityRef, runs.length]);

  // --- control ---------------------------------------------------------------
  const open =
    interventions.find((i) => i.state === "pending" || i.state === "human_control") ?? null;
  useEffect(() => {
    setHasControl(open?.state === "human_control");
  }, [open?.state, open?.id]);

  return (
    <div className="flex h-screen flex-col overflow-hidden">
      <header className="flex shrink-0 items-center gap-2 border-b border-[var(--rule)] px-3 py-2">
        <button
          className="btn lg:hidden"
          onClick={() => setNavOpen((n) => !n)}
          title="runs and capabilities"
        >
          ☰
        </button>
        <span className="text-[13px] font-semibold tracking-wide whitespace-nowrap">
          AUTOMATION CONSOLE
        </span>
        <span className="mono hidden text-[11px] text-[var(--muted)] sm:inline">
          {online === null ? "…" : online ? "up" : "unreachable"}
        </span>
        {active ? (
          <span className="mono truncate text-[11px]" style={{ color: "var(--warn)" }}>
            ◌ {active}
          </span>
        ) : null}

        <button className="btn btn-accent ml-auto shrink-0" onClick={() => setLaunching(true)}>
          + New run
        </button>
        {open ? (
          <button
            className="btn shrink-0"
            style={{ borderColor: "var(--warn)", color: "var(--warn)" }}
            onClick={() => setSelectedRun(open.run_id)}
            title={open.message}
          >
            intervention · {open.reason}
          </button>
        ) : null}
      </header>

      <div className="relative flex min-h-0 flex-1">
        {/* --- navigator ---------------------------------------------------
            A rail at desktop widths, an overlay below them. It is navigation,
            not content: the run is what the window is for. */}
        <aside
          className={`${
            navOpen ? "absolute inset-y-0 left-0 z-10 flex bg-[var(--bg)] shadow-xl" : "hidden"
          } w-[250px] shrink-0 flex-col border-r border-[var(--rule)] lg:relative lg:flex`}
        >
          <Navigator
            run={run}
            pending={pending}
            step={stepIndex}
            onSelectStep={(index) => {
              setStepIndex(index);
              setFollow(false);
            }}
            runs={runs}
            capabilities={capabilities}
            selectedRun={current}
            selectedCapability={selectedCapability}
            filter={filter}
            operator={OPERATOR}
            onSelectRun={(id) => {
              setSelectedRun(id);
              setSelectedCapability(null);
              setNavOpen(false);
            }}
            onSelectCapability={(ref) => {
              setSelectedCapability(ref);
              setDrawer("contracts");
            }}
            onFilter={setFilter}
            onChanged={poll}
            onReplay={(ref) => {
              setSelectedCapability(ref);
              setLaunching(true);
            }}
          />
        </aside>

        {/* --- the run ------------------------------------------------------ */}
        <main className="flex min-w-0 flex-1 flex-col">
          <RunBar
            run={run}
            evidence={evidence}
            onOpenDetails={() => setDrawer("details")}
            onOpenContracts={() => setDrawer("contracts")}
          />
          <StepRail
            run={run}
            selected={stepIndex}
            follow={follow}
            onFollow={setFollow}
            onSelect={(index) => {
              setStepIndex(index);
              setFollow(false);
            }}
          />
          <Frame runId={current} frame={frame} step={step} failure={run?.failure} />
          <Inspector step={step} />
        </main>

        {/* --- the session -------------------------------------------------- */}
        <aside className="hidden w-[300px] shrink-0 flex-col border-l border-[var(--rule)] md:flex xl:w-[340px]">
          <div className="flex shrink-0 items-center justify-between border-b border-[var(--rule)] px-3 py-1.5">
            <span className="mono text-[11px] tracking-wider text-[var(--muted)] uppercase">
              Live session
            </span>
            <span
              className="mono text-[11px]"
              style={{ color: hasControl ? "var(--warn)" : undefined }}
            >
              {hasControl ? "you" : "automation"}
            </span>
          </div>
          <div className="h-[220px] shrink-0 xl:h-[260px]">
            <NoVncScreen url={open?.vnc_url ?? NOVNC} viewOnly={!hasControl} />
          </div>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto">
            <InterventionPanel
              queue={interventions}
              operator={OPERATOR}
              activeRunId={current}
              evidence={evidence}
              onSelectRun={setSelectedRun}
              onChanged={() => {
                void poll();
                if (current) void load(current);
              }}
            />
            <div className="px-3 pb-3">
              <FaultPanel app={run?.app ?? null} busy={busy} />
            </div>
          </div>
        </aside>
      </div>

      {/* --- consulted, not watched ----------------------------------------- */}
      <Drawer
        open={drawer === "details"}
        title={`Run · ${current ?? ""}`}
        onClose={() => setDrawer(null)}
      >
        <RunDetails run={run} evidence={evidence} />
      </Drawer>

      <Drawer
        open={drawer === "contracts"}
        title="Contracts in force"
        onClose={() => setDrawer(null)}
      >
        <CapabilityCard capability={capability} history={history} />
        <SynthesisCard evidence={evidence} />
        <PolicyCard policy={policy} />
      </Drawer>

      <Modal open={launching} title="Start a run" onClose={() => setLaunching(false)}>
        <Launch
          capabilities={capabilities}
          apps={health?.apps ?? []}
          defaultApp={health?.default_app ?? "targetapp"}
          busyWith={active}
          selected={selectedCapability}
          onStarted={(id) => {
            setSelectedRun(id);
            setFollow(true);
            setLaunching(false);
            void poll();
          }}
          onSelectCapability={setSelectedCapability}
        />
      </Modal>
    </div>
  );
}

/** Cheap identity check, so a poll that found nothing new re-renders nothing. */
function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * What synthesis proposed, and what was thrown away.
 *
 * The declaration is the one part of an artifact a model wrote freehand, and the
 * rejections are how a reviewer judges whether to trust the rest: on the shipped
 * recording both outcomes the model proposed were phrases visible on the
 * successful run's own frames, and code caught both.
 */
function SynthesisCard({ evidence }: { evidence: Evidence | null }) {
  const note = evidence?.synthesis as
    | {
        success_text?: string;
        capability_id?: string;
        capability_id_rejected?: { proposed?: string; because?: string };
        business_outcomes?: { name: string }[];
        business_outcomes_rejected?: { name: string; rejected_because?: string }[];
      }
    | null
    | undefined;
  if (!note) return null;
  const rejected = note.business_outcomes_rejected ?? [];
  return (
    <Panel title="Synthesis · what the model proposed" bodyClassName="space-y-1 p-3 text-[12px]">
      <Field label="name" value={note.capability_id} mono />
      {note.capability_id_rejected ? (
        <div className="flex gap-2">
          <Chip tone="rejected">rejected</Chip>
          <span className="min-w-0 break-words">
            {note.capability_id_rejected.proposed || "(nothing)"} —{" "}
            {note.capability_id_rejected.because}
          </span>
        </div>
      ) : null}
      <Field label="success" value={note.success_text} />
      <Field
        label="accepted"
        value={(note.business_outcomes ?? []).map((o) => o.name).join(", ") || "none"}
      />
      {rejected.map((o) => (
        <div key={o.name} className="flex gap-2">
          <Chip tone="rejected">rejected</Chip>
          <span className="min-w-0 break-words">
            {o.name} — {o.rejected_because ?? ""}
          </span>
        </div>
      ))}
    </Panel>
  );
}
