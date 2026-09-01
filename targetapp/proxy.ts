/**
 * Session-expiry injection, in the proxy layer rather than a page guard: Next forbids
 * writing cookies during a Server Component render, and expiring a session means
 * clearing one.
 *
 * It fires ONCE and clears itself, so the automation hits it mid-flow and whoever takes
 * over can finish the work.
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
