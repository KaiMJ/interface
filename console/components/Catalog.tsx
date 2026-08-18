"use client";

/**
 * The capability catalog — what this system knows how to do.
 *
 * This is the agent-facing surface rendered for a human: the same list
 * `/capabilities` hands a calling agent, with the contract each one offers and
 * the approval gate that decides whether an agent may call it at all. A draft is
 * a proposal a model helped write and nobody has vouched for; approving it here
 * is the same act as approving it from the CLI, and it is the reviewer's only
 * gate before unattended replay.
 *
 * The history strip beside it is the reason approving is a judgement rather than
 * a formality: resolution tiers and settle modes aggregated over past runs say
 * whether the flow still fits the application, and one run cannot tell you that.
 */

import { useState } from "react";
import { Chip, Empty, Field, Json, Panel, StatusDot, ms } from "./ui";
import {
  api,
  isFailed,
  type Capability,
  type CapabilityHistory,
  type CapabilitySummary,
} from "@/lib/api";

export function Catalog({
  capabilities,
  selected,
  onSelect,
  onFilterRuns,
  operator,
  onChanged,
  className = "",
}: {
  capabilities: CapabilitySummary[];
  selected: string | null;
  onSelect: (ref: string) => void;
  onFilterRuns: (capabilityId: string) => void;
  operator: string;
  onChanged: () => void;
  className?: string;
}) {
  const [error, setError] = useState<string | null>(null);

  async function approve(c: CapabilitySummary) {
    const result = await api.approve(c.id, c.version, operator);
    if (isFailed(result)) {
      setError(result.error);
      return;
    }
    setError(null);
    onChanged();
  }

  return (
    <Panel
      className={className}
      title={`Capabilities · ${capabilities.length}`}
      bodyClassName="overflow-y-auto"
    >
      {capabilities.length === 0 ? (
        <Empty>
          Nothing recorded yet. A successful discovery run emits one and it appears here with
          the contract an agent calls it by.
        </Empty>
      ) : (
        capabilities.map((c) => (
          <div
            key={c.ref}
            className="border-b border-[var(--rule)] px-3 py-2 last:border-0"
            style={{ background: c.ref === selected ? "#222a33" : undefined }}
          >
            <button className="block w-full text-left" onClick={() => onSelect(c.ref)}>
              <div className="flex items-center gap-2">
                <span className="mono truncate">{c.ref}</span>
                <span className="ml-auto">
                  <StatusDot status={c.status} />
                </span>
              </div>
              <div className="truncate pt-0.5 text-[11px] text-[var(--muted)]">{c.goal}</div>
              <div className="flex flex-wrap gap-1 pt-1">
                <Chip>{c.app}</Chip>
                <Chip>{c.steps} steps</Chip>
                <Chip>{Object.keys(c.inputs).length} in</Chip>
                <Chip>{Object.keys(c.outputs).length} out</Chip>
                <Chip tone={c.outcomes.length ? undefined : "recovered"}>
                  {c.outcomes.length} outcomes
                </Chip>
              </div>
            </button>
            <div className="flex flex-wrap gap-2 pt-2">
              <button className="btn" onClick={() => onFilterRuns(c.id)}>
                runs
              </button>
              {c.status === "draft" ? (
                <button
                  className="btn"
                  onClick={() => approve(c)}
                  title="draft → approved. The gate on unattended replay; an agent may only call approved capabilities."
                >
                  Approve
                </button>
              ) : null}
            </div>
          </div>
        ))
      )}
      {error ? <p className="px-3 pb-2 text-[11px] text-[var(--err)]">{error}</p> : null}
    </Panel>
  );
}

export function CapabilityCard({
  capability,
  history,
}: {
  capability: Capability | null;
  history: CapabilityHistory | null;
}) {
  if (!capability) {
    return (
      <Panel title="Capability">
        <Empty>
          Recorded capabilities appear here with the contract an agent calls them by.
        </Empty>
      </Panel>
    );
  }
  const cap = capability;
  const agg = history?.aggregate;
  const tiers = Object.entries(agg?.resolution_tiers ?? {});
  const total = tiers.reduce((sum, [, n]) => sum + n, 0);

  return (
    <Panel
      title="Capability"
      right={<StatusDot status={cap.status} />}
      bodyClassName="space-y-1 p-3 text-[12px]"
    >
      <Field label="id" value={`${cap.id}@v${cap.version}`} mono />
      <Field label="goal" value={cap.goal} />
      {cap.description ? <Field label="what it does" value={cap.description} /> : null}
      <Field
        label="inputs"
        value={cap.inputs.map((i) => `${i.name}: ${i.type}`).join(", ") || "—"}
        mono
      />
      <Field
        label="outputs"
        value={cap.outputs.map((o) => `${o.name}: ${o.type}`).join(", ") || "—"}
        mono
      />
      <Field
        label="outcomes"
        value={cap.business_outcomes.map((o) => o.name).join(", ") || "none declared"}
        title="declared alternatives a calling agent can branch on, instead of treating them as failures"
      />
      <Field label="success" value={cap.success?.value ?? "—"} />
      {cap.recording ? (
        <Field
          label="recorded"
          value={`${cap.recording.recorded_at.slice(0, 16).replace("T", " ")} · ${cap.recording.model}`}
        />
      ) : null}

      {/* Steps a reviewer has to look at before approving: the ones the run could
          not verify, and the ones policy will treat as risky. */}
      {cap.steps.some((s) => s.note) ? (
        <div className="pt-1">
          {cap.steps
            .filter((s) => s.note)
            .map((s) => (
              <p key={s.id} className="text-[11px] text-[var(--warn)]">
                step {s.id}: {s.note}
              </p>
            ))}
        </div>
      ) : null}

      {agg && agg.total > 0 ? (
        <div className="space-y-1 border-t border-[var(--rule)] pt-2">
          <div className="mono text-[10px] tracking-wider text-[var(--muted)] uppercase">
            {agg.total} past runs
          </div>
          <div className="flex flex-wrap gap-1">
            {Object.entries(agg.statuses).map(([status, n]) => (
              <Chip key={status} tone={status}>
                {status} ×{n}
              </Chip>
            ))}
            {agg.median_duration_ms ? <Chip>median {ms(agg.median_duration_ms)}</Chip> : null}
          </div>
          {/* The drift canary. Anchor resolutions decaying into the recorded box
              means the application moved — visible here long before a failure. */}
          {total > 0 ? (
            <>
              <div className="flex h-[6px] w-full overflow-hidden rounded-sm">
                {tiers.map(([tier, n]) => (
                  <span
                    key={tier}
                    title={`${tier}: ${n}`}
                    style={{
                      width: `${(n / total) * 100}%`,
                      background:
                        tier === "anchor_text"
                          ? "var(--ok)"
                          : tier === "role_name"
                            ? "var(--accent)"
                            : tier === "recorded_bbox"
                              ? "var(--err)"
                              : "#39424d",
                    }}
                  />
                ))}
              </div>
              <p className="text-[11px] text-[var(--muted)]">
                resolution tiers across past runs ·{" "}
                {agg.drift_share !== null
                  ? `${Math.round(agg.drift_share * 100)}% fell through to the recorded box`
                  : "—"}
              </p>
            </>
          ) : null}
        </div>
      ) : null}
      <Json value={cap} label="raw artifact" />
    </Panel>
  );
}
