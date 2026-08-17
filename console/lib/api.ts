/**
 * Client for the automation control plane.
 *
 * Every call degrades rather than throws. The console is a debugging surface: it
 * is most useful precisely when the backend is unhealthy, so a fetch failure has
 * to render as "backend unreachable" and not as a blank page.
 *
 * Types mirror the API's own shapes rather than a convenient subset. Where they
 * drifted apart before — a run summary that did not carry steps, a `mode` field
 * the API calls `kind` — the console rendered empty panels and said nothing about
 * why.
 */

export const API = process.env.NEXT_PUBLIC_CUA_API ?? "http://localhost:8000";
export const NOVNC = process.env.NEXT_PUBLIC_NOVNC_URL ?? "http://localhost:6080";

export type RunStatus =
  | "success"
  | "business_outcome"
  | "escalated"
  | "failure"
  | "running";

export interface StepRow {
  step_id: number;
  intent: string;
  status: string;
  resolution: string;
  duration_ms: number;
  expected?: string | null;
  observed?: string | null;
}

/** What `/runs` lists. Enough to choose one; not enough to render it. */
export interface RunSummary {
  run_id: string;
  kind: "discovery" | "replay";
  status: RunStatus;
  capability?: string | null;
  started_at?: string;
  finished_at?: string;
}

/** What `/runs/{id}` returns: a ReplayResult or a DiscoveryResult. */
export interface Run {
  run_id: string;
  status: RunStatus;
  capability?: string | null;
  goal?: string | null;
  steps: StepRow[];
  outputs?: Record<string, unknown>;
  outcome?: { name: string; step_id?: number | null; fields?: Record<string, unknown> } | null;
  failure?: {
    kind: string;
    step_id?: number | null;
    message: string;
    expected?: string | null;
    observed?: string | null;
  } | null;
  intervention_id?: string | null;
  duration_ms?: number;
  stop_reason?: string;
  capability_ref?: string | null;
}

export interface EvidenceStep {
  step_id: number;
  frame: string;
  /** The numbered overlay the model was shown. Absent on replay runs. */
  annotated: string | null;
  observation: string | null;
}

export interface Evidence {
  run_id: string;
  steps: EvidenceStep[];
  intervention: { handoff?: string; handback?: string } | null;
  human_actions: { at: string; kind: string; x?: number | null; y?: number | null; detail?: string | null }[];
  capability: Capability | null;
  synthesis: Record<string, unknown> | null;
}

export interface Capability {
  id: string;
  version: number;
  status: string;
  goal: string;
  description?: string;
  inputs: { name: string; type: string; required: boolean; example?: string | null }[];
  outputs: { name: string; type: string; from_step: number }[];
  business_outcomes: { name: string; description?: string; detector: { value?: string | null } }[];
  steps: unknown[];
}

export interface CapabilitySummary {
  ref: string;
  id: string;
  version: number;
  status: string;
  goal: string;
  inputs: Record<string, string>;
  outputs: Record<string, string>;
  outcomes: string[];
  steps: number;
}

export interface Policy {
  app: string;
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
  step_id?: number | null;
  step_intent: string;
  message: string;
  expected?: string | null;
  observed?: string | null;
  state: "pending" | "human_control" | "resolved" | "aborted" | "expired";
  vnc_url?: string | null;
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

/** Where a frame lives. Served by the control plane, not by this origin. */
export function evidenceUrl(runId: string, path: string): string {
  return `${API}/runs/${runId}/evidence/${path}`;
}

export const api = {
  health: () => call<{ status: string }>("/health"),
  runs: () => call<RunSummary[]>("/runs"),
  run: (id: string) => call<Run>(`/runs/${id}`),
  evidence: (id: string) => call<Evidence>(`/runs/${id}/evidence`),
  policy: () => call<Policy>("/policy"),
  capabilities: () => call<CapabilitySummary[]>("/capabilities"),
  capability: (ref: string) => {
    // "cap_x@v2" is how a run names the contract it executed; the catalog is
    // addressed by id and version.
    const [id, version] = ref.split("@v");
    return call<Capability>(`/capabilities/${id}${version ? `?version=${version}` : ""}`);
  },
  interventions: () => call<Intervention[]>("/interventions"),
  take: (id: string, operator: string) =>
    call<Intervention>(`/interventions/${id}/take?operator=${encodeURIComponent(operator)}`, {
      method: "POST",
    }),
  resolve: (id: string, outcome: "resume" | "abort", operator: string, note = "") =>
    call<Intervention>(`/interventions/${id}/resolve`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ outcome, operator, note }),
    }),
};
