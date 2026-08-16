/**
 * Teller session.
 *
 * A cookie and a redirect. Authentication is not the interesting part of this
 * project; it is here because a back-office app that opens straight onto member
 * data would be an unrealistic surface, and because login is where the
 * automation's one credential is used — which is what makes "secrets never reach
 * an artifact" a claim with something behind it.
 */

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

const COOKIE = "teller_sid";

export async function signIn(username: string): Promise<void> {
  const jar = await cookies();
  jar.set(COOKIE, username, { path: "/", httpOnly: true, sameSite: "lax" });
}

export async function signOut(): Promise<void> {
  const jar = await cookies();
  jar.delete(COOKIE);
}

export async function currentTeller(): Promise<string | null> {
  const jar = await cookies();
  return jar.get(COOKIE)?.value ?? null;
}

/**
 * Guard for every authenticated page.
 *
 * The injected `expired` fault is handled a layer up, in `proxy.ts` —
 * expiring a session means clearing a cookie, and Next forbids cookie writes
 * during a Server Component render. This is the plain auth check.
 *
 * Session expiry is the case the replay engine must not paper over: silently
 * re-authenticating would let an automation resume a run whose context may no
 * longer be valid, so policy escalates instead.
 */
export async function requireTeller(): Promise<string> {
  const teller = await currentTeller();
  if (!teller) redirect("/login?reason=required");
  return teller;
}
