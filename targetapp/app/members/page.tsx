/**
 * Member search: search -> results -> detail, the shape the brief asks for. Two
 * intentional details — a query that matches nothing renders "No member matches" and
 * an HTTP 200, because it is an answer rather than an error; and every result row's
 * action link is labelled "View", which is the normal case in these systems and why
 * a step has to target the row matching a predicate rather than the text.
 */

import Link from "next/link";
import { Shell } from "@/components/Shell";
import { searchMembers } from "@/lib/data";
import { activeFaults, sleep } from "@/lib/faults";
import { requireTeller } from "@/lib/session";

export default async function Members({
  searchParams,
}: {
  searchParams: Promise<{ q?: string }>;
}) {
  const teller = await requireTeller();
  const faults = await activeFaults();
  const { q = "" } = await searchParams;

  if (q && faults.has("slow")) await sleep(4000);

  const results = q ? searchMembers(q) : [];

  return (
    <Shell teller={teller} banner={faults.has("banner")} modal={faults.has("modal")}>
      <div className="panel mb-3">
        <div className="panel-hd">Member Search</div>
        <form method="get" action="/members" className="p-3">
          <table>
            <tbody>
              <tr>
                <td className="pr-2 whitespace-nowrap">Member ID or Name</td>
                <td className="pr-2">
                  <input type="text" name="q" defaultValue={q} size={28} autoComplete="off" />
                </td>
                <td>
                  <button type="submit" className="btn-primary">
                    Search
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <p className="mt-2 text-[11px] text-[#5d6b7a]">
            Try 12345, 22841, 30992, 44100, 57310 — or a surname.
          </p>
        </form>
      </div>

      {q ? (
        <div className="panel">
          <div className="panel-hd">Search Results</div>
          <div className="p-3">
            {results.length === 0 ? (
              <p className="border border-[var(--rule)] bg-[#f7f8fa] px-2 py-2">
                No member matches the search criteria entered.
              </p>
            ) : (
              <table className="datagrid">
                <thead>
                  <tr>
                    <th>Member ID</th>
                    <th>Name</th>
                    <th>Branch</th>
                    <th>Member Since</th>
                    <th>Accounts</th>
                    <th>Action</th>
                  </tr>
                </thead>
                <tbody>
                  {results.map((m) => (
                    <tr key={m.id}>
                      <td>{m.id}</td>
                      <td>{m.name}</td>
                      <td>{m.branch}</td>
                      <td>{m.since}</td>
                      <td className="num">{m.accounts.length}</td>
                      <td>
                        <Link href={`/members/${m.id}`} className="text-[#1b4f83] underline">
                          View
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      ) : null}
    </Shell>
  );
}
