import { useMemo, useRef } from "react";
import { agentRows, fmtClock, fmtDur, fmtTokens, niceTicks, tickLabel, type LNode, type TimeView } from "../../lib/lineageGraph";
import { Empty } from "../ui";
import { usePanZoom, useWidth } from "./hooks";

const STRIP_H = 46;
const AXIS_H = 22;

/** One heartbeat strip per agent and model: busy time, idle gaps and tokens per minute on the shared time axis. */
export function Agents({ nodes, now, view, follow, cursor, zoom, pan }: {
  nodes: LNode[]; now: number; view: TimeView; follow: boolean; cursor: number | null;
  zoom: (factor: number, anchor: number) => void; pan: (dt: number) => void;
}) {
  const stripRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const width = Math.max(240, useWidth(stripRef));
  const buckets = Math.max(20, Math.min(120, Math.floor(width / 7)));
  const rows = useMemo(() => agentRows(nodes, cursor ?? now, view, buckets), [nodes, now, cursor, view, buckets]);
  const peak = Math.max(1, ...rows.map((r) => r.peak));
  const span = view.t1 - view.t0;
  const x = (t: number) => ((t - view.t0) / span) * width;
  const ticks = useMemo(() => niceTicks(view.t0, view.t1, Math.max(3, Math.floor(width / 100))), [view, width]);
  const { dragging } = usePanZoom(boxRef, { plotLeft: 0, plotWidth: width, view, zoom, pan });
  const at = cursor ?? now;

  if (!rows.length) return <Empty title="No agents ran in this window">Agents appear here as soon as an attempt starts.</Empty>;

  return (
    <div className={dragging ? "lin-agents grabbing" : "lin-agents"} ref={boxRef}>
      <div className="lin-agent-row lin-agent-axis" aria-hidden="true">
        <span />
        <div ref={stripRef}>
          <svg width={width} height={AXIS_H}>
            {ticks.map((t) => <text key={t.ts} x={x(t.ts)} y={15} textAnchor="middle" className="lin-tick">{tickLabel(t.ts, t.step)}</text>)}
          </svg>
        </div>
      </div>
      <ul className="lin-agent-list">
        {rows.map((r) => {
          const bw = width / buckets;
          const summary = `${r.label}: ${r.runningNow ? "running now" : "idle"}, busy ${fmtDur(r.busySeconds)} of ${fmtDur(span)} in view, ${r.tasks} attempts, ${r.gaps.length} idle gaps, on ${r.accounts.join(", ")}`;
          return (
            <li key={r.key} className="lin-agent-row">
              <div className="lin-agent-meta">
                <span className={r.runningNow ? "lin-beat on" : "lin-beat"} aria-hidden="true" />
                <div>
                  <b className="wrap-any">{r.label}</b>
                  <div className="quiet lin-agent-sub">{r.agent}{r.accounts.length ? ` · ${r.accounts.join(", ")}` : ""}</div>
                  <div className="quiet lin-agent-sub">{r.runningNow ? <span className="lin-live-text">running {r.runningIds.length > 1 ? `${r.runningIds.length} attempts` : r.runningIds[0]}</span> : "idle"} · {Math.min(100, Math.round((r.busySeconds / span) * 100))}% busy · {r.tasks} run{r.tasks === 1 ? "" : "s"}</div>
                </div>
              </div>
              <div className="lin-strip">
                <svg width={width} height={STRIP_H} role="img" aria-label={summary}>
                  <line x1={0} x2={width} y1={STRIP_H - 6} y2={STRIP_H - 6} className="lin-strip-base" />
                  {ticks.map((t) => <line key={t.ts} x1={x(t.ts)} x2={x(t.ts)} y1={0} y2={STRIP_H} className="lin-grid" />)}
                  {r.rate.map((v, i) => v > 0 && (
                    <rect key={i} x={i * bw + 0.5} width={Math.max(1, bw - 1)} y={STRIP_H - 6 - Math.max(2, (v / peak) * (STRIP_H - 20))} height={Math.max(2, (v / peak) * (STRIP_H - 20))} className="lin-rate" rx={1}>
                      <title>{`${fmtTokens(Math.round(v))} tokens/min around ${fmtClock(view.t0 + (i + 0.5) * (span / buckets), false, true)}`}</title>
                    </rect>
                  ))}
                  {r.gaps.map((g) => (
                    <g key={g.from} className="lin-gap">
                      <line x1={x(g.from)} x2={x(g.to)} y1={8} y2={8} />
                      {x(g.to) - x(g.from) > 64 && <text x={(x(g.from) + x(g.to)) / 2} y={6} textAnchor="middle">idle {fmtDur(g.to - g.from)}</text>}
                    </g>
                  ))}
                  {r.busy.map((b) => (
                    <rect key={b.from} x={x(b.from)} y={STRIP_H - 9} width={Math.max(2, x(b.to) - x(b.from))} height={6} rx={3} className="lin-busy" />
                  ))}
                  <g className={follow && cursor === null ? "lin-now follow" : "lin-now"}>
                    <line x1={x(at)} x2={x(at)} y1={0} y2={STRIP_H} />
                  </g>
                </svg>
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
