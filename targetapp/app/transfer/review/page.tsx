/**
 * Transfer funds — step 2 of 3. The risky action lives here.
 *
 * "Confirm Transfer" is the one control in this app that moves money and cannot
 * be taken back. Under `risky_disposition: confirm` in the policy, the automation
 * must escalate to a human before pressing it — which is what makes the
 * escalation demo a real guardrail firing rather than a contrived failure.
 *
 * Both business outcomes are decided here, and both render on this page with a
 * 200: insufficient funds, and over the daily limit. Neither is a failure. A
 * caller needs to distinguish "the transfer did not happen because the member is
 * short" from "the transfer may or may not have happened, we lost the thread".
 */

import Link from "next/link";
import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import {
  DAILY_TRANSFER_LIMIT,
  applyTransfer,
  availableOf,
  findAccount,
  findMember,
  money,
} from "@/lib/data";
import { activeFaults } from "@/lib/faults";
import { requireTeller } from "@/lib/session";

export default async function Review({
  searchParams,
}: {
  searchParams: Promise<{ member?: string; from?: string; to?: string; amount?: string; ack?: string }>;
}) {
  const teller = await requireTeller();
  const faults = await activeFaults();
  const { member: memberId = "", from = "", to = "", amount = "0", ack } = await searchParams;

  const member = findMember(memberId);
  const src = findAccount(from);
  const dst = findAccount(to);
  const amt = Number(amount);

  if (!member || !src || !dst || !Number.isFinite(amt)) redirect("/transfer?err=accounts");

  const available = availableOf(from);
  const overLimit = amt > DAILY_TRANSFER_LIMIT;
  const insufficient = amt > available;

  // The extra interstitial. Injected, undeclared, and sitting between the review
  // page and the confirmation — a step recorded without it will land on the wrong
  // screen and the checkpoint has to catch that.
  if (faults.has("confirm") && !ack) {
    return (
      <Shell teller={teller} banner={faults.has("banner")} modal={faults.has("modal")}>
        <div className="panel max-w-[520px]">
          <div className="panel-hd">Additional Verification Required</div>
          <div className="p-4">
            <p className="mb-4">
              This member has been flagged for secondary review. Confirm that you have
              verified the member&apos;s identity before continuing.
            </p>
            <Link
              href={`/transfer/review?member=${memberId}&from=${from}&to=${to}&amount=${amt}&ack=1`}
              className="btn-primary"
            >
              I Have Verified
            </Link>
            <Link href="/transfer" className="btn ml-2">
              Cancel
            </Link>
          </div>
        </div>
      </Shell>
    );
  }

  // The transfer's parameters travel in the form rather than in this action's
  // closure. Closing over the page's variables is the shorter spelling and it
  // fails at run time under Next 16 with `ReferenceError: memberId is not
  // defined` — the closure is not carried across the server-action boundary. It
  // surfaces only when the button is actually pressed, which is exactly the kind
  // of runtime error a replay has to survive rather than assume away.
  async function confirm(formData: FormData) {
    "use server";
    const m = String(formData.get("member") ?? "");
    const src = String(formData.get("from") ?? "");
    const dst = String(formData.get("to") ?? "");
    const value = Number(formData.get("amount") ?? 0);

    // Re-checked server-side. A guard that only exists in the render path is not
    // a guard.
    if (value > DAILY_TRANSFER_LIMIT || value > availableOf(src)) {
      redirect(`/transfer/review?member=${m}&from=${src}&to=${dst}&amount=${value}&ack=1`);
    }
    applyTransfer(src, dst, value);
    const ref = `TRF-${Date.now().toString(36).toUpperCase().slice(-8)}`;
    redirect(`/transfer/receipt?ref=${ref}&member=${m}&from=${src}&to=${dst}&amount=${value}`);
  }

  return (
    <Shell teller={teller} banner={faults.has("banner")} modal={faults.has("modal")}>
      <div className="panel max-w-[620px]">
        <div className="panel-hd">Funds Transfer — Step 2 of 3 — Review</div>
        <div className="p-3">
          {overLimit ? (
            <p className="mb-3 border border-[#c9a227] bg-[#fdf3c8] px-2 py-1.5 text-[#5b4708]">
              Transfer exceeds the daily limit of {money(DAILY_TRANSFER_LIMIT)} for this
              member. Reduce the amount or route the request to a supervisor.
            </p>
          ) : null}
          {insufficient && !overLimit ? (
            <p className="mb-3 border border-[#c9a227] bg-[#fdf3c8] px-2 py-1.5 text-[#5b4708]">
              Insufficient available funds in account {from}. Available balance is{" "}
              {money(available)}.
            </p>
          ) : null}

          <table className="grid mb-3">
            <tbody>
              <tr>
                <th className="w-[180px]">Member</th>
                <td>
                  {member.name} ({member.id})
                </td>
              </tr>
              <tr>
                <th>From Account</th>
                <td>
                  {src.account.number} — {src.account.kind} — available {money(available)}
                </td>
              </tr>
              <tr>
                <th>To Account</th>
                <td>
                  {dst.account.number} — {dst.account.kind}
                </td>
              </tr>
              <tr>
                <th>Transfer Amount</th>
                <td className="font-semibold">{money(amt)}</td>
              </tr>
              <tr>
                <th>Effective</th>
                <td>Immediate</td>
              </tr>
            </tbody>
          </table>

          <form action={confirm} className="inline">
            <input type="hidden" name="member" value={memberId} />
            <input type="hidden" name="from" value={from} />
            <input type="hidden" name="to" value={to} />
            <input type="hidden" name="amount" value={amt} />
            <button
              type="submit"
              className="btn-primary"
              disabled={overLimit || insufficient}
            >
              Confirm Transfer
            </button>
          </form>
          <Link href="/transfer" className="btn ml-2">
            Back
          </Link>

          <p className="mt-3 border-t border-[var(--rule)] pt-2 text-[11px] text-[#5d6b7a]">
            Confirming posts the transfer immediately. Reversals require a supervisor
            override.
          </p>
        </div>
      </div>
    </Shell>
  );
}
