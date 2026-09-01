/**
 * Injectable fault states — why this app exists rather than a public sandbox, which
 * cannot be made to return "session expired" on command.
 *
 * Toggled at /dev or by POST /api/faults. State lives in a cookie, so the automation's
 * session and a reviewer's own tab do not fight over it.
 *
 * These are FAULTS. The business outcomes — member not found, insufficient funds, over
 * the daily limit, restricted member — are ordinary behaviour reachable with ordinary
 * inputs, and are deliberately not in here.
 */

import { cookies } from "next/headers";

export const FAULTS = {
  banner: "Renders a site-wide notice bar that pushes all content down",
  modal: "Shows an unexpected maintenance dialog over the page",
  slow: "Adds a 4s delay to member lookups",
  expired: "Next request bounces to login with 'session expired'",
  denied: "Member detail returns a permission denial",
  error500: "Member detail returns an application error",
  validation: "Transfer form rejects with an inline error, shifting fields down",
  confirm: "Transfer gains an extra 'Are you sure?' interstitial",
} as const;

export type FaultName = keyof typeof FAULTS;

const COOKIE = "faults";

export async function activeFaults(): Promise<Set<FaultName>> {
  const jar = await cookies();
  const raw = jar.get(COOKIE)?.value ?? "";
  return new Set(
    raw
      .split(",")
      .map((s) => s.trim())
      .filter((s): s is FaultName => s in FAULTS),
  );
}

export async function hasFault(name: FaultName): Promise<boolean> {
  return (await activeFaults()).has(name);
}

export async function setFaults(names: FaultName[]): Promise<void> {
  const jar = await cookies();
  jar.set(COOKIE, names.join(","), { path: "/", httpOnly: false, sameSite: "lax" });
}

export function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
