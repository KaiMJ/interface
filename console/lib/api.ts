/** Client for the automation control plane. */

export const API = process.env.NEXT_PUBLIC_CUA_API ?? "http://localhost:8000";
export const NOVNC = process.env.NEXT_PUBLIC_NOVNC_URL ?? "http://localhost:6080";

export type RunStatus =
  | "success"
  | "business_outcome"
  | "escalated"
  | "failure"
  | "running";

export interface PolicyDecision {
  action: string;
  declared_risk: string;
  effective_risk: string;
  disposition: string;
  rule?: string | null;
  detail?: string | null;
  promoted_from?: string | null;
  intent: string;
}

export interface TierAttempt {
  tier: string;
  outcome: "matched" | "miss" | "skipped" | "error";
  candidates: number;
  matched_text?: string | null;
  detail?: string | null;
}

export interface ResolutionTrace {
  target_desc: string;
  anchor_text?: string | null;
  relation: string;
  attempts: TierAttempt[];
  tier: string;
  candidates: number;
  drift: boolean;
  bbox?: Bbox | null;
  point?: [number, number] | null;
}

export interface ModelTurn {
  call: string;
  text?: string;
  reasoning?: string;
  prompt?: string;
  arguments?: Record<string, unknown>;
  intent: string;
  expect?: string | null;
  mark?: number | null;
  element_id?: string | null;
  element_label?: string | null;
  expect_recorded?: string | null;
  anchor_proposed?: string | null;
  anchor_recorded?: string | null;
  candidates_marked: number;
  candidates_listed: number;
  latency_ms: number;
  verdict: string;
  detail?: string | null;
}

export interface Phases {
  observe_ms: number;
  observations: number;
  resolve_ms: number;
  act_ms: number;
  verify_ms: number;
}

export interface StepRow {
  step_id: number;
  intent: string;
  status: string;
  resolution: string;
  settled_by?: string;
  duration_ms: number;
  attempts?: number;
  expected?: string | null;
  observed?: string | null;
  recovery_applied?: string | null;
  note?: string | null;
  policy?: PolicyDecision | null;
  resolution_trace?: ResolutionTrace | null;
  phases?: Phases | null;
  model_turn?: ModelTurn | null;
}

export interface RunSummary {
  run_id: string;
  kind: "discovery" | "replay";
  status: RunStatus;
  app?: string | null;
  capability?: string | null;
  goal?: string | null;
  steps?: number;
  duration_ms?: number;
  started_at?: string;
  finished_at?: string;
}

