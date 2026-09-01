"use client";

/**
 * The guardrails in force, read-only: editing policy from a debug console is a hole,
 * not a feature.
 *
 * Fetched for the *selected run's* application — the wrong app's allowlist beside a run
 * is a confident answer to the wrong question.
 */

import { Chip, Empty, Field, Json, Panel } from "./ui";
import type { Policy } from "@/lib/api";

export function PolicyCard({ policy }: { policy: Policy | null }) {
  if (!policy) {
    return (
      <Panel title="Policy">
        <Empty>No policy loaded.</Empty>
      </Panel>
    );
  }
  return (
    <Panel
      title={`Policy · ${policy.app} · read-only`}
      bodyClassName="space-y-1 p-3 text-[12px]"
      right={
        policy.apps && policy.apps.length > 1 ? (
          <span className="mono text-[10px] text-[var(--muted)]">
            {policy.apps.length} apps configured
          </span>
        ) : null
      }
    >
      {/* `app` and `vendor` are what a capability recorded here carries, so an
          artifact names the product it drives rather than the URL it happened to
          be recorded at. */}
      <Field label="vendor" value={policy.vendor ?? "—"} />
      <Field label="applies to" value={policy.base_url_pattern ?? "—"} mono />
      <Field label="allowlist" value={policy.allowed_url_patterns.join(" ")} mono />
      <div className="flex flex-wrap gap-1 pt-1">
        {policy.allowed_actions.map((a) => (
          <Chip key={a}>{a}</Chip>
        ))}
      </div>
      <Field
        label="risky"
        value={
          <>
            <span style={{ color: "var(--warn)" }}>{policy.risky_disposition}</span>
            <span className="text-[var(--muted)]">
              {" "}
              · {policy.risky_intent_patterns.length} promoting patterns
            </span>
          </>
        }
        title="what happens when a step is risky: allow, confirm (escalate to a human), or block"
      />
      {policy.risky_intent_patterns.map((p) => (
        <div key={p} className="mono pl-[100px] text-[11px] break-all text-[var(--muted)]">
          {p}
        </div>
      ))}
      <Field
        label="recover"
        value={policy.recoveries.map((r) => `${r.name} ×${r.max_per_run}`).join(", ") || "—"}
        title="declared recoverable conditions and how many times each may fire before it is a hard failure"
      />
      <Field label="escalate" value={policy.escalations.map((e) => e.name).join(", ") || "—"} />
      <Field label="app error" value={policy.app_errors.map((e) => e.name).join(", ") || "—"} />
      {/* Stated plainly rather than implied: v1's redactor is a seam, and the
          frames in this console are unmasked. Someone has to know that before
          they screenshot one. */}
      <Field
        label="redaction"
        value={
          <span style={{ color: "var(--warn)" }}>
            {String(policy.redaction.pattern_masking ?? "—")}
          </span>
        }
        title="frames and observations in this console are not masked in v1"
      />
      <Json value={policy} label="raw policy" />
    </Panel>
  );
}
