/**
 * Programmatic fault control, for the test harness.
 *
 *   GET  /api/faults              -> { active, available }
 *   POST /api/faults {faults:[…]} -> sets exactly that set
 *
 * Exists so an evidence run can be scripted end to end — set the fault, invoke
 * the capability, assert the ReplayResult — rather than depending on a human
 * clicking a checkbox at the right moment.
 */

import { NextResponse } from "next/server";
import { FAULTS, type FaultName, activeFaults, setFaults } from "@/lib/faults";

export async function GET() {
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
