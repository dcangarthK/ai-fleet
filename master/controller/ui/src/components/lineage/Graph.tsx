import { useEffect, useMemo, useRef, useState, type PointerEvent, type WheelEvent } from "react";
import { focusSet, fmtDur, fmtTokens, layoutGraph, spanOf, tokenTotal, type LEdge, type LNode } from "../../lib/lineageGraph";

const CARD_W = 220, CARD_H = 64, VIEW_W = 1200, VIEW_H = 700;
const hue = (agent: string | null) => agent === "claude" ? "graph-claude" : agent === "ollama" ? "graph-ollama" : "graph-codex";

/** Full-width dependency graph; deliberately hand-written SVG to keep interaction lightweight. */
export function Graph({ nodes, edges, now, selected, onSelect }: { nodes: LNode[]; edges: LEdge[]; now: number; selected: string | null; onSelect: (node: LNode | null) => void }) {
  const layout = useMemo(() => layoutGraph(nodes, edges), [nodes, edges]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const blockByRoot = useMemo(() => new Map(layout.groups.map((g) => [g.root, g])), [layout]);
  const focused = useMemo(() => focusSet(nodes, edges, selected), [nodes, edges, selected]);
  const [view, setView] = useState({ x: 0, y: 0, z: 1 });
  const [offset, setOffset] = useState<Record<string, { x: number; y: number }>>({});
  const drag = useRef<{ x: number; y: number; view: typeof view; node?: string; base?: { x: number; y: number } } | null>(null);
  useEffect(() => { const clear = (e: KeyboardEvent) => { const t = e.target as HTMLElement; if (e.key === "Escape" && !t.closest("input,textarea,select,[role=dialog]")) onSelect(null); }; window.addEventListener("keydown", clear); return () => window.removeEventListener("keydown", clear); }, [onSelect]);
  const at = (id: string) => { const p = layout.place[id], block = blockByRoot.get(p.root)!; const o = offset[id] ?? { x: 0, y: 0 }; return { x: block.x + p.x + o.x, y: block.y + p.y + o.y }; };
  const onWheel = (e: WheelEvent<SVGSVGElement>) => { e.preventDefault(); const r = e.currentTarget.getBoundingClientRect(); const ax = (e.clientX - r.left - view.x) / view.z, ay = (e.clientY - r.top - view.y) / view.z; const z = Math.max(.45, Math.min(2.4, view.z * Math.exp(-e.deltaY * .0015))); setView({ z, x: e.clientX - r.left - ax * z, y: e.clientY - r.top - ay * z }); };
  const down = (e: PointerEvent<SVGElement>, node?: LNode) => { if (e.button !== 0) return; drag.current = { x: e.clientX, y: e.clientY, view, node: node?.id, base: node ? offset[node.id] ?? { x: 0, y: 0 } : undefined }; e.currentTarget.setPointerCapture(e.pointerId); };
  const move = (e: PointerEvent<SVGElement>) => { const d = drag.current; if (!d) return; const dx = (e.clientX - d.x) / view.z, dy = (e.clientY - d.y) / view.z; if (d.node) setOffset((old) => ({ ...old, [d.node!]: { x: d.base!.x + dx, y: d.base!.y + dy } })); else setView({ ...d.view, x: d.view.x + e.clientX - d.x, y: d.view.y + e.clientY - d.y }); };
  const fit = () => { const z = Math.max(.7, Math.min(1, (VIEW_W - 40) / layout.width, (VIEW_H - 40) / layout.height)); setView({ x: 20, y: 20, z }); setOffset({}); };
  const panFromMini = (e: PointerEvent<SVGSVGElement>) => { const r = e.currentTarget.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * layout.width, y = (e.clientY - r.top) / r.height * layout.height; setView((old) => ({ ...old, x: VIEW_W / 2 - x * old.z, y: VIEW_H / 2 - y * old.z })); };
  useEffect(() => { fit(); }, [layout.width, layout.height]); // reset only when the graph itself changes
  return <div className="graph-wrap">
    <div className="graph-actions"><button onClick={fit}>Fit</button><span className="quiet">Scroll to zoom · drag to pan · select a card to focus</span></div>
    <svg className="graph-svg" viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} onWheel={onWheel} onPointerDown={(e) => down(e)} onPointerMove={move} onPointerUp={() => { drag.current = null; }}>
      <defs><pattern id="graph-dots" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" /></pattern>{["quiet", "bad", "accent"].map((t) => <marker id={`graph-arrow-${t}`} key={t} viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" className={`graph-arrow ${t}`} /></marker>)}</defs>
      <rect width="100%" height="100%" className="graph-bg" /><g transform={`translate(${view.x} ${view.y}) scale(${view.z})`}>
        {layout.groups.map((g) => <g key={g.root}><rect x={g.x} y={g.y} width={g.w} height={g.h} rx="10" className="graph-band"/><text x={g.x + 16} y={g.y + 20} className="graph-band-label">{g.root}</text></g>)}
        {edges.map((e) => { const a = byId.get(e.from), b = byId.get(e.to); if (!a || !b || !layout.place[a.id] || !layout.place[b.id]) return null; const p = at(a.id), q = at(b.id), x1 = p.x + CARD_W, y1 = p.y + CARD_H / 2, x2 = q.x, y2 = q.y + CARD_H / 2, d = `M${x1} ${y1} C${x1 + 48} ${y1} ${x2 - 48} ${y2} ${x2} ${y2}`, failed = a.state === "failed" || b.state === "failed", running = a.ended_at === null || b.ended_at === null, hot = !!selected && focused.has(a.id) && focused.has(b.id); return <g key={`${e.from}-${e.to}`} className={`graph-edge ${failed ? "bad" : running ? "running" : "quiet"}${hot ? " hot" : ""}`}><path d={d} markerEnd={`url(#graph-arrow-${failed ? "bad" : running ? "accent" : "quiet"})`}/>{running && <><circle r="3"><animateMotion dur="1.4s" repeatCount="indefinite" path={d}/></circle><circle r="3"><animateMotion dur="1.4s" begin=".7s" repeatCount="indefinite" path={d}/></circle></>}{(e.link === "retry" || e.link === "fix") && <g transform={`translate(${(x1 + x2) / 2} ${(y1 + y2) / 2 - 12})`} className="graph-link"><rect x="-20" y="-10" width="40" height="18" rx="9"/><text textAnchor="middle" y="3">{e.link}</text></g>}</g>; })}
        {nodes.map((n) => { const p = at(n.id), s = spanOf(n, now), dim = !!selected && !focused.has(n.id), state = n.state === "failed" ? "failed" : n.state === "cancelled" ? "cancelled" : n.ended_at === null ? "running" : "done"; return <g key={n.id} transform={`translate(${p.x} ${p.y})`} style={{ animationDelay: `${layout.place[n.id].col * 70}ms` }} className={`graph-node ${state} ${hue(n.agent)}${dim ? " dim" : ""}`} tabIndex={0} role="button" onPointerDown={(e) => { e.stopPropagation(); down(e, n); }} onClick={(e) => { e.stopPropagation(); onSelect(n); }} onKeyDown={(e) => { if (e.key === "Enter") onSelect(n); }}><rect width={CARD_W} height={CARD_H} rx="10"/><circle cx="20" cy="22" r="11"/><text x="20" y="26" textAnchor="middle" className="graph-initial">{(n.agent ?? "?").slice(0, 1).toUpperCase()}</text><text x="38" y="22" className="graph-title">{trim(`${n.kind ?? "task"} · ${n.stage ?? ""}`, 25)}</text><text x="38" y="44" className="graph-sub">{trim(`${n.model ?? n.agent ?? "agent"} · ${fmtDur(s.start ? s.end - s.start : s.wait)} · ${fmtTokens(tokenTotal(n.tokens))}`, 31)}</text>{state === "done" && <text x="181" y="25" className="graph-tick">✓</text>}{state === "failed" && <text x="181" y="25" className="graph-fail">×</text>}</g>; })}
      </g>
    </svg>
    <svg className="graph-mini" viewBox={`0 0 ${layout.width} ${layout.height}`} role="button" tabIndex={0} aria-label="Graph overview; click to pan" onPointerDown={panFromMini} onKeyDown={(e) => { if (e.key === "Enter") fit(); }}>
      <rect width={layout.width} height={layout.height} className="graph-mini-bg"/>
      {layout.groups.map((g) => <rect key={g.root} x={g.x} y={g.y} width={g.w} height={g.h} className="graph-mini-band"/>)}
      {nodes.map((n) => { const p = at(n.id); return <rect key={n.id} x={p.x} y={p.y} width={CARD_W} height={CARD_H} className={`graph-mini-node ${n.state === "failed" ? "bad" : ""}`}/>; })}
      <rect x={-view.x / view.z} y={-view.y / view.z} width={VIEW_W / view.z} height={VIEW_H / view.z} className="graph-mini-viewport"/>
    </svg>
  </div>;
}
const trim = (text: string, max: number) => text.length > max ? `${text.slice(0, max - 1)}…` : text;
