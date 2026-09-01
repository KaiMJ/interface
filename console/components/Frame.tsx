"use client";

/**
 * What the agent was looking at, and what it made of it. Three layers over one
 * image: the capture an operator would have seen over VNC; the numbered marks the
 * model was actually shown; and every box perception found, with role, source and
 * confidence.
 *
 * Coordinates are normalised 0..1 of the recording viewport, so they overlay any
 * rendered size.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Chip, ms } from "./ui";
import { StepLine } from "./RunView";
import {
  api,
  evidenceUrl,
  isFailed,
  type Bbox,
  type EvidenceStep,
  type Observation,
  type Run,
  type StepRow,
} from "@/lib/api";

type Layer = "capture" | "marks" | "elements" | "after";

const SOURCE_COLOR: Record<string, string> = {
  ocr: "#5aa9e6",
  omniparser: "#d8a13a",
  dom: "#56b56a",
  ax: "#a06fd0",
};

export function Frame({
  runId,
  frame,
  step,
  failure,
}: {
  runId: string | null;
  frame: EvidenceStep | null;
  step: StepRow | null;
  failure?: Run["failure"];
}) {
  const [layer, setLayer] = useState<Layer>("marks");
  const [observation, setObservation] = useState<Observation | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const wrapRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const [paint, setPaint] = useState<React.CSSProperties | null>(null);

  // The observation is a per-step file, fetched on demand: loading every step's
  // elements up front would pull a megabyte of boxes to render one frame.
  useEffect(() => {
    setObservation(null);
    setHover(null);
    if (!runId || !frame?.observation) return;
    let live = true;
    void api.observation(runId, frame.observation).then((o) => {
      if (live) setObservation(o);
    });
    return () => {
      live = false;
    };
  }, [runId, frame?.observation]);

  // Where the image actually lands inside its box. The overlays are percentages of
  // the *displayed* frame, so they need the displayed frame's rectangle rather than
  // the element containing it — a wrapper clamped to the scroll area while the image
  // keeps its own height scales every overlay against a shorter frame. Measured, so a
  // layout change cannot quietly reintroduce it.
  const measure = useCallback(() => {
    const wrap = wrapRef.current;
    const img = imgRef.current;
    if (!wrap || !img) return;
    const { naturalWidth: nw, naturalHeight: nh } = img;
    const box = img.getBoundingClientRect();
    const origin = wrap.getBoundingClientRect();
    if (!nw || !nh || !box.width || !box.height) return setPaint(null);
    // Sub-pixel on purpose: offsetWidth and friends round, and half a pixel of rounding
    // misplaces where the agent clicked.
    const scale = Math.min(box.width / nw, box.height / nh); // what object-contain did
    const w = nw * scale;
    const h = nh * scale;
    setPaint({
      left: box.left - origin.left + (box.width - w) / 2,
      top: box.top - origin.top + (box.height - h) / 2,
      width: w,
      height: h,
    });
  }, []);

  // A step that failed *after* acting has its reason on the after-frame — the
  // permission denial, the application error, the screen the checkpoint did not find.
  // Defaulting to the pre-action capture there shows the screen from before the thing
  // went wrong. Keyed on the step, so it re-decides on selection and still leaves the
  // tabs under the operator's hand.
  useEffect(() => {
    setLayer(step && isFailed(step.status) && frame?.after ? "after" : "marks");
  }, [frame?.step_id, frame?.after, step?.status, step]);

  // Fall back to the clean capture when a run has no overlay — replay runs never
  // write one, because no model was shown anything — or no after-frame, which a
  // step that produced no effect will not have.
  const missing =
    (layer === "marks" && !frame?.annotated) || (layer === "after" && !frame?.after);
  const showing: Layer = missing ? "capture" : layer;
  const src = !frame || !runId
    ? null
    : evidenceUrl(
        runId,
        showing === "marks" && frame.annotated
          ? frame.annotated
          : showing === "after" && frame.after
            ? frame.after
            : frame.frame,
      );

  useEffect(() => {
    const wrap = wrapRef.current;
    const img = imgRef.current;
    if (!wrap || !img) return;
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(wrap);
    ro.observe(img);
    return () => ro.disconnect();
  }, [measure, src]);

  const failureBox =
    failure?.region && frame && failure.step_id === frame.step_id ? failure.region : null;
  const targetBox = step?.resolution_trace?.bbox ?? null;
  const needle = filter.trim().toLowerCase();
  const elements = (observation?.elements ?? []).filter(
    (e) =>
      !needle ||
      (e.text ?? "").toLowerCase().includes(needle) ||
      (e.name ?? "").toLowerCase().includes(needle) ||
      (e.role ?? "").toLowerCase().includes(needle) ||
      e.id === needle,
  );
  const hovered = observation?.elements.find((e) => e.id === hover) ?? null;

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--rule)] px-3 py-1.5 text-[12px]">
        <StepLine step={step} />
        <span className="ml-auto flex shrink-0 items-center gap-1">
          {showing === "elements" && observation ? (
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="filter"
              className="mono w-[90px] rounded border border-[var(--rule)] bg-[#10151a] px-1.5 py-0.5 text-[11px] outline-none focus:border-[var(--accent)]"
            />
          ) : null}
          {(["capture", "marks", "elements", "after"] as Layer[]).map((l) => (
            <button
              key={l}
              className="btn"
              disabled={
                (l === "marks" && !frame?.annotated) || (l === "after" && !frame?.after)
              }
              title={
                l === "marks" && !frame?.annotated
                  ? "no overlay — replay runs show the model nothing"
                  : l === "after"
                    ? "what this step produced. The three other layers are the screen it acted on."
                    : `show the ${l}`
              }
              style={
                showing === l ? { borderColor: "var(--accent)" } : { color: "var(--muted)" }
              }
              onClick={() => setLayer(l)}
            >
              {l}
            </button>
          ))}
        </span>
      </div>
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-hidden bg-black">
        {src && frame ? (
          <div ref={wrapRef} className="relative flex h-full w-full items-center justify-center">
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              ref={imgRef}
              src={src}
              alt={`step ${frame.step_id}`}
              onLoad={measure}
              className="max-h-full max-w-full object-contain"
            />

            {/* One layer, laid over exactly the pixels of the frame. Everything
                inside it is a percentage of the recording viewport, which is only
                true of this rectangle. */}
            <div className="pointer-events-none absolute" style={paint ?? { display: "none" }}>
              {showing === "elements"
                ? elements.map((el) => (
                    <span
                      key={el.id}
                      onMouseEnter={() => setHover(el.id)}
                      onMouseLeave={() => setHover((h) => (h === el.id ? null : h))}
                      className="pointer-events-auto absolute cursor-crosshair"
                      style={{
                        ...place(el.bbox),
                        border: `1px solid ${SOURCE_COLOR[el.source] ?? "#8593a2"}`,
                        background: hover === el.id ? "rgba(90,169,230,0.25)" : "transparent",
                      }}
                      title={`${el.id} ${el.role ?? "?"} · ${el.source} ${el.conf.toFixed(2)}\n${
                        el.text ?? el.name ?? ""
                      }`}
                    />
                  ))
                : null}

              {/* Where the resolver landed. Drawn on every layer: "the anchor
                  matched" and "the click went here" are only the same claim if you
                  can see them in the same place. */}
              {targetBox && showing !== "after" ? (
                <span
                  className="absolute border-2 border-[var(--accent)]"
                  style={place(targetBox)}
                  title={`resolved via ${step?.resolution}`}
                />
              ) : null}

              {failureBox ? (
                <span
                  className="absolute border-2 border-[var(--err)]"
                  style={{ ...place(failureBox), boxShadow: "0 0 0 9999px rgba(0,0,0,0.45)" }}
                  title={`${failure?.kind}: ${failure?.observed ?? ""}`}
                />
              ) : null}
            </div>
          </div>
        ) : (
          <p className="p-6 text-[12px] text-[var(--muted)]">
            No frame for this step. Evidence is written per step as a run proceeds.
          </p>
        )}
      </div>

      {/* Only what this frame can say for itself. What the *step* did is in the
          inspector below; repeating it here is how a footer becomes a wall. */}
      <div className="flex items-center gap-2 border-t border-[var(--rule)] px-3 py-1 text-[11px]">
        {step ? (
          <Chip
            tone={step.resolution === "recorded_bbox" ? "recovered" : undefined}
            title="which tier of the resolver ladder produced the coordinate"
          >
            {step.resolution}
          </Chip>
        ) : null}
        {step?.duration_ms ? <Chip>{ms(step.duration_ms)}</Chip> : null}
        {observation ? (
          <Chip title="elements perception found on this frame">
            {needle ? `${elements.length}/` : ""}
            {observation.elements.length} elements
          </Chip>
        ) : null}
        {hovered ? (
          <span className="mono ml-auto min-w-0 truncate" style={{ color: SOURCE_COLOR[hovered.source] }}>
            {hovered.id} · {hovered.role ?? "?"} · {hovered.source} {hovered.conf.toFixed(2)} ·{" "}
            {hovered.text ?? hovered.name ?? "—"}
          </span>
        ) : observation?.url ? (
          <span className="mono ml-auto min-w-0 truncate text-[var(--muted)]">{observation.url}</span>
        ) : null}
      </div>
    </div>
  );
}

function place(b: Bbox): React.CSSProperties {
  return {
    left: `${b.x * 100}%`,
    top: `${b.y * 100}%`,
    width: `${b.w * 100}%`,
    height: `${b.h * 100}%`,
  };
}
