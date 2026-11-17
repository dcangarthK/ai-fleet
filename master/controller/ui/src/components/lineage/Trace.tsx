import { useMemo, type KeyboardEvent } from "react";
import { useFleet, useTask, useTaskLog } from "../../lib/data";
import { nodeName } from "../../lib/format";
import { STATE } from "../../lib/format";
import {
  criticalPath, edgeKey, fmtClock, fmtDur, fmtTokens, layoutTrace, marksFor, MARK_LABEL, spanOf, stageOf, tokenTotal,
  waitReason, type LEdge, type LEvent, type LNode, type Stage,
} from "../../lib/lineageGraph";
import { Terminal } from "../Terminal";
import { Empty } from "../ui";
import { toneOf } from "./River";
import { useMedia } from "./hooks";

const COL_W = 212;
const ROW_H = 96;
const CARD_W = 172;
const CARD_H = 66;
const PAD_X = 20;
const HEAD_H = 34;

const STAGE_LABEL: Record<Stage, string> = {
  spawn: "Spawn", build: "Build", gate: "Gate", review: "Review", fix: "Fix", "re-review": "Re-review", merge: "Merge", done: "Done",
};

/** The work item's DAG, left to right. The critical path is drawn bold; everything else steps back. */
export function TraceGraph({ all, visible, edges, now, events, cursor, selected, onSelect }: {
  /** Every attempt of the item, so the layout stays put while a replay reveals them one by one. */
  all: LNode[]; visible: LNode[]; edges: LEdge[]; now: number; events: LEvent[]; cursor: number | null;
  selected: string | null; onSelect: (id: string) => void;
}) {
  const stacked = useMedia("(max-width: 639px)");
  const layout = useMemo(() => layoutTrace(all), [all]);
  const critical = useMemo(() => criticalPath(all, now), [all, now]);
  const onPath = useMemo(() => new Set(critical), [critical]);
  const pathEdges = useMemo(() => new Set(critical.slice(1).map((id, i) => edgeKey(critical[i], id))), [critical]);
  const seen = useMemo(() => new Map(visible.map((n) => [n.id, n])), [visible]);
  const full = useMemo(() => new Map(all.map((n) => [n.id, n])), [all]);
  const pos = useMemo(() => new Map(layout.items.map((i) => [i.id, i])), [layout]);

  if (!all.length) return <Empty title="Nothing to trace">This work item has no attempts yet.</Empty>;

  if (stacked) {
    const ordered = [...layout.items].sort((a, b) => a.col - b.col || a.row - b.row);
    return (
      <ol className="lin-steps" aria-label="Trace steps">
        {ordered.map((it) => {
          const n = seen.get(it.id) ?? full.get(it.id)!;
          const ghost = !seen.has(it.id);
          const s = spanOf(n, now);
          return (
            <li key={it.id} className={`${onPath.has(it.id) ? "crit" : "dim"}${ghost ? " ghost" : ""}`}>
              <button type="button" className={`lin-step t-${toneOf(n.state)}${it.id === selected ? " sel" : ""}`} onClick={() => onSelect(it.id)} aria-pressed={it.id === selected}>
                <span className="lin-step-stage">{STAGE_LABEL[stageOf(n)]}</span>
                <span className="mono">{n.id}</span>
                <span className={`state tone-${toneOf(n.state)}`}>{STATE[n.state as keyof typeof STATE]?.label ?? n.state}</span>
                <span className="quiet">{s.start !== null ? fmtDur(s.end - s.start) : s.wait > 0 ? `queued ${fmtDur(s.wait)}` : ""}{n.link ? ` · ${n.link}` : ""}</span>
              </button>
            </li>
          );
        })}
      </ol>
    );
  }

  const W = PAD_X * 2 + layout.cols * COL_W;
  const H = HEAD_H + layout.rows * ROW_H + 16;
  const cx = (col: number) => PAD_X + col * COL_W;
  const cy = (row: number) => HEAD_H + 10 + row * ROW_H;
  const rootNode = all.find((n) => n.id === all[0].root_id) ?? all[0];
  const spawnRow = pos.get(rootNode.id)?.row ?? 0;
  const key = (id: string) => (e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onSelect(id); } };

  return (
    <div className="lin-trace-scroll">
      <svg className="lin-trace" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="group" aria-label={`Trace of ${all.length} attempts. Critical path: ${critical.join(", then ")}.`}>
        <defs>
          <marker id="lin-tarrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" className="lin-tarrow" /></marker>
          <marker id="lin-tarrow-crit" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" className="lin-tarrow crit" /></marker>
        </defs>
        {layout.columns.map((c) => (
          <g key={c.col} aria-hidden="true">
            <text x={cx(c.col) + (c.col === 0 ? 0 : CARD_W / 2)} y={20} textAnchor={c.col === 0 ? "start" : "middle"} className="lin-col-head">{STAGE_LABEL[c.stage]}</text>
            {c.col > 0 && <line x1={cx(c.col) - (COL_W - CARD_W) / 2} x2={cx(c.col) - (COL_W - CARD_W) / 2} y1={HEAD_H - 6} y2={H - 8} className="lin-col-sep" />}
          </g>
        ))}

        {/* spawn pip */}
        <g aria-hidden="true">
          <circle cx={cx(0) + 14} cy={cy(spawnRow) + CARD_H / 2} r={9} className="lin-pip" />
          <text x={cx(0) + 14} y={cy(spawnRow) + CARD_H / 2 + 24} textAnchor="middle" className="lin-pip-text">{fmtClock(rootNode.created_at)}</text>
        </g>
        <Edge d={link(cx(0) + 23, cy(spawnRow) + CARD_H / 2, cx(1), cy(spawnRow) + CARD_H / 2)} crit />

        {edges.map((e) => {
          const a = pos.get(e.from), b = pos.get(e.to);
          if (!a || !b) return null;
          const crit = pathEdges.has(edgeKey(e.from, e.to));
          const ghost = !seen.has(e.to) || !seen.has(e.from);
          const x1 = cx(a.col) + CARD_W, y1 = cy(a.row) + CARD_H / 2, x2 = cx(b.col), y2 = cy(b.row) + CARD_H / 2;
          const label = e.reason && e.reason !== e.link ? `${e.link} · ${e.reason.replace("_", " ")}` : e.link;
          return (
            <g key={edgeKey(e.from, e.to)} className={`lin-tedge${crit ? " crit" : ""}${ghost ? " ghost" : ""}`}>
              <path d={link(x1, y1, x2, y2)} markerEnd={`url(#lin-tarrow${crit ? "-crit" : ""})`} />
              <text x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 6} textAnchor="middle" className="lin-edge-label">{label}</text>
            </g>
          );
        })}

        {layout.done && (() => {
          const a = pos.get(layout.done.from)!;
          const x1 = cx(a.col) + CARD_W, y1 = cy(a.row) + CARD_H / 2, x2 = cx(layout.done.col) + 14, y2 = cy(layout.done.row) + CARD_H / 2;
          return (
            <g aria-hidden="true" className={cursor !== null && cursor < (full.get(layout.done.from)?.ended_at ?? 0) ? "ghost" : undefined}>
              <Edge d={link(x1, y1, x2 - 10, y2)} crit />
              <circle cx={x2} cy={y2} r={10} className="lin-pip done" />
              <path d={`M${x2 - 4} ${y2}l3 3l5 -6`} className="lin-pip-tick" />
            </g>
          );
        })()}

        {layout.items.map((it) => {
          const n = seen.get(it.id) ?? full.get(it.id)!;
          const ghost = !seen.has(it.id);
          const crit = onPath.has(it.id);
          const s = spanOf(n, now);
          const marks = marksFor(n.id, events).filter((m) => cursor === null || m.ts <= cursor);
          const x = cx(it.col), y = cy(it.row);
          const total = tokenTotal(n.tokens);
          const label = `${n.id}, ${STAGE_LABEL[stageOf(n)]}, ${STATE[n.state as keyof typeof STATE]?.label ?? n.state}${crit ? ", on the critical path" : ""}. Press Enter to open details.`;
          return (
            <g
              key={it.id} tabIndex={0} role="button" aria-label={label} aria-pressed={it.id === selected}
              className={`lin-tnode t-${toneOf(n.state)}${crit ? " crit" : " dim"}${ghost ? " ghost" : ""}${it.id === selected ? " sel" : ""}`}
              onClick={() => onSelect(it.id)} onKeyDown={key(it.id)} transform={`translate(${x} ${y})`}
            >
              <rect width={CARD_W} height={CARD_H} rx={8} className="lin-tnode-body" />
              <rect width={5} height={CARD_H} rx={2} className="lin-tnode-edge" />
              <text x={16} y={21} className="lin-tnode-id">{n.id}</text>
              <text x={16} y={38} className="lin-tnode-sub">{truncate(`${n.kind ?? "task"} · ${n.model ?? n.agent ?? ""}`, 24)}</text>
              <text x={16} y={55} className="lin-tnode-meta">
                {ghost ? "not started yet" : s.start !== null ? `${fmtDur(s.end - s.start)}${s.live ? " so far" : ""}` : s.wait > 0 ? `queued ${fmtDur(s.wait)}` : STATE[n.state as keyof typeof STATE]?.label ?? n.state}
                {total > 0 ? ` · ${fmtTokens(total)} tok` : ""}
              </text>
              {n.gate && !ghost && (
                <g transform={`translate(${CARD_W - 50} 8)`} className={`lin-gate ${n.gate.passed === false ? "bad" : n.gate.passed ? "ok" : ""}`}>
                  <rect width={42} height={16} rx={8} />
                  <text x={21} y={11.5} textAnchor="middle">{n.gate.passed === false ? "gate ✗" : n.gate.passed ? "gate ✓" : "gate"}</text>
                </g>
              )}
              {marks.slice(0, 3).map((m, i) => (
                <g key={i} className={`lin-diamond d-${m.kind}`} transform={`translate(${CARD_W - 12 - i * 16} ${CARD_H - 12})`}>
                  <path d="M0 -6L6 0L0 6L-6 0z" /><title>{MARK_LABEL[m.kind]}</title>
                </g>
              ))}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

function link(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(24, Math.abs(x2 - x1) * 0.5);
  return `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
}
function Edge({ d, crit }: { d: string; crit?: boolean }) {
  return <g className={`lin-tedge${crit ? " crit" : ""}`} aria-hidden="true"><path d={d} markerEnd={`url(#lin-tarrow${crit ? "-crit" : ""})`} /></g>;
}
function truncate(s: string, max: number): string {
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** Log excerpt, gate summary, findings, tokens and timing for one attempt. */
export function TracePanel({ node, now, events }: { node: LNode; now: number; events: LEvent[] }) {
  const fleet = useFleet();
  const { data: task } = useTask(node.id);
  const active = node.state === "working" || node.state === "starting";
  const log = useTaskLog(node.id, active);
  const s = spanOf(node, now);
  const excerpt = useMemo(() => log.split("\n").slice(-120).join("\n"), [log]);
  const repo = (task?.repo ?? "").split("/").filter(Boolean).pop() ?? "";
  const title = `${nodeName(fleet.data?.nodes ?? [], node.node)}:~/${repo}${node.branch ? ` (${node.branch})` : ""}`;
  const gate = task?.gate_result;
  const t = node.tokens;
  const marks = marksFor(node.id, events);
  return (
    <div className="lin-panel stack" aria-label={`Details for ${node.id}`}>
      <div>
        <div className="lin-panel-head"><span className="mono wrap-any">{node.id}</span><span className={`state tone-${toneOf(node.state)}`}>{STATE[node.state as keyof typeof STATE]?.label ?? node.state}</span></div>
        {node.title && <p className="lin-panel-title">{node.title}</p>}
        <dl className="lin-dl">
          <dt>Stage</dt><dd>{node.kind ?? "task"}{node.stage ? ` · ${node.stage}` : ""}</dd>
          <dt>Agent</dt><dd>{node.model ?? node.agent ?? "—"} on {node.node} ({node.account})</dd>
          <dt>Queued</dt><dd>{s.wait > 0 ? <>{fmtDur(s.wait)} <span className="quiet">({waitReason(node)})</span></> : "no wait"}</dd>
          <dt>Duration</dt><dd>{s.start !== null ? `${fmtDur(s.end - s.start)}${s.live ? " so far" : ""}` : "did not run"}</dd>
          {node.link && <><dt>Link</dt><dd>{node.link} from <span className="mono">{node.parent_id}</span></dd></>}
          {marks.length > 0 && <><dt>Marks</dt><dd>{marks.map((m) => `${MARK_LABEL[m.kind]} ${fmtClock(m.ts, false, true)}`).join("; ")}</dd></>}
        </dl>
      </div>

      <section aria-label="Tokens">
        <h3>Tokens</h3>
        {t ? (
          <dl className="lin-tokens">
            <div><dt>Input</dt><dd>{fmtTokens(t.input)}</dd></div>
            <div><dt>Output</dt><dd>{fmtTokens(t.output)}</dd></div>
            <div><dt>Cache read</dt><dd>{fmtTokens(t.cache_read)}</dd></div>
            <div><dt>Cache write</dt><dd>{fmtTokens(t.cache_write)}</dd></div>
            <div><dt>Turns</dt><dd>{t.turns}</dd></div>
          </dl>
        ) : <p className="quiet">No token record for this attempt.</p>}
      </section>

      <section aria-label="Gate">
        <h3>Gate</h3>
        {gate ? (
          <>
            <p><span className={`state tone-${gate.state === "passed" ? "ok" : gate.state === "failed" ? "bad" : "quiet"}`}>{gate.state}</span>{gate.cmd ? <> <span className="mono quiet wrap-any">{gate.cmd}</span></> : null}{gate.exit_code !== null ? <span className="quiet"> exit {gate.exit_code}</span> : null}</p>
            {gate.output_tail && <Terminal title={`gate: ${gate.cmd ?? "gate"}`} text={gate.output_tail} height={180} idPrefix={`gate-${node.id}-`} label="Gate output" />}
          </>
        ) : node.gate ? (
          <p><span className={`state tone-${node.gate.passed ? "ok" : "bad"}`}>{node.gate.passed ? "passed" : "failed"}</span></p>
        ) : <p className="quiet">This attempt has no gate result.</p>}
      </section>

      {task?.result && (
        <section aria-label="Findings">
          <h3>Result and findings</h3>
          <pre className="lin-result">{task.result.slice(0, 1200)}{task.result.length > 1200 ? "\n…" : ""}</pre>
        </section>
      )}

      <section aria-label="Agent log">
        <h3>Log excerpt</h3>
        <Terminal title={title} text={excerpt} live={active} height={280} idPrefix={`log-${node.id}-`} label={`Log of ${node.id}`} empty="No log yet, or the runner is offline." />
      </section>
    </div>
  );
}
