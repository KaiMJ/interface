"use client";

/**
 * The fault harness, in the operator console.
 *
 * §3.3 is about what a replay does when the application does something
 * legitimate and inconvenient — a session that expires, a modal nobody declared,
 * a 500, a validation error. Producing those on demand is what the demo app's
 * fault panel is for, and until now reaching it meant leaving the console for the
 * target app's own `/dev` page in another tab. That is the wrong seam: you arm a
 * fault *because* you want to watch what the automation does about it, and the
 * watching is here.
 *
 * Three properties worth knowing, because they are what make this safe to expose
 * beside a live session rather than a hazard:
 *
 *   it is not the agent      the control plane drives the browser to the fault
 *                            URL with the driver — no policy check, no evidence,
 *                            no step. The URL is *outside* the app's allowlist by
 *                            construction, because an agent that could arm its
 *                            own faults could disarm them.
 *   it cannot interleave     arming claims the session the same way a run does,
 *                            so it is refused while one is in flight.
 *   it is per-application    faults live in the app's policy, so an application
 *                            that declares no harness — every real one — shows no
 *                            panel at all.
 */

import { useEffect, useState } from "react";
import { Panel } from "./ui";
import { api, isFailed, type Faults } from "@/lib/api";

export function FaultPanel({
  app,
  busy,
  onArmed,
}: {
  app?: string | null;
  /** The run holding the session, if any. Arming would fight it for the display. */
  busy: string | null;
  onArmed?: () => void;
}) {
  const [faults, setFaults] = useState<Faults | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    void api.faults(app).then((f) => {
      setFaults(f);
      if (f) setChosen(f.armed);
    });
  }, [app]);

  // No harness declared: a real application, and nothing to show.
  if (!faults || Object.keys(faults.available).length === 0) return null;

  async function arm(names: string[]) {
    setPending(true);
    setError(null);
    const result = await api.armFaults(names);
    setPending(false);
    if (isFailed(result)) {
      setError(result.error);
      return;
    }
    setChosen(result.armed);
    onArmed?.();
  }

  return (
    <Panel
      title="Fault injection · test harness"
      right={
        chosen.length > 0 ? (
          <span className="mono normal-case" style={{ color: "var(--warn)" }}>
            {chosen.length} armed
          </span>
        ) : null
      }
      bodyClassName="p-3 text-[12px]"
    >
      <div className="space-y-1">
        {Object.entries(faults.available).map(([name, what]) => (
          <label
            key={name}
            className="flex cursor-pointer items-start gap-2"
            title={what}
          >
            <input
              type="checkbox"
              className="mt-0.5"
              checked={chosen.includes(name)}
              onChange={(e) =>
                setChosen((c) =>
                  e.target.checked ? [...c, name] : c.filter((n) => n !== name),
                )
              }
            />
            <span className="min-w-0">
              <span className="mono">{name}</span>
              <span className="block text-[11px] text-[var(--muted)]">{what}</span>
            </span>
          </label>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2 pt-2">
        <button
          className="btn btn-accent"
          onClick={() => arm(chosen)}
          disabled={pending || busy !== null}
        >
          {pending ? "arming…" : "Arm"}
        </button>
        <button
          className="btn"
          onClick={() => arm([])}
          disabled={pending || busy !== null || chosen.length === 0}
        >
          Clear
        </button>
        {busy ? (
          <span className="mono text-[11px] text-[var(--warn)]">
            {busy} holds the session
          </span>
        ) : null}
      </div>
      {error ? <p className="pt-1 text-[11px] text-[var(--err)]">{error}</p> : null}
      <p className="pt-2 text-[11px] text-[var(--muted)]">
        Armed in the automation&apos;s own browser, before a run — faults live in a cookie, so
        your tab and its session never share them. Then start a run and watch what the engine
        makes of it: a modal is recovered, a slow page is waited for, an expiry signs itself
        back in, a 500 stops with the kind that names the cause.
      </p>
    </Panel>
  );
}
