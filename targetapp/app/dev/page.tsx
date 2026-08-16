/**
 * Fault panel.
 *
 * Not part of the simulated bank — this is the control surface that makes §6.3
 * evidence producible on demand. It is also outside the agent's allowlist by
 * construction: the policy permits the app's routes, and a reviewer toggling
 * faults here is doing something the automation cannot do to itself.
 */

import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { resetLedger } from "@/lib/data";
import { FAULTS, type FaultName, activeFaults, setFaults } from "@/lib/faults";
import { currentTeller } from "@/lib/session";

export default async function Dev() {
  const teller = (await currentTeller()) ?? "not signed in";
  const active = await activeFaults();

  async function apply(formData: FormData) {
    "use server";
    const names = Object.keys(FAULTS).filter((k) => formData.get(k) === "on") as FaultName[];
    await setFaults(names);
    redirect("/dev");
  }

  async function reset() {
    "use server";
    await setFaults([]);
    resetLedger();
    redirect("/dev");
  }

  return (
    <Shell teller={teller} banner={false} modal={false}>
      <div className="panel max-w-[760px]">
        <div className="panel-hd">Fault Injection — Test Harness</div>
        <div className="p-3">
          <p className="mb-3 text-[11px] text-[#5d6b7a]">
            These simulate runtime conditions, not business outcomes. Member not found,
            no savings account, insufficient funds, over the daily limit and restricted
            members are ordinary behaviour of the app and need no toggle — use member IDs
            99999, 30992, 44100 and amounts above $5,000.
          </p>

          <form action={apply}>
            <table className="grid mb-3">
              <thead>
                <tr>
                  <th className="w-[40px]">On</th>
                  <th className="w-[120px]">Fault</th>
                  <th>Effect</th>
                  <th className="w-[150px]">Class</th>
                </tr>
              </thead>
              <tbody>
                {(Object.keys(FAULTS) as FaultName[]).map((name) => (
                  <tr key={name}>
                    <td>
                      <input type="checkbox" name={name} defaultChecked={active.has(name)} />
                    </td>
                    <td className="font-mono">{name}</td>
                    <td>{FAULTS[name]}</td>
                    <td className="text-[#5d6b7a]">{CLASSES[name]}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <button type="submit" className="btn-primary">
              Apply
            </button>
          </form>

          <form action={reset} className="mt-2">
            <button type="submit" className="btn">
              Clear faults &amp; reset balances
            </button>
          </form>

          <p className="mt-4 border-t border-[var(--rule)] pt-2 text-[11px] text-[#5d6b7a]">
            Active: {active.size ? [...active].join(", ") : "none"}
          </p>
        </div>
      </div>
    </Shell>
  );
}

/** What the replay engine is expected to do with each, per the taxonomy. */
const CLASSES: Record<FaultName, string> = {
  banner: "variance — handled",
  modal: "recoverable (declared)",
  slow: "recoverable (wait)",
  expired: "escalate",
  denied: "business outcome",
  error500: "hard failure",
  validation: "hard failure",
  confirm: "hard failure (undeclared)",
};
