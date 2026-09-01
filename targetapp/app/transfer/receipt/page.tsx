/**
 * Transfer funds — step 3 of 3, the checkpoint screen. Everything a capability extracts
 * is distinguishable by anchor text rather than position, and the success checkpoint
 * asserts "Transfer Complete" rather than concluding success from a button press.
 */

import Link from "next/link";
import { Shell } from "@/components/Shell";
import { balanceOf, findAccount, findMember, money } from "@/lib/data";
import { activeFaults } from "@/lib/faults";
import { requireTeller } from "@/lib/session";

export default async function Receipt({
  searchParams,
}: {
  searchParams: Promise<{ ref?: string; member?: string; from?: string; to?: string; amount?: string }>;
}) {
  const teller = await requireTeller();
  const faults = await activeFaults();
  const { ref = "", member: memberId = "", from = "", to = "", amount = "0" } = await searchParams;

  const member = findMember(memberId);
  const src = findAccount(from);
  const dst = findAccount(to);

  return (
    <Shell teller={teller} banner={faults.has("banner")} modal={faults.has("modal")}>
      <div className="panel max-w-[620px]">
        <div className="panel-hd">Funds Transfer — Step 3 of 3</div>
        <div className="p-3">
          <p className="mb-3 border border-[#8fb08f] bg-[#eef7ee] px-2 py-1.5 font-semibold text-[#245c24]">
            Transfer Complete
          </p>

          <table className="datagrid">
            <tbody>
              <tr>
                <th className="w-[200px]">Confirmation Reference</th>
                <td className="font-semibold">{ref}</td>
              </tr>
              <tr>
                <th>Member</th>
                <td>
                  {member?.name ?? "—"} ({memberId})
                </td>
              </tr>
              <tr>
                <th>Amount Transferred</th>
                <td>{money(Number(amount))}</td>
              </tr>
              <tr>
                <th>From Account {from} — New Balance</th>
                <td className="num">{money(balanceOf(from))}</td>
              </tr>
              <tr>
                <th>To Account {to} — New Balance</th>
                <td className="num">{money(balanceOf(to))}</td>
              </tr>
              <tr>
                <th>Posted By</th>
                <td>
                  {teller} · {src?.account.kind ?? "—"} → {dst?.account.kind ?? "—"}
                </td>
              </tr>
            </tbody>
          </table>

          <p className="mt-3">
            <Link href="/transfer" className="btn">
              New Transfer
            </Link>
            <Link href={`/members/${memberId}`} className="btn ml-2">
              Return to Member
            </Link>
          </p>
        </div>
      </div>
    </Shell>
  );
}
