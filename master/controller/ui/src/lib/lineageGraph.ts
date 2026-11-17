/**
 * Lineage page logic (docs/SPEC-ui-revamp.md 6c): lane packing, trace layout, critical path, replay snapshots,
 * agent heartbeats, intelligence (bottleneck, anomalies) and the deterministic narrative. Pure functions only, so
 * every rule is unit-tested; the SVG lives in components/lineage/.
 */

export interface LTokens { turns: number; input: number; cache_read: number; cache_write: number; output: number }
export interface LGate { passed: boolean | null; tail: string }

export interface LNode {
  id: string;
  root_id: string;
  parent_id: string | null;
  link: string | null;
  title: string | null;
  kind: string | null;
  stage: string | null;
  agent: string | null;
  model: string | null;
  account: string;
  node: string;
  lane: string;
  state: string;
  created_at: number;
  started_at: number | null;
  ended_at: number | null;
  failure_reason: string | null;
  gate_failed: boolean;
  gate: LGate | null;
  branch: string | null;
  budget_wait: unknown;
  resets_at: number | null;
  tokens: LTokens | null;
}
export interface LEdge { from: string; to: string; link: string; reason: string | null }
export interface LLane { id: string; node: string; account: string; agent: string | null }
export interface LEvent { ts: number; task_id: string; type: string; event_id?: number; to?: string; from?: string; mark?: "limit" | "stall" | "gate" }
export interface LineageData {
  now: number; since: number; until: number; root: string | null; truncated: boolean;
  nodes: LNode[]; edges: LEdge[]; lanes: LLane[]; events: LEvent[];
}

// ---------------------------------------------------------------------------------------------------- formatting

export function fmtDur(s: number): string {
  const v = Math.max(0, Math.round(s));
  if (v < 60) return `${v}s`;
  if (v < 3600) return `${Math.floor(v / 60)}m ${String(v % 60).padStart(2, "0")}s`;
  if (v < 86400) return `${Math.floor(v / 3600)}h ${String(Math.floor((v % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(v / 86400)}d ${Math.floor((v % 86400) / 3600)}h`;
}

const pad2 = (n: number) => String(n).padStart(2, "0");
export function fmtClock(ts: number, utc = false, seconds = false): string {
  const d = new Date(ts * 1000);
  const h = utc ? d.getUTCHours() : d.getHours();
  const m = utc ? d.getUTCMinutes() : d.getMinutes();
  const s = utc ? d.getUTCSeconds() : d.getSeconds();
  return seconds ? `${pad2(h)}:${pad2(m)}:${pad2(s)}` : `${pad2(h)}:${pad2(m)}`;
}