export interface Bbox {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Run {
  run_id: string;
  status: RunStatus;
  app?: string | null;
  capability?: string | null;
  goal?: string | null;
  steps: StepRow[];
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown>;
  outcome?: { name: string; step_id?: number | null; fields?: Record<string, unknown> } | null;
  failure?: {
    kind: string;
    step_id?: number | null;
    message: string;
    expected?: string | null;
    observed?: string | null;
  region?: Bbox | null;
  } | null;
  intervention_id?: string | null;
  duration_ms?: number;
  started_at?: string;
  finished_at?: string;
  stop_reason?: string;
  capability_ref?: string | null;
  model?: string;
  llm_calls?: number;
  steps_taken?: number;
  evidence_dir?: string;
}

export interface EvidenceStep {
  step_id: number;
  frame: string;
  after: string | null;
  annotated: string | null;
  observation: string | null;
}

export interface HumanActionRow {
  at: string;
  kind: string;
  x?: number | null;
  y?: number | null;
  detail?: string | null;
}

export interface Evidence {
  run_id: string;
  steps: EvidenceStep[];
  intervention:
    | {
        handoff?: string;
        handback?: string;
        request?: Intervention | null;
        resolution?: {
          outcome: string;
          operator: string;
          note: string;
          resolved_at: string;
          human_actions: HumanActionRow[];
        } | null;
      }
    | null;
  human_actions: HumanActionRow[];
  capability: Capability | null;
  synthesis: Record<string, unknown> | null;
}

export interface Observation {
  screenshot_path: string;
  viewport: { width: number; height: number };
  elements: {
    id: string;
    role?: string | null;
    name?: string | null;
    text?: string | null;
    bbox: Bbox;
    source: string;
    conf: number;
  }[];
  url?: string | null;
  frame_hash?: string | null;
  settled_by?: string;
  taken_at: string;
}

export interface Capability {
  id: string;
  version: number;
  status: string;
  goal: string;
  description?: string;
  app?: { name: string; vendor?: string | null; base_url_pattern?: string };
  inputs: {
    name: string;
    type: string;
    required: boolean;
    example?: string | null;
    description?: string;
  }[];
  outputs: { name: string; type: string; from_step: number; description?: string }[];
  business_outcomes: { name: string; description?: string; detector: { value?: string | null } }[];
  steps: { id: number; kind: string; action?: string; risk?: string; note?: string | null }[];
  success?: { kind: string; value?: string | null };
  recording?: { run_id: string; model: string; recorded_at: string; step_count: number };
}

export interface CapabilitySummary {
  ref: string;
  id: string;
  app: string;
  version: number;
  status: string;
  goal: string;
  description?: string;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
  outcomes: string[];
  steps: number;
}

/** `/capabilities/{id}/history` — every run of one flow, plus what they say together. */
export interface CapabilityHistory {
  capability_id: string;
  versions: number[];
  runs: RunSummary[];
  aggregate: {
    total: number;
    statuses: Record<string, number>;
    resolution_tiers: Record<string, number>;
    settled_by: Record<string, number>;
    success_rate: number | null;
    median_duration_ms: number | null;
    drift_share: number | null;
  };
}

export interface Policy {
  app: string;
  /** Every application with a policy file. One file per app; `--app` selects one. */
  apps?: string[];
  vendor?: string | null;
  base_url_pattern?: string;
  allowed_url_patterns: string[];
  allowed_actions: string[];
  risky_disposition: string;
  risky_intent_patterns: string[];
  recoveries: { name: string; detector: string; max_per_run: number }[];
  app_errors: { name: string; detector: string }[];
  escalations: { name: string; detector: string }[];
  redaction: Record<string, unknown>;
}

export interface Intervention {
  id: string;
  run_id: string;
  mode: string;
  capability?: string | null;
  goal: string;
  reason: string;
  failure_kind?: string | null;
  step_id?: number | null;
  step_intent: string;
  message: string;
  expected?: string | null;
  observed?: string | null;
  state: "pending" | "human_control" | "resolved" | "aborted" | "expired";
  vnc_url?: string | null;
  raised_at?: string;
}

export interface Failed {
  error: string;
}

export function isFailed(value: unknown): value is Failed {
  return typeof value === "object" && value !== null && "error" in value;
}

async function call<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const res = await fetch(`${API}${path}`, { cache: "no-store", ...init });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

/**
 * A call the operator made on purpose. Failures come back rather than degrading:
 * a 409 for a session already held, a 404 for an unknown capability.
 */
async function send<T>(path: string, init?: RequestInit): Promise<T | Failed> {
  try {
    const res = await fetch(`${API}${path}`, { cache: "no-store", ...init });
    const body = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = body?.detail;
      return { error: typeof detail === "string" ? detail : `${res.status} ${res.statusText}` };
    }
    return body as T;
  } catch (e) {
    return { error: `control plane unreachable: ${(e as Error).message}` };
  }
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify(body),
});

/** Where a frame lives. Served by the control plane, not by this origin. */
export function evidenceUrl(runId: string, path: string): string {
  return `${API}/runs/${runId}/evidence/${path}`;
}

export interface Health {
  status: string;
  /** Which run holds the display, if any. One session, one run. */
  active_run: string | null;
  /** Whether *anything* holds it — arming a fault does too, and is not a run. */
  session_busy?: boolean;
  default_app: string;
  apps: string[];
}

export interface Faults {
  /** name -> what it does. Empty when the app declares no harness. */
  available: Record<string, string>;
  armed: string[];
  url: string | null;
}

