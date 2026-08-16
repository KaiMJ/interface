/** Dismisses the maintenance modal and returns to wherever the caller was.
 *  The declared recovery in policies/targetapp.yaml drives this control. */

import { NextResponse } from "next/server";
import { type FaultName, activeFaults, setFaults } from "@/lib/faults";

export async function POST(req: Request) {
  const active = await activeFaults();
  active.delete("modal");
  await setFaults([...active] as FaultName[]);
  const back = req.headers.get("referer") ?? "/members";
  return NextResponse.redirect(back, { status: 303 });
}
