"use client";

/**
 * The fault harness, beside the run it is meant to be watched against.
 *
 * Three properties make it safe next to a live session: the control plane drives the
 * browser to the fault URL itself, which is outside the app's allowlist by
 * construction, so arming is never something the agent can do; arming claims the
 * session, so it cannot interleave with a run; and faults live in app policy, so an
 * application declaring no harness shows no panel.
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