export const api = {
  health: () => call<Health>("/health"),

  runs: (query: Record<string, string> = {}) => {
    const qs = new URLSearchParams(query).toString();
    return call<RunSummary[]>(`/runs${qs ? `?${qs}` : ""}`);
  },
  run: (id: string) => call<Run>(`/runs/${id}`),
  evidence: (id: string) => call<Evidence>(`/runs/${id}/evidence`),
  observation: (id: string, path: string) => call<Observation>(`/runs/${id}/evidence/${path}`),

  /** The prompt box. Returns as soon as the run has an id, not when it ends. */
  discover: (body: {
    goal: string;
    inputs: Record<string, unknown>;
    app?: string | null;
    capability_id?: string | null;
  }) => send<{ run_id: string }>("/runs/discover", json(body)),

  /** The re-run button. Same engine as an agent's invoke, started in background. */
  replay: (body: { capability_id: string; inputs: Record<string, unknown>; version?: number }) =>
    send<{ run_id: string }>("/runs/replay", json(body)),

  policy: (app?: string | null) => call<Policy>(`/policy${app ? `?app=${app}` : ""}`),
  capabilities: () => call<CapabilitySummary[]>("/capabilities"),
  capability: (ref: string) => {
    // "cap_x@v2" is how a run names the contract it executed; the catalog is
    // addressed by id and version.
    const [id, version] = ref.split("@v");
    return call<Capability>(`/capabilities/${id}${version ? `?version=${version}` : ""}`);
  },
  history: (id: string) => call<CapabilityHistory>(`/capabilities/${id}/history`),
  approve: (id: string, version: number, operator: string) =>
    send<{ ref: string; status: string }>(
      `/capabilities/${id}/approve?version=${version}&operator=${encodeURIComponent(operator)}`,
      { method: "POST" },
    ),

  faults: (app?: string | null) => call<Faults>(`/session/faults${app ? `?app=${app}` : ""}`),
  armFaults: (names: string[]) =>
    send<{ armed: string[] }>("/session/faults", json({ names })),

  interventions: (includeResolved = false) =>
    call<Intervention[]>(`/interventions${includeResolved ? "?include_resolved=true" : ""}`),
  take: (id: string, operator: string) =>
    send<{ holder: string; capturing: boolean; capture_note: string | null }>(
      `/interventions/${id}/take?operator=${encodeURIComponent(operator)}`,
      { method: "POST" },
    ),
  resolve: (id: string, outcome: "resume" | "abort", operator: string, note = "") =>
    send<{ outcome: string; human_actions: number }>(
      `/interventions/${id}/resolve`,
      json({ outcome, operator, note }),
    ),
};

/**
 * The run is waiting on the model for this step. Not evidence — it describes work
 * that has not happened yet — and the step whose id it carries retires it.
 */
export interface Thinking {
  step_id: number;
  since: string;
  model: string;
}

/**
 * Tail a run's evidence as it is written.
 *
 * The stream reads the run's own `steps.jsonl` and `run.json` server-side, so it
 * works for a CLI-started run and cannot diverge from the audit trail. Polling
 * stays as the fallback: EventSource does not report why it failed, and a proxy
 * that buffers SSE would otherwise leave the console blank.
 */
export function runEvents(
  runId: string,
  on: {
    step?: (row: StepRow) => void;
    run?: (run: Run) => void;
    thinking?: (thinking: Thinking) => void;
    error?: () => void;
  },
): () => void {
  let source: EventSource | null = null;
  try {
    source = new EventSource(`${API}/runs/${runId}/events`);
  } catch {
    on.error?.();
    return () => {};
  }
  const parse = <T,>(handler?: (value: T) => void) =>
    (event: MessageEvent) => {
      if (!handler) return;
      try {
        handler(JSON.parse(event.data) as T);
      } catch {
        /* a half-written line; the next event carries the whole thing */
      }
    };
  source.addEventListener("step", parse<StepRow>(on.step));
  source.addEventListener("run", parse<Run>(on.run));
  source.addEventListener("thinking", parse<Thinking>(on.thinking));
  source.addEventListener("error", () => on.error?.());
  return () => source?.close();
}
