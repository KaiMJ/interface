/**
 * Programmatic fault control, so an evidence run can be scripted end to end: set the
 * fault, invoke the capability, assert the ReplayResult.
 *
 * The `?set=` form exists because faults live in a *cookie*, so arming one means arming
 * it inside the automation's browser — which curl cannot reach but one navigation can.
 * This route and /dev are both excluded from the agent's allowlist.
 */

import { NextResponse } from "next/server";
import { FAULTS, type FaultName, activeFaults, setFaults } from "@/lib/faults";

export async function GET(req: Request) {
  const requested = new URL(req.url).searchParams.get("set");
  if (requested !== null) {
    const names = requested
      .split(",")
      .map((s) => s.trim())
      .filter((s): s is FaultName => s in FAULTS);
    await setFaults(names);
    // Built from the Host header, not from `req.url`: inside the container the latter
    // reports the bind address (0.0.0.0), which does not resolve for the browser.
    const host = req.headers.get("host") ?? new URL(req.url).host;
    return NextResponse.redirect(`http://${host}/`);
  }
  return NextResponse.json({
    active: [...(await activeFaults())],
    available: FAULTS,
  });
}

export async function POST(req: Request) {
  const body = (await req.json().catch(() => ({}))) as { faults?: string[] };
  const names = (body.faults ?? []).filter((f): f is FaultName => f in FAULTS);
  await setFaults(names);
  return NextResponse.json({ active: names });
}
