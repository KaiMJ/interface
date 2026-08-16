import { redirect } from "next/navigation";
import { TELLER } from "@/lib/data";
import { signIn } from "@/lib/session";

const REASONS: Record<string, string> = {
  expired: "Your session has expired due to inactivity. Please sign in again.",
  required: "Sign in to continue.",
  bad: "Sign-on failed. Check your user ID and password.",
};

export default async function Login({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string }>;
}) {
  const { reason } = await searchParams;

  async function submit(formData: FormData) {
    "use server";
    const u = String(formData.get("user") ?? "");
    const p = String(formData.get("pw") ?? "");
    if (u !== TELLER.username || p !== TELLER.password) redirect("/login?reason=bad");
    await signIn(u);
    redirect("/members");
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="panel w-[380px]">
        <div className="panel-hd">Meridian Credit Union — Staff Sign-On</div>
        <form action={submit} className="p-4">
          {reason ? (
            <p className="mb-3 border border-[#c9a227] bg-[#fdf3c8] px-2 py-1.5 text-[12px] text-[#5b4708]">
              {REASONS[reason] ?? REASONS.required}
            </p>
          ) : null}

          <table className="mb-3 w-full">
            <tbody>
              <tr>
                <td className="py-1 pr-3 whitespace-nowrap">User ID</td>
                <td className="py-1">
                  <input type="text" name="user" autoComplete="off" className="w-full" />
                </td>
              </tr>
              <tr>
                <td className="py-1 pr-3 whitespace-nowrap">Password</td>
                <td className="py-1">
                  <input type="password" name="pw" autoComplete="off" className="w-full" />
                </td>
              </tr>
            </tbody>
          </table>

          <button type="submit" className="btn-primary">
            Sign On
          </button>

          <p className="mt-4 border-t border-[var(--rule)] pt-2 text-[11px] text-[#5d6b7a]">
            Demo environment. No real member data. Credentials are in{" "}
            <code>.env.example</code>; the automation reads them from configuration
            and never records them.
          </p>
        </form>
      </div>
    </div>
  );
}
