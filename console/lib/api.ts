/**
 * Client for the automation control plane.
 *
 * Every call degrades rather than throws. The console is a debugging surface: it
 * is most useful precisely when the backend is unhealthy, so a fetch failure has
 * to render as "backend unreachable" and not as a blank page.
 */

export const API = process.env.NEXT_PUBLIC_CUA_API ?? "http://localhost:8000";
export const NOVNC = process.env.NEXT_PUBLIC_NOVNC_URL ?? "http://localhost:6080";

export type RunStatus = "success" | "business_outcome" | "escalated" | "failure" | "running";

export interface StepRow {
  step_id: number;
  intent: string;
  status: string;
  resolution: string;
  duration_ms: number;
  expected?: string | null;
  observed?: string | null;
}

export interface Run {
  run_id: string;
  mode: "discovery" | "replay";
  capability?: string;
  goal?: string;
  status: RunStatus;
  steps: StepRow[];
  intervention_id?: string | null;
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

export const api = {
  health: () => call<{ status: string }>("/health"),
  runs: () => call<Run[]>("/runs"),
  run: (id: string) => call<Run>(`/runs/${id}`),
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
