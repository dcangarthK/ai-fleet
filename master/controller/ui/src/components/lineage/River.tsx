import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { Link } from "react-router-dom";
import { STATE } from "../../lib/format";
import {
  focusSet, fmtClock, fmtDur, fmtTokens, gateLabel, kindGlyph, layoutRiver, MARK_LABEL, marksFor, niceTicks, spanOf, tickLabel, tokenTotal,
  waitReason, GROUP_H, ROW_H, type AnomalyKind, type LEdge, type LEvent, type LLane, type LNode, type TimeView,
} from "../../lib/lineageGraph";
import { useMedia, usePanZoom, useWidth } from "./hooks";

const BAR_H = 28;
const AXIS_H = 30;
const MIN_W = 880;

export const toneOf = (state: string): string => STATE[state as keyof typeof STATE]?.tone ?? "quiet";
const LINK_TONE: Record<string, string> = { retry: "warn", fix: "warn", review: "info", merge: "ok", handoff: "accent", resume: "accent", check: "quiet" };

interface Card { id: string; x: number; y: number }

/** The fleet river: swimlanes per node and account over one time axis. Hand-written SVG. */
export function River({ nodes, edges, lanes, events, now, view, follow, cursor, flags, selected, grouped = true, resetVersion = 0, onOpen, zoom, pan }: {
  nodes: LNode[]; edges: LEdge[]; lanes: LLane[]; events: LEvent[]; now: number; view: TimeView; follow: boolean;
  cursor: number | null; flags: Map<string, AnomalyKind[]>; selected?: string | null; grouped?: boolean; resetVersion?: number;
  onOpen: (n: LNode | null) => void; zoom: (factor: number, anchor: number) => void; pan: (dt: number) => void;
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
  const [positions, setPositions] = useState<Record<string, { x: number; y: number }>>({});
  const nodeDrag = useRef<{ id: string; pointer: number; x: number; y: number; base: { x: number; y: number }; moved: boolean } | null>(null);

  const layout = useMemo(() => layoutRiver(nodes, lanes, now, span / plotW * 8, grouped, grouped), [nodes, lanes, now, span, plotW, grouped]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const focused = useMemo(() => focusSet(nodes, edges, selected ?? null), [nodes, edges, selected]);
  const ticks = useMemo(() => niceTicks(view.t0, view.t1, Math.max(3, Math.floor(plotW / 110))), [view, plotW]);
  const { justDragged, dragging } = usePanZoom(svgRef, { plotLeft: plotL, plotWidth: plotW, view, zoom, pan });

  const H = AXIS_H + layout.height + 8;
  const offsetOf = (id: string) => positions[id] ?? { x: 0, y: 0 };
  const yOf = (id: string) => AXIS_H + layout.place[id].y + offsetOf(id).y;
  const edgeKeyHot = (e: LEdge) => hot !== null && (e.from === hot || e.to === hot);
  const inFocus = (id: string) => !selected || focused.has(id);
  useEffect(() => { setPositions({}); }, [resetVersion]);

  useEffect(() => {
    const clear = (e: globalThis.KeyboardEvent) => { const el = e.target as HTMLElement; if (e.key === "Escape" && selected && !["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) && !el.closest("[role=dialog]")) onOpen(null); };
    window.addEventListener("keydown", clear);
    return () => window.removeEventListener("keydown", clear);
  }, [selected, onOpen]);

  function show(n: LNode, el: Element) {
    const wrap = wrapRef.current?.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    if (!wrap) return;
    setCard({ id: n.id, x: Math.min(Math.max(r.left - wrap.left + r.width / 2, 130), Math.max(130, wrap.width - 130)), y: r.bottom - wrap.top + 8 });
    setHot(n.id);
  }
  const hide = () => { setCard(null); setHot(null); };
  const open = (n: LNode) => { if (!justDragged.current && !nodeDrag.current?.moved) onOpen(n); };
  const onKey = (n: LNode) => (e: KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(n); }
  };

  const nowX = x(cursor ?? now);
  const cardNode = card ? byId.get(card.id) : undefined;
  const domain = useMemo(() => {
    const starts = nodes.map((n) => n.created_at);
    const ends = nodes.map((n) => spanOf(n, now).end);
    return { t0: Math.min(...starts, view.t0), t1: Math.max(...ends, view.t1) };
  }, [nodes, now, view]);
  const miniW = 112, miniH = 34;
  const mini = (t: number) => ((t - domain.t0) / Math.max(1, domain.t1 - domain.t0)) * miniW;
  const miniPan = (e: PointerEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const t = domain.t0 + Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)) * (domain.t1 - domain.t0);
    pan(t - (view.t0 + view.t1) / 2);
  };

  function dragStart(n: LNode, e: PointerEvent<SVGGElement>) {
    if (e.button !== 0) return;
    e.stopPropagation();
    const base = offsetOf(n.id);
    nodeDrag.current = { id: n.id, pointer: e.pointerId, x: e.clientX, y: e.clientY, base, moved: false };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function dragMove(e: PointerEvent<SVGSVGElement>) {
    const d = nodeDrag.current;
    if (!d || d.pointer !== e.pointerId) return;
    const dx = e.clientX - d.x, dy = e.clientY - d.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    setPositions((old) => ({ ...old, [d.id]: { x: d.base.x + dx, y: d.base.y + dy } }));
  }
  function dragEnd(e: PointerEvent<SVGSVGElement>) {
    if (nodeDrag.current?.pointer === e.pointerId) setTimeout(() => { nodeDrag.current = null; }, 0);
  }

  return (
    <div className="lin-river" ref={wrapRef}>
      <div className="lin-scroll" ref={scrollRef}>
        <svg
          ref={svgRef} className={dragging ? "lin-svg grabbing" : "lin-svg"} width={W} height={H} viewBox={`0 0 ${W} ${H}`}
          role="group" aria-label={`Fleet river: ${nodes.length} attempts on ${layout.groups.reduce((a, g) => a + g.lanes.length, 0)} lanes`}
          onPointerMove={dragMove} onPointerUp={dragEnd} onPointerCancel={dragEnd}
        >
          <defs>
            <pattern id="lin-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="6" className="lin-hatch-line" />
            </pattern>
            {["warn", "info", "ok", "accent", "quiet", "bad"].map((t) => (
              <marker key={t} id={`lin-arrow-${t}`} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M0 0L8 4L0 8z" className={`lin-arrow t-${t}`} />
              </marker>
            ))}
            <clipPath id="lin-plot"><rect x={plotL} y={0} width={plotW} height={H} /></clipPath>
          </defs>

          {/* node bands and lanes */}
          {grouped && layout.groups.map((g) => (
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
                const x1 = x(ps.end) + offsetOf(p.id).x, y1 = yOf(p.id) + BAR_H / 2 + 3;
                const x2 = x(cs.created) + offsetOf(c.id).x, y2 = yOf(c.id) + BAR_H / 2 + 3;
                if (Math.max(x1, x2) < plotL - 10 || Math.min(x1, x2) > plotL + plotW + 10) return null;
                const dx = Math.min(140, Math.max(26, Math.abs(x2 - x1) * 0.5));
                const tone = p.state === "failed" || c.state === "failed" ? "bad" : LINK_TONE[e.link] ?? "quiet";
                const label = e.reason && e.reason !== e.link ? `${e.link} · ${e.reason.replace("_", " ")}` : e.link;
                const wide = Math.abs(x2 - x1) > 84 || hot !== null && edgeKeyHot(e);
                const d = `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
                const runParticles = (p.ended_at === null || c.ended_at === null) && (edges.length <= 150 || !!selected && focused.has(e.from) && focused.has(e.to));
                return (
                  <g key={`${e.from}>${e.to}`} className={`lin-edge t-${tone}${edgeKeyHot(e) ? " hot" : ""}${!inFocus(e.from) || !inFocus(e.to) ? " dim" : ""}`}>
                    <path d={d} markerEnd={`url(#lin-arrow-${tone})`} />
                    {runParticles && <circle className="lin-particle" r="3"><animateMotion dur="1.7s" repeatCount="indefinite" path={d} /></circle>}
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
              const off = offsetOf(n.id);
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
                  transform={`translate(${off.x} ${off.y})`} data-nodrag="true"
                  className={`lin-bar t-${tone}${n.id === selected ? " sel" : ""}${s.live && s.start !== null ? " live" : ""}${hot === n.id ? " hot" : ""}${!inFocus(n.id) ? " dim" : ""}`}
                  onClick={() => open(n)} onKeyDown={onKey(n)}
                  onPointerDown={(e) => dragStart(n, e)}
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
                      {!showTitle && <text x={xe + 6} y={y + 18} className="lin-outside-label">{kindGlyph(n.kind)}</text>}
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
      <svg className="lin-minimap" viewBox={`0 0 ${miniW} ${miniH}`} aria-label="Overview minimap" onPointerDown={miniPan}>
        <rect width={miniW} height={miniH} rx={4} />
        {nodes.map((n) => { const pl = layout.place[n.id]; if (!pl) return null; const s = spanOf(n, now); const y = 7 + ((pl.y / Math.max(1, layout.height)) * (miniH - 14)); return <line key={n.id} x1={mini(s.created)} x2={mini(s.end)} y1={y} y2={y} />; })}
        <rect className="lin-minimap-view" x={mini(view.t0)} width={Math.max(3, mini(view.t1) - mini(view.t0))} height={miniH} />
      </svg>

      {card && cardNode && <HoverCard node={cardNode} now={cursor ?? now} events={events} x={card.x} y={card.y} />}
    </div>
  );
}

/** Selection detail for the river, shown beside the chart or as a bottom sheet on small screens. */
export function RiverPanel({ node: n, now, onClose }: { node: LNode; now: number; onClose: () => void }) {
  const s = spanOf(n, now);
  return <aside className="lin-river-panel" aria-label="Selected attempt details">
    <div className="lin-panel-head"><div><h2 className="mono">{n.id}</h2><p className="quiet">{n.title ?? n.kind ?? "Task attempt"}</p></div><button type="button" className="icon ghost" aria-label="Close details" onClick={onClose}>×</button></div>
    <dl className="kv">
      <dt>Model</dt><dd>{n.model ?? n.agent ?? "—"}</dd><dt>Account</dt><dd>{n.account}</dd><dt>Kind / stage</dt><dd>{n.kind ?? "task"}{n.stage ? ` · ${n.stage}` : ""}</dd>
      <dt>State</dt><dd>{STATE[n.state as keyof typeof STATE]?.label ?? n.state}</dd><dt>Tokens</dt><dd>{fmtTokens(tokenTotal(n.tokens))}</dd><dt>Duration</dt><dd>{s.start === null ? `Queued ${fmtDur(s.wait)}` : `${fmtDur(s.end - s.start)}${s.live ? " so far" : ""}`}</dd>
      <dt>Gate</dt><dd>{gateLabel(n)}</dd><dt>Verdict</dt><dd>{n.failure_reason ?? "—"}</dd>
    </dl>
    <div className="row"><Link className="button-link" to={`/work/${n.root_id}`}>Open work</Link><Link className="button-link" to={`/lineage/${n.root_id}?sel=${n.id}`}>Open trace</Link></div>
  </aside>;
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
