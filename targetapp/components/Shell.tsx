/**
 * App chrome: title bar, teller strip, nav, and the two injectable interruptions.
 *
 * Deliberately hostile-ish, in ways that are true of real back-office systems and
 * that a modern component library would accidentally fix:
 *
 *   - no data-testid, anywhere in this app
 *   - nav items are plain anchors with generic labels
 *   - the notice banner and the modal are the two "everything moved / something
 *     is on top of my target" cases, and they are togglable
 *
 * The banner is the shift case: it pushes every coordinate below it down by its
 * own height. The modal is the overlay case: it moves nothing and lands on top,
 * which no amount of better targeting detects — only verification does.
 */

import Link from "next/link";

export function Shell({
  teller,
  banner,
  modal,
  children,
}: {
  teller: string;
  banner: boolean;
  modal: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="min-h-screen">
      {banner ? (
        <div className="border-b border-[#c9a227] bg-[#fdf3c8] px-3 py-2 text-[12px] text-[#5b4708]">
          <strong>Notice:</strong> Core processing will be unavailable Saturday 02:00–04:00 PT.
          Batch postings may be delayed. Contact the service desk at extension 4180 with questions.
        </div>
      ) : null}

      <div className="bg-[var(--chrome-dark)] px-3 py-1.5 text-[13px] font-semibold tracking-wide text-white">
        MERIDIAN CREDIT UNION &nbsp;·&nbsp; Member Services Console
        <span className="float-right text-[11px] font-normal opacity-80">
          Teller: {teller} &nbsp;|&nbsp; Terminal 04-A &nbsp;|&nbsp;{" "}
          <Link href="/logout" className="underline">
            Sign out
          </Link>
        </span>
      </div>

      <nav className="flex gap-4 border-b border-[var(--rule)] bg-[#c3ccd6] px-3 py-1 text-[12px]">
        <Link href="/members" className="hover:underline">
          Member Search
        </Link>
        <Link href="/transfer" className="hover:underline">
          Transfers
        </Link>
        <span className="cursor-default text-[#6d7a88]">Holds</span>
        <span className="cursor-default text-[#6d7a88]">Cards</span>
        <span className="cursor-default text-[#6d7a88]">Reports</span>
        <Link href="/dev" className="ml-auto text-[#455a70] hover:underline">
          Fault Panel
        </Link>
      </nav>

      <main className="p-3">{children}</main>

      {modal ? <MaintenanceModal /> : null}
    </div>
  );
}

/**
 * The unexpected dialog.
 *
 * Rendered last and positioned over everything. It does not move the page — that
 * is the whole point. A recorded coordinate still resolves to the "right" place
 * and the click lands here instead.
 */
function MaintenanceModal() {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="w-[420px] border border-[#7b8794] bg-white shadow-lg">
        <div className="bg-[var(--chrome)] px-3 py-1.5 text-[12px] font-semibold text-white">
          System Message
        </div>
        <div className="p-4 text-[12px] leading-relaxed">
          A scheduled maintenance window has been announced for this environment. Some
          balances may reflect prior-day values until batch completes.
        </div>
        <div className="flex justify-end gap-2 border-t border-[var(--rule)] bg-[#eef1f4] px-3 py-2">
          {/* Two controls, one of which is not what an agent wants. Dismissing is
              a declared recovery in policy; "More Info" is a trap that navigates
              away mid-flow. */}
          <a href="/dev" className="btn">
            More Info
          </a>
          <form action="/api/dismiss" method="post">
            <button type="submit" className="btn-primary">
              Dismiss
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
