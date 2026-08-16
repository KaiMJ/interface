/**
 * Transfer funds — step 1 of 3. The write capability's entry point.
 *
 * Multi-field form -> review -> confirmation is the second flow shape the brief
 * asks for, and it is where the risky/irreversible distinction becomes concrete:
 * everything on this page is safe, and the single button on the review page is
 * not.
 *
 * The `validation` fault makes the inline error render above the fields, which
 * pushes every input below it down. That is the everyday reason a recorded
 * coordinate is wrong in an app that has not changed at all.
 */

import { redirect } from "next/navigation";
import { Shell } from "@/components/Shell";
import { MEMBERS, findMember, money, balanceOf } from "@/lib/data";
import { activeFaults } from "@/lib/faults";
import { requireTeller } from "@/lib/session";

const ERRORS: Record<string, string> = {
  amount: "Enter a valid transfer amount greater than 0.00.",
  same: "The source and destination accounts must be different.",
  member: "Enter a valid member ID.",
  accounts: "Select both a source and a destination account.",
  injected: "Amount field failed validation. Correct the highlighted field and resubmit.",
};

export default async function Transfer({
  searchParams,
}: {
  searchParams: Promise<{ member?: string; err?: string; amount?: string }>;
}) {
  const teller = await requireTeller();
  const faults = await activeFaults();
  const { member: memberId = "", err, amount = "" } = await searchParams;

  const member = memberId ? findMember(memberId) : undefined;

  async function submit(formData: FormData) {
    "use server";
    const m = String(formData.get("member") ?? "").trim();
    const from = String(formData.get("from") ?? "");
    const to = String(formData.get("to") ?? "");
    const amt = String(formData.get("amount") ?? "").trim();

    const active = await activeFaults();
    if (active.has("validation")) {
      redirect(`/transfer?member=${encodeURIComponent(m)}&err=injected&amount=${encodeURIComponent(amt)}`);
    }
    if (!findMember(m)) redirect(`/transfer?member=${encodeURIComponent(m)}&err=member`);
    if (!from || !to) redirect(`/transfer?member=${encodeURIComponent(m)}&err=accounts`);
    if (from === to) redirect(`/transfer?member=${encodeURIComponent(m)}&err=same`);

    const n = Number(amt.replace(/[$,]/g, ""));
    if (!Number.isFinite(n) || n <= 0) {
      redirect(`/transfer?member=${encodeURIComponent(m)}&err=amount`);
    }

    redirect(`/transfer/review?member=${m}&from=${from}&to=${to}&amount=${n}`);
  }

  return (
    <Shell teller={teller} banner={faults.has("banner")} modal={faults.has("modal")}>
      <div className="panel max-w-[620px]">
        <div className="panel-hd">Funds Transfer — Step 1 of 3</div>
        <form action={submit} className="p-3">
          {/* Rendered ABOVE the fields on purpose: an inline error that appears
              here moves every control below it. */}
          {err ? (
            <p className="mb-3 border border-[#c98b8b] bg-[#fbeaea] px-2 py-1.5 text-[#7d1f1f]">
              {ERRORS[err] ?? "The request could not be processed."}
            </p>
          ) : null}

          <table className="mb-3">
            <tbody>
              <tr>
                <td className="py-1 pr-3 whitespace-nowrap">Member ID</td>
                <td className="py-1">
                  <input type="text" name="member" defaultValue={memberId} size={12} autoComplete="off" />
                  <button type="submit" formAction={loadMember} className="btn ml-2">
                    Load
                  </button>
                </td>
              </tr>
              <tr>
                <td className="py-1 pr-3 whitespace-nowrap">From Account</td>
                <td className="py-1">
                  <select name="from" defaultValue="">
                    <option value="">-- select --</option>
                    {(member?.accounts ?? []).map((a) => (
                      <option key={a.number} value={a.number}>
                        {a.number} — {a.kind} — {money(balanceOf(a.number))}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
              <tr>
                <td className="py-1 pr-3 whitespace-nowrap">To Account</td>
                <td className="py-1">
                  <select name="to" defaultValue="">
                    <option value="">-- select --</option>
                    {(member?.accounts ?? []).map((a) => (
                      <option key={a.number} value={a.number}>
                        {a.number} — {a.kind} — {money(balanceOf(a.number))}
                      </option>
                    ))}
                  </select>
                </td>
              </tr>
              <tr>
                <td className="py-1 pr-3 whitespace-nowrap">Amount</td>
                <td className="py-1">
                  <input
                    type="text"
                    name="amount"
                    defaultValue={amount}
                    size={12}
                    autoComplete="off"
                    className={err === "injected" || err === "amount" ? "border-2 border-[#a33]" : ""}
                  />
                </td>
              </tr>
            </tbody>
          </table>

          <button type="submit" className="btn-primary">
            Continue
          </button>
          <p className="mt-3 border-t border-[var(--rule)] pt-2 text-[11px] text-[#5d6b7a]">
            Transfers are limited to {money(5000)} per member per day. Nothing moves until
            the confirmation step.
          </p>
        </form>
      </div>

      <p className="mt-3 text-[11px] text-[#5d6b7a]">
        Members with more than one account: {MEMBERS.filter((m) => m.accounts.length > 1).map((m) => m.id).join(", ")}
      </p>
    </Shell>
  );
}

/** Reloads the form with that member's accounts populated. */
async function loadMember(formData: FormData) {
  "use server";
  const m = String(formData.get("member") ?? "").trim();
  redirect(`/transfer?member=${encodeURIComponent(m)}`);
}
