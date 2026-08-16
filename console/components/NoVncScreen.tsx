"use client";

/**
 * The live session, streamed from the automation container's X display.
 *
 * This component is the handoff. Not a mock of one — the same pixels the agent is
 * driving, over the same VNC connection, and `viewOnly` is the control token made
 * visible:
 *
 *   viewOnly = true    automation holds control. The operator watches. Their
 *                      clicks go nowhere, which is the point: an operator who can
 *                      click during an automated run races the agent on a live
 *                      banking screen.
 *   viewOnly = false   the operator holds control. Input goes through websockify
 *                      -> x11vnc -> Xvfb, into the same Chromium process, with the
 *                      same cookies and the same half-filled form.
 *
 * Toggling this property is cheaper and more honest than tearing down and
 * re-establishing the connection, because it makes clear that nothing about the
 * session changed — only who is allowed to touch it.
 *
 * The noVNC library is used rather than an iframe to `vnc.html` so that view-only
 * state is ours to control rather than a URL parameter the operator could edit,
 * and so connection state is observable here.
 */

import { useEffect, useRef, useState } from "react";
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

  function report(s: Status) {
    setStatus(s);
    onStatus?.(s);
  }

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
  }, [url]);

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
            {status === "disconnected" ? "session disconnected" : null}
            {status === "error" ? (
              <>
                no live session at {url}
                <div className="mt-1 text-[11px]">start the stack with `docker compose up`</div>
              </>
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
