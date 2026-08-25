"use client";

/**
 * Starting work from the console. Two ways in, and the difference is the system in
 * miniature: **Discover** takes a goal in English, costs model calls and may stop to
 * ask a human; **Replay** takes a capability and typed inputs, and constructs no
 * model at all. One panel rather than two, because putting them side by side is the
 * clearest statement of which is the production path.
 *
 * Both start the run in the background and hand back an id — a discovery run outlasts
 * any sensible HTTP timeout, and the operator wants to watch rather than wait.
 */

import { useEffect, useState } from "react";
import { Empty, Panel } from "./ui";
import { api, isFailed, type CapabilitySummary } from "@/lib/api";

type Mode = "discover" | "replay";

export function Launch({
  capabilities,
  apps,
  defaultApp,
  busyWith,
  selected,
  onStarted,
  onSelectCapability,
}: {
  capabilities: CapabilitySummary[];
  /** Every application with a policy file. Selecting one selects its guardrails. */
  apps: string[];
  defaultApp: string;
  /** The run currently holding the session, if any. One display, one run. */
  busyWith: string | null;
  selected: string | null;
  onStarted: (runId: string) => void;
  onSelectCapability: (ref: string) => void;
}) {
  const [mode, setMode] = useState<Mode>("discover");
  const [app, setApp] = useState(defaultApp);
  const [goal, setGoal] = useState(
    "open the profile for member 12345 and read the current balance of their Primary Savings account",
  );
  // Inputs are not just "what to type": they are the parameter declaration. Any
  // literal in the recording matching one of these becomes a placeholder at
  // synthesis, which is how "who decided 12345 was a parameter?" gets a
  // deterministic answer. The hint says so, because it is not obvious.
  const [pairs, setPairs] = useState<[string, string][]>([
    ["member_id", "12345"],
    ["account_nickname", "Primary Savings"],
  ]);
  const [capabilityId, setCapabilityId] = useState("");
  const [replayInputs, setReplayInputs] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const chosen = capabilities.find((c) => c.ref === selected) ?? capabilities[0] ?? null;

  useEffect(() => {
    if (!chosen) return;
    setReplayInputs((current) => {
      const next: Record<string, string> = {};
      for (const name of Object.keys(chosen.inputs)) next[name] = current[name] ?? "";
      return next;
    });
  }, [chosen?.ref]); // eslint-disable-line react-hooks/exhaustive-deps

  async function start() {
    setError(null);
    setPending(true);
    const started =
      mode === "discover"
        ? await api.discover({
            goal,
            app,
            inputs: Object.fromEntries(pairs.filter(([k]) => k.trim())),
            capability_id: capabilityId.trim() || null,
          })
        : chosen
          ? await api.replay({
              capability_id: chosen.id,
              version: chosen.version,
              inputs: replayInputs,
            })
          : { error: "no capability selected" };
    setPending(false);
    if (isFailed(started)) {
      setError(started.error);
      return;
    }
    onStarted(started.run_id);
  }

  const disabled = pending || busyWith !== null || (mode === "discover" ? !goal.trim() : !chosen);

  return (
    <Panel
      title="Start a run"
      right={
        <span className="flex gap-1">
          {(["discover", "replay"] as Mode[]).map((m) => (
            <button
              key={m}
              className="btn"
              style={
                mode === m
                  ? { borderColor: "var(--accent)", color: "var(--text)" }
                  : { color: "var(--muted)" }
              }
              onClick={() => setMode(m)}
            >
              {m}
            </button>
          ))}
        </span>
      }
    >
      <div className="space-y-2 p-3 text-[12px]">
        {mode === "discover" ? (
          <>
            {apps.length > 1 ? (
              <>
                <label className="mono block text-[11px] text-[var(--muted)]">
                  application — selects its policy file
                </label>
                <select
                  value={app}
                  onChange={(e) => setApp(e.target.value)}
                  className="mono w-full rounded border border-[var(--rule)] bg-[#10151a] px-2 py-1 outline-none focus:border-[var(--accent)]"
                >
                  {apps.map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </select>
              </>
            ) : null}
            <label className="mono block text-[11px] text-[var(--muted)]">goal</label>
            <textarea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={3}
              className="w-full resize-y rounded border border-[var(--rule)] bg-[#10151a] p-2 text-[12px] outline-none focus:border-[var(--accent)]"
              placeholder="look up member 12345 and read their savings balance"
            />
            <label className="mono block text-[11px] text-[var(--muted)]">
              inputs — also the parameter declaration
            </label>
            {pairs.map(([k, v], i) => (
              <div key={i} className="flex gap-1">
                <input
                  value={k}
                  onChange={(e) =>
                    setPairs((p) => p.map((row, j) => (j === i ? [e.target.value, row[1]] : row)))
                  }
                  placeholder="name"
                  className="mono w-[40%] rounded border border-[var(--rule)] bg-[#10151a] px-2 py-1 outline-none focus:border-[var(--accent)]"
                />
                <input
                  value={v}
                  onChange={(e) =>
                    setPairs((p) => p.map((row, j) => (j === i ? [row[0], e.target.value] : row)))
                  }
                  placeholder="value for this run"
                  className="mono min-w-0 flex-1 rounded border border-[var(--rule)] bg-[#10151a] px-2 py-1 outline-none focus:border-[var(--accent)]"
                />
                <button
                  className="btn"
                  onClick={() => setPairs((p) => p.filter((_, j) => j !== i))}
                  title="remove"
                >
                  ×
                </button>
              </div>
            ))}
            <button className="btn" onClick={() => setPairs((p) => [...p, ["", ""]])}>
              + input
            </button>
            <input
              value={capabilityId}
              onChange={(e) => setCapabilityId(e.target.value)}
              placeholder="capability id (optional — derived from the goal if blank)"
              className="mono w-full rounded border border-[var(--rule)] bg-[#10151a] px-2 py-1 outline-none focus:border-[var(--accent)]"
            />
          </>
        ) : capabilities.length === 0 ? (
          <Empty>
            No capabilities yet. Record one with a discovery run and it appears here as a
            callable contract.
          </Empty>
        ) : (
          <>
            <label className="mono block text-[11px] text-[var(--muted)]">capability</label>
            <select
              value={chosen?.ref ?? ""}
              onChange={(e) => onSelectCapability(e.target.value)}
              className="mono w-full rounded border border-[var(--rule)] bg-[#10151a] px-2 py-1 outline-none focus:border-[var(--accent)]"
            >
              {capabilities.map((c) => (
                <option key={c.ref} value={c.ref}>
                  {c.ref} · {c.status}
                </option>
              ))}
            </select>
            {chosen && Object.keys(chosen.inputs).length === 0 ? (
              <p className="text-[var(--muted)]">This capability declares no inputs.</p>
            ) : null}
            {Object.entries(chosen?.inputs ?? {}).map(([name, type]) => (
              <div key={name} className="flex items-center gap-2">
                <span className="mono w-[140px] shrink-0 text-[var(--muted)]">
                  {name}
                  <span className="opacity-60"> :{type}</span>
                </span>
                <input
                  value={replayInputs[name] ?? ""}
                  onChange={(e) =>
                    setReplayInputs((r) => ({ ...r, [name]: e.target.value }))
                  }
                  className="mono min-w-0 flex-1 rounded border border-[var(--rule)] bg-[#10151a] px-2 py-1 outline-none focus:border-[var(--accent)]"
                />
              </div>
            ))}
            <p className="text-[11px] text-[var(--muted)]">
              Inputs are validated against the declared contract before anything is touched. A
              type error is a rejected call, not a run that types “None” into a field.
            </p>
          </>
        )}

        <div className="flex items-center gap-2 pt-1">
          <button className="btn btn-accent" onClick={start} disabled={disabled}>
            {pending ? "starting…" : mode === "discover" ? "Record" : "Replay"}
          </button>
          {busyWith ? (
            <span className="mono text-[11px] text-[var(--warn)]">
              {busyWith} holds the session
            </span>
          ) : null}
        </div>
        {error ? <p className="text-[11px] text-[var(--err)]">{error}</p> : null}
      </div>
    </Panel>
  );
}
