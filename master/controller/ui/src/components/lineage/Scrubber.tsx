import { useEffect, useMemo, useRef, useState } from "react";
import { describeEvent, fmtClock, type LEvent, type LNode } from "../../lib/lineageGraph";
import { Button } from "../ui";

/**
 * The shared replay control. `cursor === null` means live. Stepping jumps between recorded events; play advances
 * through them on a timer. The range input is the same position as a continuous scrubber.
 */
export function Scrubber({ t0, t1, steps, events, nodes, cursor, setCursor }: {
  t0: number; t1: number; steps: number[]; events: LEvent[]; nodes: LNode[];
  cursor: number | null; setCursor: (t: number | null) => void;
}) {
  const [playing, setPlaying] = useState(false);
  const cur = useRef(cursor);
  cur.current = cursor;
  const stepsRef = useRef(steps);
  stepsRef.current = steps;

  const nextStep = (from: number): number | null => stepsRef.current.find((t) => t > from + 1e-6) ?? null;
  const prevStep = (from: number): number | null => [...stepsRef.current].reverse().find((t) => t < from - 1e-6) ?? null;

  useEffect(() => {
    if (!playing) return;
    const id = setInterval(() => {
      const from = cur.current ?? t0 - 1;
      const n = nextStep(from);
      if (n === null) { setPlaying(false); setCursor(null); return; }
      setCursor(n);
    }, 700);
    return () => clearInterval(id);
  }, [playing, t0, setCursor]);

  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const last = useMemo(() => {
    if (cursor === null) return null;
    return [...events].reverse().find((e) => e.ts <= cursor) ?? null;
  }, [events, cursor]);

  const value = cursor ?? t1;
  const disabled = steps.length === 0;
  return (
    <div className="lin-scrub" role="group" aria-label="Replay">
      <div className="lin-scrub-row">
        <Button size="sm" disabled={disabled} onClick={() => { setPlaying(false); const p = prevStep(cursor ?? t1 + 1); if (p !== null) setCursor(p); }} aria-label="Previous event">‹ Step</Button>
        <Button size="sm" disabled={disabled} onClick={() => { if (playing) setPlaying(false); else { if (cursor === null) setCursor(t0 - 1); setPlaying(true); } }} aria-pressed={playing}>{playing ? "Pause" : "Replay"}</Button>
        <Button size="sm" disabled={disabled || cursor === null} onClick={() => { setPlaying(false); const n = nextStep(cursor ?? t1); setCursor(n); }} aria-label="Next event">Step ›</Button>
        <input
          type="range" className="lin-range" min={t0} max={Math.max(t1, t0 + 1)} step={1} value={Math.min(Math.max(value, t0), Math.max(t1, t0 + 1))}
          aria-label="Replay position" aria-valuetext={cursor === null ? "Live" : `as of ${fmtClock(cursor, false, true)}`}
          onChange={(e) => { setPlaying(false); const v = Number(e.target.value); setCursor(v >= t1 ? null : v); }}
        />
        <span className="lin-scrub-clock mono" aria-live="off">{cursor === null ? "Live" : fmtClock(cursor, false, true)}</span>
        <Button size="sm" variant={cursor === null ? "ghost" : "primary"} disabled={cursor === null} onClick={() => { setPlaying(false); setCursor(null); }}>Back to live</Button>
      </div>
      {cursor !== null && (
        <p className="lin-scrub-note" role="status">
          Replaying: showing the fleet as it was at {fmtClock(cursor, false, true)}.{last ? <> Last event: <span className="mono">{describeEvent(last, byId)}</span> at {fmtClock(last.ts, false, true)}.</> : null}
        </p>
      )}
    </div>
  );
}
