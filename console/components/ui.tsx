"use client";

/**
 * The console's vocabulary: panels, labelled fields, status marks, raw JSON. The same
 * four shapes carry every panel, so a status reads the same way everywhere.
 */

import { useState } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
  bodyClassName = "",
  tone,
}: {
  title: React.ReactNode;
  right?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  bodyClassName?: string;
  tone?: "warn" | "err";
}) {
  const border = tone === "warn" ? "var(--warn)" : tone === "err" ? "var(--err)" : undefined;
  return (
    <div className={`panel ${className}`} style={border ? { borderColor: border } : undefined}>
      <div className="panel-hd flex items-center justify-between gap-2">
        <span style={border ? { color: border } : undefined}>{title}</span>
        {right ? <span className="flex items-center gap-2">{right}</span> : null}
      </div>
      <div className={bodyClassName}>{children}</div>
    </div>
  );
}

export function Field({
  label,
  value,
  title,
  mono,
}: {
  label: string;
  value?: React.ReactNode;
  title?: string;
  mono?: boolean;
}) {
  return (
    <div className="flex gap-2" title={title}>
      <span className="mono w-[92px] shrink-0 text-[var(--muted)]">{label}</span>
      <span className={`min-w-0 break-words ${mono ? "mono" : ""}`}>{value ?? "—"}</span>
    </div>
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return <p className="p-3 text-[12px] text-[var(--muted)]">{children}</p>;
}

const COLORS: Record<string, string> = {
  ok: "var(--ok)",
  success: "var(--ok)",
  matched: "var(--ok)",
  approved: "var(--ok)",
  kept: "var(--ok)",
  allow: "var(--ok)",
  recovered: "var(--warn)",
  confirm: "var(--warn)",
  kept_without_checkpoint: "var(--warn)",
  draft: "var(--warn)",
  escalated: "var(--warn)",
  miss: "var(--warn)",
  skipped: "var(--muted)",
  unset: "var(--muted)",
  business_outcome: "var(--accent)",
  running: "var(--muted)",
  failed: "var(--err)",
  failure: "var(--err)",
  denied: "var(--err)",
  rejected: "var(--err)",
  discarded: "var(--err)",
};

export function statusColor(status: string): string {
  return COLORS[status] ?? "var(--muted)";
}

/**
 * A run in flight reads as `running`, not as its eventual failure.
 *
 * The engine writes its result to evidence before every step, so the initial status has
 * to mean "not finished" rather than a terminal class. The hollow mark distinguishes it
 * from the solid terminal states.
 */
export function StatusDot({ status, label = true }: { status: string; label?: boolean }) {
  const live = status === "running";
  return (
    <span
      className={`mono text-[11px]${live ? " animate-pulse" : ""}`}
      style={{ color: statusColor(status) }}
    >
      {live ? "◌" : "●"}
      {label ? ` ${status}` : ""}
    </span>
  );
}

export function Chip({
  children,
  tone,
  title,
}: {
  children: React.ReactNode;
  tone?: string;
  title?: string;
}) {
  return (
    <span
      className="mono rounded-sm border px-1.5 py-px text-[10px] whitespace-nowrap"
      title={title}
      style={{
        color: tone ? statusColor(tone) : "var(--muted)",
        borderColor: "var(--rule)",
        background: "#1f262e",
      }}
    >
      {children}
    </span>
  );
}

/**
 * The escape hatch: every panel here is an opinionated reading of a record on disk, and
 * a reading is what you stop trusting when debugging what produced it. Collapsed by
 * default, one click from the bytes.
 */
export function Json({ value, label = "raw" }: { value: unknown; label?: string }) {
  const [open, setOpen] = useState(false);
  if (value === null || value === undefined) return null;
  return (
    <div className="mt-2">
      <button className="btn" onClick={() => setOpen((o) => !o)}>
        {open ? `hide ${label}` : label}
      </button>
      {open ? (
        <pre className="mono mt-1 max-h-[320px] overflow-auto rounded border border-[var(--rule)] bg-[#10151a] p-2 text-[11px] whitespace-pre-wrap">
          {JSON.stringify(value, null, 2)}
        </pre>
      ) : null}
    </div>
  );
}

export function ms(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return value < 1000 ? `${value}ms` : `${(value / 1000).toFixed(1)}s`;
}
