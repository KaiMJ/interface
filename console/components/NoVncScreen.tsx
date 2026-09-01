"use client";

/**
 * The live session, streamed from the automation container's X display: the same pixels
 * the agent is driving. `viewOnly` is the control token made visible — true while the
 * automation holds control, so an operator's clicks go nowhere rather than racing it;
 * false once they take over, reaching the same Chromium process and the same half-filled
 * form.
 *
 * Toggled rather than reconnected, because nothing about the session changed — only who
 * may touch it.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import type RFBType from "@novnc/novnc";

type Status = "connecting" | "connected" | "disconnected" | "error";

export function NoVncScreen({
  url,
  viewOnly,
  onStatus,
}: {
  url: string;
  viewOnly: boolean;
  onStatus?: (s: Status) => void;
}) {
  const host = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFBType | null>(null);
  const [status, setStatus] = useState<Status>("connecting");
  // Bumping this re-runs the connect effect. Without it a dropped VNC is permanent and
  // the panel freezes on its last frame; the session outlives any one run, so the view
  // of it has to outlive a network blip.
  const [attempt, setAttempt] = useState(0);

  const report = useCallback(
    (s: Status) => {
      setStatus(s);
      onStatus?.(s);
    },
    [onStatus],
  );

  useEffect(() => {
    let cancelled = false;

    (async () => {
      if (!host.current) return;
      try {
        // Dynamic import: noVNC touches `window` at module scope. The package
        // exposes a single export (core/rfb.js), so the bare specifier is the
        // supported path — deep imports break on >=1.7.
        const { default: RFB } = await import("@novnc/novnc");
        if (cancelled || !host.current) return;

        const ws = url.replace(/^http/, "ws") + "/websockify";
        // `shared` matters: x11vnc runs with -shared so the operator can attach
        // without evicting anyone. A non-shared client would disconnect the other.
        const conn = new RFB(host.current, ws, { shared: true });
        conn.viewOnly = viewOnly;
        conn.scaleViewport = true;
        conn.addEventListener("connect", () => report("connected"));
        conn.addEventListener("disconnect", () => report("disconnected"));
        rfb.current = conn;
      } catch {
        if (!cancelled) report("error");
      }
    })();

    return () => {
      cancelled = true;
      rfb.current?.disconnect();
      rfb.current = null;
    };
    // Deliberately does NOT depend on `viewOnly` — see the effect below. Changing
    // who holds control must not reconnect the session.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, attempt]);

  // Retry on its own, slowly. The common causes — a restarted container, a websocket
  // dropped while nothing was watching — fix themselves, and nothing is lost by
  // reconnecting a moment late.
  useEffect(() => {
    if (status === "connected" || status === "connecting") return;
    const t = setTimeout(() => setAttempt((n) => n + 1), 3000);
    return () => clearTimeout(t);
  }, [status, attempt]);

  useEffect(() => {
    if (rfb.current) rfb.current.viewOnly = viewOnly;
  }, [viewOnly]);

  return (
    <div className="relative h-full w-full bg-black">
      <div ref={host} className="h-full w-full" />

      {status !== "connected" ? (
        <div className="absolute inset-0 flex items-center justify-center bg-black/70 text-center">
          <div className="mono text-[var(--muted)]">
            {status === "connecting" ? "connecting to live session…" : null}
            {status === "disconnected" ? "session disconnected — retrying" : null}
            {status === "error" ? (
              <>
                no live session at {url}
                <div className="mt-1 text-[11px]">start the stack with `docker compose up`</div>
              </>
            ) : null}
            {status !== "connecting" ? (
              <div className="mt-2">
                <button className="btn" onClick={() => setAttempt((n) => n + 1)}>
                  reconnect now
                </button>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <div
        className="mono absolute top-2 right-2 rounded px-2 py-0.5 text-[11px]"
        style={{
          background: viewOnly ? "#232b34" : "#5a2b28",
          border: `1px solid ${viewOnly ? "var(--rule)" : "#8c4340"}`,
        }}
      >
        {viewOnly ? "watching — automation has control" : "YOU HAVE CONTROL"}
      </div>
    </div>
  );
}
