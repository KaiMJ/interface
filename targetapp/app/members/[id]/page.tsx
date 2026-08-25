/**
 * Member detail: profile, accounts, and a long transaction history. Four
 * distinguishable results live on this one route, and telling them apart is most of
 * the point: member not found (a business outcome), restricted member (a permission
 * denial, which is not "not found"), the error500 fault (a hard failure), and the
 * slow fault (recoverable by waiting).
 *
 * The history is long enough to require scrolling, which makes find_and_act a real
 * requirement. Balance and Available differ by holds on some accounts — an
 * extraction that grabs the first currency-looking string gets the wrong number and
 * looks correct.
 */

import Link from "next/link";
import { Shell } from "@/components/Shell";
import {
  availableOf,
  balanceOf,
  findMember,
  money,
  transactionsFor,
} from "@/lib/data";
import { activeFaults, sleep } from "@/lib/faults";
import { requireTeller } from "@/lib/session";

export default async function MemberDetail({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ acct?: string }>;
}) {
  const teller = await requireTeller();
  const faults = await activeFaults();
  const { id } = await params;
  const { acct } = await searchParams;

  if (faults.has("slow")) await sleep(4000);

  const shell = (children: React.ReactNode) => (
    <Shell teller={teller} banner={faults.has("banner")} modal={faults.has("modal")}>
      {children}
    </Shell>
  );

  if (faults.has("error500")) {
    return shell(
      <div className="panel">
        <div className="panel-hd">Application Error</div>
        <div className="p-4">
          <p className="mb-2 font-semibold text-[#8a1c1c]">
            An unexpected error occurred while processing your request.
          </p>
          <p className="text-[11px] text-[#5d6b7a]">
            Reference: MCU-5031-A. Contact the service desk if this persists.
          </p>
        </div>
      </div>,
    );
  }

  const member = findMember(id);

  if (!member) {
    return shell(
      <div className="panel">
        <div className="panel-hd">Member Inquiry</div>
        <div className="p-4">
          <p className="border border-[var(--rule)] bg-[#f7f8fa] px-2 py-2">
            No member record found for ID {id}.
          </p>
          <p className="mt-3">
            <Link href="/members" className="text-[#1b4f83] underline">
              Return to search
            </Link>
          </p>
        </div>
      </div>,
    );
  }

  if (member.restricted || faults.has("denied")) {
    return shell(
      <div className="panel">
        <div className="panel-hd">Member Inquiry</div>
        <div className="p-4">
          <p className="border border-[#c98b8b] bg-[#fbeaea] px-2 py-2 text-[#7d1f1f]">
            You do not have permission to view this member record. Entitlement
            MBR_VIEW_RESTRICTED is required.
          </p>
          <p className="mt-3 text-[11px] text-[#5d6b7a]">
            Member ID {member.id} exists. Access is restricted by policy.
          </p>
        </div>
      </div>,
    );
  }

  const selected = acct ?? member.accounts[0]?.number ?? "";
  const txns = selected ? transactionsFor(selected) : [];

  return shell(
    <>
      <div className="panel mb-3">
        <div className="panel-hd">Member Profile</div>
        <div className="p-3">
          {/* A long name wraps here and pushes the accounts grid down. Nothing
              below this block has a stable y coordinate across members. */}
          <h1 className="mb-2 max-w-[380px] text-[16px] leading-tight font-semibold">
            {member.name}
          </h1>
          <table>
            <tbody>
              <tr>
                <td className="pr-6 text-[#5d6b7a]">Member ID</td>
                <td className="pr-10 font-semibold">{member.id}</td>
                <td className="pr-6 text-[#5d6b7a]">Branch</td>
                <td>{member.branch}</td>
              </tr>
              <tr>
                <td className="pr-6 text-[#5d6b7a]">Member Since</td>
                <td className="pr-10">{member.since}</td>
                <td className="pr-6 text-[#5d6b7a]">Phone</td>
                <td>{member.phone}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div className="panel mb-3">
        <div className="panel-hd">Accounts</div>
        <div className="p-3">
          <table className="datagrid">
            <thead>
              <tr>
                <th>Account</th>
                <th>Type</th>
                <th>Nickname</th>
                <th>Status</th>
                <th className="num">Current Balance</th>
                <th className="num">Available Balance</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {member.accounts.map((a) => (
                <tr key={a.number}>
                  <td>{a.number}</td>
                  <td>{a.kind}</td>
                  <td>{a.nickname}</td>
                  <td>{a.status}</td>
                  <td className="num">{money(balanceOf(a.number))}</td>
                  <td className="num">{money(availableOf(a.number))}</td>
                  <td>
                    <Link
                      href={`/members/${member.id}?acct=${a.number}`}
                      className="text-[#1b4f83] underline"
                    >
                      View
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] text-[#5d6b7a]">
            Available balance reflects current holds and pending items.
          </p>
        </div>
      </div>

      <div className="panel">
        <div className="panel-hd">Transaction History — Account {selected}</div>
        <div className="p-3">
          <table className="datagrid">
            <thead>
              <tr>
                <th>Date</th>
                <th>Posted</th>
                <th>Description</th>
                <th>Category</th>
                <th className="num">Amount</th>
              </tr>
            </thead>
            <tbody>
              {txns.map((t, i) => (
                <tr key={i}>
                  <td className="whitespace-nowrap">{t.date}</td>
                  <td className="whitespace-nowrap">{t.posted}</td>
                  {/* Truncated at a fixed width, ellipsis and all. The single most
                      common reason a correct predicate fails to match. */}
                  <td className="max-w-[240px] truncate">{t.description}</td>
                  <td>{t.category}</td>
                  <td className={`num ${t.amount < 0 ? "text-[#7d1f1f]" : ""}`}>
                    {t.amount < 0 ? `(${money(-t.amount)})` : money(t.amount)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-[11px] text-[#5d6b7a]">
            Debits shown in parentheses. Showing most recent {txns.length} items.
          </p>
        </div>
      </div>
    </>,
  );
}