export const tokenTotal = (t: LTokens | null | undefined): number => (t ? t.input + t.output + t.cache_read + t.cache_write : 0);
export function fmtTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}k`;
  return String(n);
}

// ---------------------------------------------------------------------------------------------------- stages

export const STAGES = ["spawn", "build", "gate", "review", "fix", "re-review", "merge", "done"] as const;
export type Stage = (typeof STAGES)[number];

const KIND_STAGE: Record<string, Stage> = {
  draft: "spawn", build: "build", check: "gate", test: "gate", benchmark: "gate", review: "review",
  security_review: "review", final_review: "review", fix: "fix", re_review: "re-review", merge: "merge",
};

/** The pipeline stage an attempt belongs to, from its kind first and its stage label ("fix-2") second. */
export function stageOf(n: Pick<LNode, "kind" | "stage" | "parent_id">): Stage {
  if (n.kind && KIND_STAGE[n.kind]) return KIND_STAGE[n.kind];
  const label = (n.stage ?? "").toLowerCase().replace(/[-_ ]?\d+$/, "").replace("_", "-");
  if ((STAGES as readonly string[]).includes(label)) return label as Stage;
  return n.parent_id ? "review" : "build";
}
export const stageRank = (s: Stage): number => STAGES.indexOf(s);

export const KIND_GLYPH: Record<string, string> = {
  build: "B", fix: "F", review: "R", re_review: "R2", final_review: "R", security_review: "S", merge: "M",
  check: "C", test: "T", benchmark: "K", draft: "D",
};
export const kindGlyph = (kind: string | null): string => (kind ? KIND_GLYPH[kind] ?? kind.slice(0, 1).toUpperCase() : "·");

const TERMINAL = new Set(["done", "failed", "interrupted", "cancelled"]);
export const isTerminal = (state: string): boolean => TERMINAL.has(state);

// ---------------------------------------------------------------------------------------------------- spans

export interface Span {
  /** Where the attempt entered the queue. */
  created: number;
  /** When it began running; null when it never did. */
  start: number | null;
  end: number;
  /** Queue wait, drawn hatched before the bar. */
  wait: number;
  live: boolean;
}

export function spanOf(n: LNode, now: number): Span {
  const end = Math.max(n.ended_at ?? now, n.created_at);
  const start = n.started_at !== null && n.started_at <= end ? n.started_at : null;
  const waitEnd = start ?? end;
  return { created: n.created_at, start, end, wait: Math.max(0, waitEnd - n.created_at), live: n.ended_at === null };
}

// ---------------------------------------------------------------------------------------------------- river layout

export const ROW_H = 30;
export const LANE_PAD = 7;
export const GROUP_H = 26;

/** Greedy interval packing: each lane gets as many sub-rows as it needs, and no two attempts on one row overlap. */
export function packLane(items: { id: string; from: number; to: number }[], gap = 0): { rows: Record<string, number>; count: number } {
  const sorted = [...items].sort((a, b) => a.from - b.from || a.to - b.to || a.id.localeCompare(b.id));
  const rowEnd: number[] = [];
  const rows: Record<string, number> = {};
  for (const it of sorted) {
    let r = rowEnd.findIndex((e) => e + gap <= it.from);
    if (r < 0) { r = rowEnd.length; rowEnd.push(it.to); } else rowEnd[r] = it.to;
    rows[it.id] = r;
  }
  return { rows, count: Math.max(1, rowEnd.length) };
}

export interface RiverLane { id: string; account: string; agent: string | null; y: number; h: number; rows: number }
export interface RiverGroup { node: string; y: number; h: number; lanes: RiverLane[] }
export interface RiverLayout {
  groups: RiverGroup[];
  /** Per attempt: its lane and the y of the top of its row. */
  place: Record<string, { lane: string; row: number; y: number }>;
  height: number;
}

/** Lanes grouped by node, each as tall as its packed rows. `gap` is the minimum clear time between bars on a row. */
export function layoutRiver(nodes: LNode[], lanes: LLane[], now: number, gap = 0): RiverLayout {
  const byLane = new Map<string, LNode[]>();
  for (const n of nodes) byLane.set(n.lane, [...(byLane.get(n.lane) ?? []), n]);
  const known = new Map(lanes.map((l) => [l.id, l]));
  for (const id of byLane.keys()) {
    if (!known.has(id)) { const n = byLane.get(id)![0]; known.set(id, { id, node: n.node, account: n.account, agent: n.agent }); }
  }
  const ordered = [...known.values()].filter((l) => byLane.has(l.id)).sort((a, b) => a.node.localeCompare(b.node) || a.account.localeCompare(b.account));
  const groups: RiverGroup[] = [];
  const place: RiverLayout["place"] = {};
  let y = 0;
  for (const lane of ordered) {
    let group = groups[groups.length - 1];
    if (!group || group.node !== lane.node) {
      group = { node: lane.node, y, h: GROUP_H, lanes: [] };
      groups.push(group);
      y += GROUP_H;
    }
    const members = byLane.get(lane.id)!;
    const packed = packLane(members.map((n) => { const s = spanOf(n, now); return { id: n.id, from: s.created, to: s.end }; }), gap);
    const h = packed.count * ROW_H + LANE_PAD * 2;
    group.lanes.push({ id: lane.id, account: lane.account, agent: lane.agent, y, h, rows: packed.count });
    for (const n of members) place[n.id] = { lane: lane.id, row: packed.rows[n.id], y: y + LANE_PAD + packed.rows[n.id] * ROW_H };
    y += h;
    group.h = y - group.y;
  }
  return { groups, place, height: y };
}

// ---------------------------------------------------------------------------------------------------- time view

export interface TimeView { t0: number; t1: number }
export const MIN_SPAN = 30;
export const MAX_SPAN = 14 * 86400;

export function zoomView(v: TimeView, factor: number, anchor = 0.5): TimeView {
  const span = v.t1 - v.t0;
  const next = Math.min(MAX_SPAN, Math.max(MIN_SPAN, span * factor));
  const at = v.t0 + span * anchor;
  return { t0: at - next * anchor, t1: at + next * (1 - anchor) };
}
export const panView = (v: TimeView, dt: number): TimeView => ({ t0: v.t0 + dt, t1: v.t1 + dt });

const STEPS = [10, 30, 60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400];
export function niceTicks(t0: number, t1: number, max = 8): { ts: number; step: number }[] {
  const span = Math.max(1, t1 - t0);
  const step = STEPS.find((s) => span / s <= max) ?? STEPS[STEPS.length - 1];
  const offset = new Date(t0 * 1000).getTimezoneOffset() * 60; // align to local clock times
  const first = Math.ceil((t0 - offset) / step) * step + offset;
  const out: { ts: number; step: number }[] = [];
  for (let t = first; t <= t1 && out.length < 200; t += step) out.push({ ts: t, step });
  return out;
}
export function tickLabel(ts: number, step: number, utc = false): string {
  if (step >= 86400) { const d = new Date(ts * 1000); return `${(utc ? d.getUTCMonth() : d.getMonth()) + 1}/${utc ? d.getUTCDate() : d.getDate()}`; }
  return fmtClock(ts, utc, step < 60);
}

// ---------------------------------------------------------------------------------------------------- critical path

/** The chain of dependencies that set the end time: from the attempt that finished last, up through its parents. */
export function criticalPath(nodes: LNode[], now: number): string[] {
  if (!nodes.length) return [];
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const endOf = (n: LNode) => spanOf(n, now).end;
  const last = [...nodes].sort((a, b) => endOf(b) - endOf(a) || b.created_at - a.created_at || b.id.localeCompare(a.id))[0];
  const path: string[] = [];
  const seen = new Set<string>();
  for (let cur: LNode | undefined = last; cur && !seen.has(cur.id); cur = cur.parent_id ? byId.get(cur.parent_id) : undefined) {
    seen.add(cur.id);
    path.unshift(cur.id);
  }
  return path;
}
export const edgeKey = (from: string, to: string): string => `${from}>${to}`;

// ---------------------------------------------------------------------------------------------------- trace layout

export interface TraceItem { id: string; col: number; row: number }
export interface TraceLayout {
  items: TraceItem[];
  /** Number of columns including the spawn pip (column 0). */
  cols: number;
  rows: number;
  /** One label per column: the stage of the attempts in it. */
  columns: { col: number; stage: Stage }[];
  /** The "done" pip sits after the last column when the item has finished well. */
  done: { col: number; row: number; from: string } | null;
}

/** Left to right by dependency depth (a child is always right of its parent); rows keep children near their parent. */
export function layoutTrace(nodes: LNode[]): TraceLayout {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map<string, number>();
  const depthOf = (n: LNode, guard = 0): number => {
    if (depth.has(n.id)) return depth.get(n.id)!;
    const p = n.parent_id ? byId.get(n.parent_id) : undefined;
    const d = p && guard < 200 ? depthOf(p, guard + 1) + 1 : 1; // column 0 is the spawn pip
    depth.set(n.id, d);
    return d;
  };
  nodes.forEach((n) => depthOf(n));
  const cols = Math.max(1, ...nodes.map((n) => depth.get(n.id)!)) + 1;
  const rowOf = new Map<string, number>();
  const items: TraceItem[] = [];
  let rows = 0;
  for (let c = 1; c < cols; c++) {
    const inCol = nodes.filter((n) => depth.get(n.id) === c).sort((a, b) =>
      (rowOf.get(a.parent_id ?? "") ?? 0) - (rowOf.get(b.parent_id ?? "") ?? 0)
      || stageRank(stageOf(a)) - stageRank(stageOf(b)) || a.created_at - b.created_at || a.id.localeCompare(b.id));
    const taken = new Set<number>();
    for (const n of inCol) {
      let r = Math.round(rowOf.get(n.parent_id ?? "") ?? 0);
      while (taken.has(r)) r++;
      taken.add(r);
      rowOf.set(n.id, r);
      items.push({ id: n.id, col: c, row: r });
      rows = Math.max(rows, r + 1);
    }
  }
  const columns: TraceLayout["columns"] = [{ col: 0, stage: "spawn" }];
  for (let c = 1; c < cols; c++) {
    const here = nodes.filter((n) => depth.get(n.id) === c).map(stageOf).sort((a, b) => stageRank(a) - stageRank(b));
    columns.push({ col: c, stage: here[0] ?? "build" });
  }
  const endOf = (n: LNode): number => n.ended_at ?? n.created_at;
  const finishing = [...nodes].sort((a, b) => endOf(b) - endOf(a))[0];
  const well = finishing && finishing.state === "done" && !nodes.some((n) => !isTerminal(n.state));
  let done: TraceLayout["done"] = null;
  if (well) {
    done = { col: cols, row: rowOf.get(finishing.id) ?? 0, from: finishing.id };
    columns.push({ col: cols, stage: "done" });
  }
  return { items, cols: cols + (done ? 1 : 0), rows: Math.max(1, rows), columns, done };
}

// ---------------------------------------------------------------------------------------------------- queue waits

export function waitSeconds(n: LNode, now: number): number {
  const s = spanOf(n, now);
  return s.wait;
}

/** Why an attempt sat in the queue, from what the controller recorded. */
export function waitReason(n: LNode): string {
  if (typeof n.budget_wait === "string" && n.budget_wait) return `budget wait: ${n.budget_wait}`;
  if (n.resets_at) return `limit until ${fmtClock(n.resets_at)}`;
  return `node busy (no free slot on ${n.node})`;
}

// ---------------------------------------------------------------------------------------------------- events

export interface Mark { ts: number; kind: "limit" | "stall" | "gate" }
export function marksFor(id: string, events: LEvent[]): Mark[] {
  return events.filter((e) => e.task_id === id && e.mark).map((e) => ({ ts: e.ts, kind: e.mark! }));
}
export const MARK_LABEL: Record<Mark["kind"], string> = { limit: "Usage limit hit", stall: "Stalled", gate: "Gate failed" };

/** Distinct event times in order: what the replay scrubber steps through. */
export function stepTimes(events: LEvent[]): number[] {
  return [...new Set(events.map((e) => e.ts))].sort((a, b) => a - b);
}

/** The data as it stood at `ts`: later attempts are absent, unfinished ones are clipped, states come from the events. */
export function snapshotAt(nodes: LNode[], events: LEvent[], ts: number): LNode[] {
  const latest = new Map<string, string>();
  for (const e of events) if (e.ts <= ts && e.to) latest.set(e.task_id, e.to);
  const out: LNode[] = [];
  for (const n of nodes) {
    if (n.created_at > ts) continue;
    const ended = n.ended_at !== null && n.ended_at <= ts;
    out.push({
      ...n,
      started_at: n.started_at !== null && n.started_at <= ts ? n.started_at : null,
      ended_at: ended ? n.ended_at : null,
      state: ended ? n.state : (latest.get(n.id) && !isTerminal(latest.get(n.id)!) ? latest.get(n.id)! : n.started_at !== null && n.started_at <= ts ? "working" : "queued"),
      gate_failed: n.gate_failed && ended,
    });
  }
  return out;
}

export function describeEvent(e: LEvent, byId: Map<string, LNode>): string {
  const n = byId.get(e.task_id);
  const what = e.mark ? MARK_LABEL[e.mark].toLowerCase() : e.type === "created" ? "queued" : e.to ? `${e.from ? `${e.from} → ` : ""}${e.to}` : e.type;
  return `${e.task_id}${n?.kind ? ` (${n.kind})` : ""} ${what}`;
}

// ---------------------------------------------------------------------------------------------------- intelligence

export interface Cite { id: string; ts: number }
export interface Bottleneck { kind: "queue" | "run"; id: string; seconds: number; why: string; evidence: Cite[] }

/** The longest queue wait or slowest run in view, with the reason the controller recorded. */
export function findBottleneck(nodes: LNode[], now: number, minSeconds = 30): Bottleneck | null {
  let best: Bottleneck | null = null;
  for (const n of nodes) {
    const s = spanOf(n, now);
    const wait = s.wait;
    if (wait >= minSeconds && (!best || wait > best.seconds)) {
      best = { kind: "queue", id: n.id, seconds: wait, why: `${n.id} waited ${fmtDur(wait)} before starting: ${waitReason(n)}.`,
        evidence: [{ id: n.id, ts: s.created }, ...(s.start ? [{ id: n.id, ts: s.start }] : [])] };
    }
    if (s.start !== null && nodes.length > 1) {
      const run = s.end - s.start;
      if (run >= minSeconds && (!best || run > best.seconds)) {
        const stage = stageOf(n);
        best = { kind: "run", id: n.id, seconds: run, why: `The ${stage} stage (${n.id}, ${n.model ?? n.agent ?? "agent"}) was the slowest step at ${fmtDur(run)}${s.live ? " and is still running" : ""}.`,
          evidence: [{ id: n.id, ts: s.start }, ...(s.live ? [] : [{ id: n.id, ts: s.end }])] };
      }
    }
  }
  return best;
}

export type AnomalyKind = "retries" | "gate_rounds" | "stall" | "cost" | "limit";
export interface Anomaly { kind: AnomalyKind; title: string; why: string; ids: string[]; evidence: Cite[] }

const median = (xs: number[]): number => {
  const s = [...xs].sort((a, b) => a - b);
  return s.length ? (s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2) : 0;
};

export const RETRY_LIMIT = 2;
export const GATE_ROUND_LIMIT = 3;
export const COST_FACTOR = 3;

/** Retries above two, gate rounds above three, stalls and cost outliers against the per-kind median. */
export function findAnomalies(nodes: LNode[], events: LEvent[]): Anomaly[] {
  const out: Anomaly[] = [];
  const roots = [...new Set(nodes.map((n) => n.root_id))];
  for (const root of roots) {
    const mine = nodes.filter((n) => n.root_id === root);
    const retries = mine.filter((n) => n.link === "retry" || n.link === "handoff" || n.link === "resume");
    if (retries.length > RETRY_LIMIT) {
      out.push({ kind: "retries", title: `${retries.length} retries on ${root}`, ids: retries.map((n) => n.id),
        why: `More than ${RETRY_LIMIT} retry or handoff attempts on one work item usually means the same cause keeps recurring.`,
        evidence: retries.map((n) => ({ id: n.id, ts: n.created_at })) });
    }
    const rounds = mine.filter((n) => n.gate !== null);
    if (rounds.length > GATE_ROUND_LIMIT) {
      out.push({ kind: "gate_rounds", title: `${rounds.length} gate rounds on ${root}`, ids: rounds.map((n) => n.id),
        why: `The gate has run more than ${GATE_ROUND_LIMIT} times for this work item; the pipeline escalates to the owner after three.`,
        evidence: rounds.map((n) => ({ id: n.id, ts: n.ended_at ?? n.created_at })) });
    }
  }
  const stalls = events.filter((e) => e.mark === "stall");
  for (const e of stalls) {
    out.push({ kind: "stall", title: `Stall on ${e.task_id}`, ids: [e.task_id], why: `${e.type} at ${fmtClock(e.ts, false, true)}: the attempt stopped making progress.`,
      evidence: [{ id: e.task_id, ts: e.ts }] });
  }
  const byKind = new Map<string, LNode[]>();
  for (const n of nodes) if (tokenTotal(n.tokens) > 0) byKind.set(n.kind ?? "?", [...(byKind.get(n.kind ?? "?") ?? []), n]);
  for (const [kind, group] of byKind) {
    if (group.length < 3) continue;
    const med = median(group.map((n) => tokenTotal(n.tokens)));
    for (const n of group) {
      const total = tokenTotal(n.tokens);
      if (med > 0 && total > COST_FACTOR * med) {
        out.push({ kind: "cost", title: `${n.id} used ${fmtTokens(total)} tokens`, ids: [n.id],
          why: `${(total / med).toFixed(1)}x the median ${fmtTokens(med)} for ${kind} attempts in view (${group.length} compared).`,
          evidence: [{ id: n.id, ts: n.ended_at ?? n.created_at }] });
      }
    }
  }
  for (const e of events.filter((x) => x.mark === "limit")) {
    out.push({ kind: "limit", title: `Usage limit hit on ${e.task_id}`, ids: [e.task_id], why: "The account reached its limit; the controller waits for the reset or hands the work to another account.",
      evidence: [{ id: e.task_id, ts: e.ts }] });
  }
  return out;
}

/** Attempt id to the anomaly kinds that flag it, for the markers on the bars. */
export function flagMap(anoms: Anomaly[]): Map<string, AnomalyKind[]> {
  const m = new Map<string, AnomalyKind[]>();
  for (const a of anoms) for (const id of a.ids) if (!(m.get(id) ?? []).includes(a.kind)) m.set(id, [...(m.get(id) ?? []), a.kind]);
  return m;
}

// ---------------------------------------------------------------------------------------------------- narrative

const LINK_VERB: Record<string, string> = {
  retry: "retried as", resume: "resumed as", handoff: "handed off to", fix: "sent back for a fix as",
  review: "sent to review as", merge: "sent to merge as", check: "checked by",
};

export interface Narrative { text: string; evidence: Cite[] }

/** What happened, in order, written from the records alone. No model is involved. */
export function buildNarrative(nodes: LNode[], now: number, utc = false): Narrative {
  if (!nodes.length) return { text: "Nothing has happened on this work item yet.", evidence: [] };
  const sorted = [...nodes].sort((a, b) => a.created_at - b.created_at || a.id.localeCompare(b.id));
  const first = sorted[0];
  const evidence: Cite[] = [{ id: first.id, ts: first.created_at }];
  const clock = (t: number) => fmtClock(t, utc);
  const sentences: string[] = [];
  sentences.push(`${first.root_id} was queued at ${clock(first.created_at)} as ${first.kind ?? "a task"} on ${first.node} using ${first.model ?? first.agent ?? "an agent"}.`);
  const outcome = (n: LNode): string => {
    const s = spanOf(n, now);
    if (n.ended_at === null) return s.start === null ? "is still queued" : `is still ${n.state === "working" ? "working" : n.state}`;
    const took = s.start !== null ? fmtDur(s.end - s.start) : null;
    if (n.state === "done") return `finished${took ? ` in ${took}` : ""}`;
    const why = n.failure_reason === "gate_failed" || n.gate_failed ? "its gate failed" : n.failure_reason === "limit" ? "it hit a usage limit" : n.failure_reason ? `it ${n.failure_reason}` : "";
    return `${n.state}${took ? ` after ${took}` : ""}${why ? ` because ${why}` : ""}`;
  };
  sentences.push(`${first.id} ${outcome(first)}.`);
  const shown = sorted.slice(1, 6);
  for (const n of shown) {
    const parent = n.parent_id ? sorted.find((x) => x.id === n.parent_id) : undefined;
    const verb = (n.link && LINK_VERB[n.link]) ?? "continued as";
    evidence.push({ id: n.id, ts: n.created_at });
    sentences.push(`At ${clock(n.created_at)} ${parent ? parent.id : "the item"} was ${verb} ${n.id} (${n.kind ?? "task"}, ${n.model ?? n.agent ?? "agent"}), which ${outcome(n)}.`);
  }
  if (sorted.length > 6) sentences.push(`${sorted.length - 6} more attempts followed.`);
  const waits = sorted.filter((n) => spanOf(n, now).wait >= 60).sort((a, b) => spanOf(b, now).wait - spanOf(a, now).wait).slice(0, 2);
  for (const n of waits) {
    evidence.push({ id: n.id, ts: n.created_at });
    sentences.push(`${n.id} waited ${fmtDur(spanOf(n, now).wait)} in the queue (${waitReason(n)}).`);
  }
  const path = criticalPath(sorted, now);
  const end = Math.max(...sorted.map((n) => spanOf(n, now).end));
  const active = sorted.some((n) => n.ended_at === null);
  const total = fmtDur(end - first.created_at);
  sentences.push(active
    ? `It is still running after ${total}; the critical path so far is ${path.length} ${path.length === 1 ? "step" : "steps"} (${path.join(" → ")}).`
    : `It ended at ${clock(end)}, ${total} after it was queued; the critical path was ${path.length} ${path.length === 1 ? "step" : "steps"} (${path.join(" → ")}).`);
  return { text: sentences.join(" "), evidence };
}

// ---------------------------------------------------------------------------------------------------- agents

export interface AgentRow {
  key: string;
  agent: string;
  model: string | null;
  label: string;
  accounts: string[];
  /** Merged busy intervals. */
  busy: { from: number; to: number }[];
  /** Idle stretches longer than `minGap` between busy intervals. */
  gaps: { from: number; to: number }[];
  /** Tokens per minute in each of `buckets` equal slices of the window. */
  rate: number[];
  peak: number;
  busySeconds: number;
  runningNow: boolean;
  runningIds: string[];
  tasks: number;
}

export function mergeIntervals(iv: { from: number; to: number }[]): { from: number; to: number }[] {
  const sorted = iv.filter((i) => i.to >= i.from).sort((a, b) => a.from - b.from);
  const out: { from: number; to: number }[] = [];
  for (const i of sorted) {
    const last = out[out.length - 1];
    if (last && i.from <= last.to) last.to = Math.max(last.to, i.to);
    else out.push({ ...i });
  }
  return out;
}

export function agentRows(nodes: LNode[], now: number, win: TimeView, buckets = 60, minGap = 60): AgentRow[] {
  const groups = new Map<string, LNode[]>();
  for (const n of nodes) {
    const key = `${n.agent ?? "agent"}|${n.model ?? ""}`;
    groups.set(key, [...(groups.get(key) ?? []), n]);
  }
  const width = (win.t1 - win.t0) / buckets;
  const rows: AgentRow[] = [];
  for (const [key, members] of groups) {
    const spans = members.map((n) => ({ n, s: spanOf(n, now) })).filter((x) => x.s.start !== null);
    const busy = mergeIntervals(spans.map((x) => ({ from: Math.max(x.s.start!, win.t0), to: Math.min(x.s.end, win.t1) })));
    const gaps: AgentRow["gaps"] = [];
    for (let i = 1; i < busy.length; i++) if (busy[i].from - busy[i - 1].to >= minGap) gaps.push({ from: busy[i - 1].to, to: busy[i].from });
    const rate = new Array<number>(buckets).fill(0);
    for (const { n, s } of spans) {
      const total = tokenTotal(n.tokens);
      const dur = Math.max(1, s.end - s.start!);
      if (!total || width <= 0) continue;
      for (let b = 0; b < buckets; b++) {
        const lo = Math.max(win.t0 + b * width, s.start!), hi = Math.min(win.t0 + (b + 1) * width, s.end);
        if (hi > lo) rate[b] += (total * (hi - lo)) / dur;
      }
    }
    const perMin = rate.map((t) => (width > 0 ? t / (width / 60) : 0));
    const running = members.filter((n) => n.ended_at === null && n.started_at !== null);
    const first = members[0];
    rows.push({
      key, agent: first.agent ?? "agent", model: first.model, label: first.model ?? first.agent ?? "agent",
      accounts: [...new Set(members.map((n) => n.account))].sort(),
      busy, gaps, rate: perMin, peak: Math.max(0, ...perMin),
      busySeconds: busy.reduce((a, b) => a + (b.to - b.from), 0),
      runningNow: running.length > 0, runningIds: running.map((n) => n.id), tasks: members.length,
    });
  }
  return rows.sort((a, b) => Number(b.runningNow) - Number(a.runningNow) || b.busySeconds - a.busySeconds || a.label.localeCompare(b.label));
}

// ---------------------------------------------------------------------------------------------------- work items

export interface RootSummary { root: string; title: string; first: number; last: number; count: number; active: boolean; failed: boolean }
export function rootsOf(nodes: LNode[], now: number): RootSummary[] {
  const m = new Map<string, LNode[]>();
  for (const n of nodes) m.set(n.root_id, [...(m.get(n.root_id) ?? []), n]);
  return [...m.entries()].map(([root, ns]) => {
    const rootNode = ns.find((n) => n.id === root) ?? ns[0];
    return {
      root, title: rootNode.title ?? root, first: Math.min(...ns.map((n) => n.created_at)),
      last: Math.max(...ns.map((n) => spanOf(n, now).end)), count: ns.length,
      active: ns.some((n) => n.ended_at === null), failed: ns.some((n) => n.state === "failed" || n.state === "interrupted"),
    };
  }).sort((a, b) => b.last - a.last);
}

/** The window a set of attempts occupies, with a little air so the first and last bars aren't glued to the edges. */
export function dataExtent(nodes: LNode[], now: number): TimeView {
  if (!nodes.length) return { t0: now - 3600, t1: now };
  const t0 = Math.min(...nodes.map((n) => n.created_at));
  const t1 = Math.max(now > t0 && nodes.some((n) => n.ended_at === null) ? now : 0, ...nodes.map((n) => spanOf(n, now).end));
  const pad = Math.max(10, (t1 - t0) * 0.04);
  return { t0: t0 - pad, t1: t1 + pad };
}
