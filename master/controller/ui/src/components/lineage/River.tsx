import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { STATE } from "../../lib/format";
import {
  fmtClock, fmtDur, fmtTokens, kindGlyph, layoutRiver, MARK_LABEL, marksFor, niceTicks, spanOf, tickLabel, tokenTotal,
  waitReason, GROUP_H, ROW_H, type AnomalyKind, type LEdge, type LEvent, type LLane, type LNode, type TimeView,
} from "../../lib/lineageGraph";
import { useMedia, usePanZoom, useWidth } from "./hooks";

const BAR_H = 22;
const AXIS_H = 30;
const MIN_W = 880;

export const toneOf = (state: string): string => STATE[state as keyof typeof STATE]?.tone ?? "quiet";
const LINK_TONE: Record<string, string> = { retry: "warn", fix: "warn", review: "info", merge: "ok", handoff: "accent", resume: "accent", check: "quiet" };

interface Card { id: string; x: number; y: number }

/** The fleet river: swimlanes per node and account over one time axis. Hand-written SVG. */
export function River({ nodes, edges, lanes, events, now, view, follow, cursor, flags, selected, onOpen, zoom, pan }: {
  nodes: LNode[]; edges: LEdge[]; lanes: LLane[]; events: LEvent[]; now: number; view: TimeView; follow: boolean;
  cursor: number | null; flags: Map<string, AnomalyKind[]>; selected?: string | null;
  onOpen: (n: LNode) => void; zoom: (factor: number, anchor: number) => void; pan: (dt: number) => void;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const boxW = useWidth(scrollRef);
  const compact = useMedia("(max-width: 639px)");
  const labelW = compact ? 112 : 184;
  const W = Math.max(boxW, MIN_W);
  const plotL = labelW;
  const plotW = Math.max(200, W - labelW - 14);
  const span = view.t1 - view.t0;
  const x = (t: number) => plotL + ((t - view.t0) / span) * plotW;
  const [card, setCard] = useState<Card | null>(null);
  const [hot, setHot] = useState<string | null>(null);

  const layout = useMemo(() => layoutRiver(nodes, lanes, now, span / plotW * 8), [nodes, lanes, now, span, plotW]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const ticks = useMemo(() => niceTicks(view.t0, view.t1, Math.max(3, Math.floor(plotW / 110))), [view, plotW]);
  const { justDragged, dragging } = usePanZoom(svgRef, { plotLeft: plotL, plotWidth: plotW, view, zoom, pan });

  const H = AXIS_H + layout.height + 8;
  const yOf = (id: string) => AXIS_H + layout.place[id].y;
  const edgeKeyHot = (e: LEdge) => hot !== null && (e.from === hot || e.to === hot);

  function show(n: LNode, el: Element) {
    const wrap = wrapRef.current?.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (!wrap) return;
    setCard({ id: n.id, x: Math.min(Math.max(r.left - wrap.left + r.width / 2, 130), Math.max(130, wrap.width - 130)), y: r.bottom - wrap.top + 8 });
    setHot(n.id);
  }
  const hide = () => { setCard(null); setHot(null); };
  const open = (n: LNode) => { if (!justDragged.current) onOpen(n); };
  const onKey = (n: LNode) => (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(n); }
  };

  const nowX = x(cursor ?? now);
  const cardNode = card ? byId.get(card.id) : undefined;

  return (
    <div className="lin-river" ref={wrapRef}>
      <div className="lin-scroll" ref={scrollRef}>
        <svg
          ref={svgRef} className={dragging ? "lin-svg grabbing" : "lin-svg"} width={W} height={H} viewBox={`0 0 ${W} ${H}`}
          role="group" aria-label={`Fleet river: ${nodes.length} attempts on ${layout.groups.reduce((a, g) => a + g.lanes.length, 0)} lanes`}
        >
          <defs>
            <pattern id="lin-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="6" className="lin-hatch-line" />
            </pattern>
            {["warn", "info", "ok", "accent", "quiet"].map((t) => (
              <marker key={t} id={`lin-arrow-${t}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M0 0L8 4L0 8z" className={`lin-arrow t-${t}`} />
              </marker>
            ))}
            <clipPath id="lin-plot"><rect x={plotL} y={0} width={plotW} height={H} /></clipPath>
          </defs>

          {/* node bands and lanes */}
          {layout.groups.map((g) => (
            <g key={g.node}>
              <rect x={0} y={AXIS_H + g.y} width={W} height={GROUP_H} className="lin-group-bg" />
              <text x={12} y={AXIS_H + g.y + GROUP_H / 2 + 4} className="lin-group-label">{g.node}</text>
              {g.lanes.map((l) => (
                <g key={l.id}>
                  <line x1={0} x2={W} y1={AXIS_H + l.y + l.h} y2={AXIS_H + l.y + l.h} className="lin-lane-line" />
                  <title>{`${g.node} · ${l.account}`}</title>
                </g>
              ))}
            </g>
          ))}

          {/* time grid */}
          <g clipPath="url(#lin-plot)" aria-hidden="true">
            {ticks.map((t) => (
              <line key={t.ts} x1={x(t.ts)} x2={x(t.ts)} y1={AXIS_H - 4} y2={H} className="lin-grid" />
            ))}
          </g>

          {/* lane labels sit above the grid, in their own column */}
          {layout.groups.flatMap((g) => g.lanes.map((l) => (
            <g key={l.id} className="lin-lane-label">
              <rect x={0} y={AXIS_H + l.y} width={labelW - 4} height={l.h} className="lin-label-bg" />
              <text x={12} y={AXIS_H + l.y + 18} className="lin-lane-name">{truncate(l.account, compact ? 14 : 24)}</text>
              <text x={12} y={AXIS_H + l.y + 32} className="lin-lane-sub">{l.rows} row{l.rows > 1 ? "s" : ""} · {nodes.filter((n) => n.lane === l.id).length} runs</text>
            </g>
          )))}

          {/* axis */}
          <g aria-hidden="true">
            <rect x={0} y={0} width={W} height={AXIS_H - 4} className="lin-axis-bg" />
            {ticks.map((t) => (
              <text key={t.ts} x={x(t.ts)} y={17} textAnchor="middle" className="lin-tick">{tickLabel(t.ts, t.step)}</text>
            ))}
          </g>

          <g clipPath="url(#lin-plot)">
            {/* lineage edges */}
            <g className="lin-edges">
              {edges.map((e) => {
                const p = byId.get(e.from), c = byId.get(e.to);
                if (!p || !c || !layout.place[p.id] || !layout.place[c.id]) return null;
                const ps = spanOf(p, now), cs = spanOf(c, now);
                const x1 = x(ps.end), y1 = yOf(p.id) + BAR_H / 2 + 3;
                const x2 = x(cs.created), y2 = yOf(c.id) + BAR_H / 2 + 3;
                if (Math.max(x1, x2) < plotL - 10 || Math.min(x1, x2) > plotL + plotW + 10) return null;
                const dx = Math.min(140, Math.max(26, Math.abs(x2 - x1) * 0.5));
                const tone = LINK_TONE[e.link] ?? "quiet";
                const label = e.reason && e.reason !== e.link ? `${e.link} · ${e.reason.replace("_", " ")}` : e.link;
                const wide = Math.abs(x2 - x1) > 84 || hot !== null && edgeKeyHot(e);
                return (
                  <g key={`${e.from}>${e.to}`} className={`lin-edge t-${tone}${edgeKeyHot(e) ? " hot" : ""}`}>
                    <path d={`M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`} markerEnd={`url(#lin-arrow-${tone})`} />
                    {wide && <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 5} textAnchor="middle" className="lin-edge-label">{label}</text>}
                  </g>
                );
              })}
            </g>

            {/* attempts */}
            {nodes.map((n) => {
              const s = spanOf(n, cursor ?? now);
              const pl = layout.place[n.id];
              if (!pl) return null;
              const y = AXIS_H + pl.y + (ROW_H - BAR_H) / 2;
              const xs = x(s.created), xb = s.start !== null ? x(s.start) : null, xe = x(s.end);
              if (xe < plotL - 4 || xs > plotL + plotW + 4) return null;
              const barW = xb !== null ? Math.max(6, xe - xb) : 0;
              const tone = toneOf(n.state);
              const marks = marksFor(n.id, events).filter((m) => cursor === null || m.ts <= cursor);
              const flagged = flags.get(n.id);
              const label = `${n.id}, ${n.kind ?? "task"}, ${STATE[n.state as keyof typeof STATE]?.label ?? n.state}, ${n.model ?? n.agent ?? ""}, on ${n.node}. Press Enter to open the trace.`;
              const showTitle = barW > 96;
              return (
                <g
                  key={n.id} tabIndex={0} role="link" aria-label={label} data-id={n.id}
                  className={`lin-bar t-${tone}${n.id === selected ? " sel" : ""}${s.live && s.start !== null ? " live" : ""}${hot === n.id ? " hot" : ""}`}
                  onClick={() => open(n)} onKeyDown={onKey(n)}
                  onPointerEnter={(e) => show(n, e.currentTarget)} onPointerLeave={hide}
                  onFocus={(e) => show(n, e.currentTarget)} onBlur={hide}
                >
                  {s.wait > 0 && (
                    <rect x={xs} y={y + 3} width={Math.max(2, (xb ?? xe) - xs)} height={BAR_H - 6} className="lin-wait" rx={3} />
                  )}
                  {xb !== null && (
                    <>
                      <rect x={xb} y={y} width={barW} height={BAR_H} rx={5} className="lin-bar-body" />
                      <rect x={xb} y={y} width={Math.min(4, barW)} height={BAR_H} rx={2} className="lin-bar-edge" />
                      {barW > 38 && (
                        <g className="lin-glyph" transform={`translate(${xb + 9} ${y + 4})`}>
                          <rect width={14} height={14} rx={3} />
                          <text x={7} y={10.5} textAnchor="middle">{kindGlyph(n.kind)}</text>
                        </g>
                      )}
                      {showTitle && (
                        <text x={xb + 28} y={y + 15} className="lin-bar-text">{truncate(`${n.id.replace(/^t-/, "")} ${n.model ?? n.agent ?? ""}`, Math.floor((barW - 34) / 6.4))}</text>
                      )}
                    </>
                  )}
                  {xb === null && <rect x={xs} y={y + 3} width={Math.max(2, xe - xs)} height={BAR_H - 6} rx={3} className="lin-wait-end" />}
                  {marks.map((m, i) => (
                    <g key={`${m.kind}${m.ts}${i}`} className={`lin-diamond d-${m.kind}`} transform={`translate(${x(m.ts)} ${y - 1})`}>
                      <path d="M0 -6L6 0L0 6L-6 0z" />
                      <title>{`${MARK_LABEL[m.kind]} at ${fmtClock(m.ts, false, true)}`}</title>
                    </g>
                  ))}
                  {flagged && (
                    <g className="lin-flag" transform={`translate(${Math.max(xe, (xb ?? xs) + 14) - 2} ${y + 2})`}>
                      <circle r={7} cy={4} />
                      <text y={8} textAnchor="middle">!</text>
                      <title>{`Anomaly: ${flagged.join(", ").replace("_", " ")}`}</title>
                    </g>
                  )}
                  <rect x={Math.min(xs, xb ?? xs) - 2} y={y - 3} width={Math.max(12, xe - Math.min(xs, xb ?? xs)) + 4} height={BAR_H + 6} className="lin-hit" />
                </g>
              );
            })}

            {/* now line */}
            <g className={follow && cursor === null ? "lin-now follow" : "lin-now"} aria-hidden="true">
              <line x1={nowX} x2={nowX} y1={AXIS_H - 4} y2={H} />
              <g transform={`translate(${nowX} ${AXIS_H - 4})`}>
                <rect x={-17} y={-13} width={34} height={13} rx={3} />
                <text y={-3} textAnchor="middle">{cursor === null ? "now" : fmtClock(cursor, false, true).slice(0, 5)}</text>
              </g>
            </g>
          </g>
        </svg>
      </div>

      {card && cardNode && <HoverCard node={cardNode} now={cursor ?? now} events={events} x={card.x} y={card.y} />}
    </div>
  );
}

function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, Math.max(1, max - 1))}…` : s;
}

export function HoverCard({ node: n, now, events, x, y }: { node: LNode; now: number; events: LEvent[]; x: number; y: number }) {
  const s = spanOf(n, now);
  const marks = marksFor(n.id, events);
  const total = tokenTotal(n.tokens);
  return (
    <div className="lin-card" role="tooltip" style={{ left: x, top: y }}>
      <div className="lin-card-head">
        <span className="mono">{n.id}</span>
        <span className={`state tone-${toneOf(n.state)}`}>{STATE[n.state as keyof typeof STATE]?.label ?? n.state}</span>
      </div>
      {n.title && <div className="lin-card-title">{n.title}</div>}
      <dl>
        <dt>Kind</dt><dd>{n.kind ?? "task"}{n.stage ? ` · ${n.stage}` : ""}</dd>
        <dt>Agent</dt><dd>{n.model ?? n.agent ?? "—"}</dd>
        <dt>Lane</dt><dd>{n.node} · {n.account}</dd>
        {n.link && <><dt>Link</dt><dd>{n.link}{n.parent_id ? ` from ${n.parent_id}` : ""}</dd></>}
        {s.wait > 0 && <><dt>Queued</dt><dd>{fmtDur(s.wait)} <span className="quiet">({waitReason(n)})</span></dd></>}
        {s.start !== null && <><dt>Ran</dt><dd>{fmtDur(s.end - s.start)}{s.live ? " so far" : ""}</dd></>}
        {total > 0 && <><dt>Tokens</dt><dd>{fmtTokens(total)}</dd></>}
        {marks.length > 0 && <><dt>Marks</dt><dd>{[...new Set(marks.map((m) => MARK_LABEL[m.kind]))].join(", ")}</dd></>}
      </dl>
      <div className="quiet lin-card-foot">Enter or click opens the trace</div>
    </div>
  );
}
