/**
 * Injectable fault states.
 *
 * The reason this app exists rather than pointing the demo at a public sandbox:
 * §3.3 asks the replay engine to detect and respond to runtime conditions —
 * validation errors, permission denials, unexpected dialogs, session expiry,
 * transient slowness, app errors — and §6.3 asks for evidence of one. You cannot
 * make a public demo site return "session expired" on command.
 *
 * Toggled at /dev, or by POST /api/faults for a test harness. State lives in a
 * cookie so it is per-browser-session and survives navigation, which means the
 * automation's session and a reviewer's own tab do not fight over it.
 *
 * Note the deliberate split: these are FAULTS. The business outcomes — member not
 * found, no savings account, insufficient funds, over the daily limit, restricted
 * member — are NOT here. They are ordinary behaviour of the app, reachable with
 * ordinary inputs, because conflating "a legitimate answer" with "an injected
 * failure" is exactly the mistake the system is supposed to avoid.
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
