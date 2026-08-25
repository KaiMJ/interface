"use client";

/**
 * The chrome: a drawer, a modal, a tab strip, a collapsible section. All four exist
 * so that everything which is not the run you are looking at now can be put away.
 */

import { useEffect } from "react";

/** Slide-over from the right. For things you consult, not things you watch. */
export function Drawer({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <>
      <button
        aria-label="close"
        className="fixed inset-0 z-20 cursor-default bg-black/50"
        onClick={onClose}
      />
      <aside className="fixed top-0 right-0 z-30 flex h-full w-[min(420px,90vw)] flex-col border-l border-[var(--rule)] bg-[var(--bg)]">
        <header className="flex items-center justify-between border-b border-[var(--rule)] px-3 py-2">
          <span className="mono text-[11px] tracking-wider text-[var(--muted)] uppercase">
            {title}
          </span>
          <button className="btn" onClick={onClose}>
            close
          </button>
        </header>
        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3">{children}</div>
      </aside>
    </>
  );
}

/** Centred modal. For the one thing that starts work. */
export function Modal({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  useEscape(open, onClose);
  if (!open) return null;
  return (
    <>
      <button
        aria-label="close"
        className="fixed inset-0 z-20 cursor-default bg-black/50"
        onClick={onClose}
      />
      <div className="fixed top-1/2 left-1/2 z-30 w-[min(520px,92vw)] -translate-x-1/2 -translate-y-1/2">
        <div className="panel max-h-[86vh] overflow-y-auto">
          <div className="panel-hd flex items-center justify-between">
            <span>{title}</span>
            <button className="btn" onClick={onClose}>
              esc
            </button>
          </div>
          {children}
        </div>
      </div>
    </>
  );
}

/** One tab strip, used by the inspector and the navigator. */
export function Tabs<T extends string>({
  tabs,
  active,
  onSelect,
  counts,
}: {
  tabs: readonly T[];
  active: T;
  onSelect: (tab: T) => void;
  counts?: Partial<Record<T, number | string>>;
}) {
  return (
    <div className="flex shrink-0 gap-px border-b border-[var(--rule)]">
      {tabs.map((tab) => {
        const on = tab === active;
        return (
          <button
            key={tab}
            onClick={() => onSelect(tab)}
            className="mono px-2.5 py-1.5 text-[11px] tracking-wide whitespace-nowrap uppercase"
            style={{
              color: on ? "var(--text)" : "var(--muted)",
              borderBottom: `2px solid ${on ? "var(--accent)" : "transparent"}`,
              background: on ? "#1f262e" : "transparent",
            }}
          >
            {tab}
            {counts?.[tab] !== undefined ? (
              <span className="ml-1 opacity-60">{counts[tab]}</span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function useEscape(open: boolean, onClose: () => void): void {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
}
