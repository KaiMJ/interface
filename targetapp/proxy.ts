/**
 * Session-expiry injection.
 *
 * This lives in the proxy layer (Next 16's replacement for `middleware`) rather
 * than in the page guard for a mundane reason:
 * Next forbids writing cookies during a Server Component render, and expiring a
 * session means clearing one. The proxy is the one place that can both redirect
 * and mutate cookies.
 *
 * It fires ONCE and clears itself. A session-expiry that fired on every request
 * would make the app permanently unusable and would test only whether the agent
 * can loop. Firing once means the automation hits it mid-flow — which is how it
 * happens in production — and the app is usable again afterwards, so the operator
 * who takes over can actually finish the work.
 */

import { NextResponse, type NextRequest } from "next/server";

const EXEMPT = ["/login", "/logout", "/dev", "/api", "/_next", "/favicon.ico"];

export default function proxy(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (EXEMPT.some((p) => pathname.startsWith(p))) return NextResponse.next();

  const faults = (req.cookies.get("faults")?.value ?? "").split(",").map((s) => s.trim());
  if (!faults.includes("expired")) return NextResponse.next();

  const url = req.nextUrl.clone();
  url.pathname = "/login";
  url.search = "?reason=expired";

  const res = NextResponse.redirect(url);
  res.cookies.delete("teller_sid");
  res.cookies.set("faults", faults.filter((f) => f && f !== "expired").join(","), {
    path: "/",
    sameSite: "lax",
  });
  return res;
}

export const config = {
  matcher: ["/((?!_next/static|_next/image).*)"],
};
