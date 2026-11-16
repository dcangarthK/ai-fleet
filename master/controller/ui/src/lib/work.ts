// Work page logic (docs/SPEC-ui-revamp.md sections 3, 4, 6a). Pure functions, so they're testable.
// The list comes from GET /api/work (lineage.ts WorkItem); the detail from GET /api/work/{root} (WorkDetail below).
import type { WorkItem } from "./lineage";

// ---------- API shapes (master/controller/ui_api.py) ----------

export interface Tokens { turns: number; input: number; cache_read: number; cache_write: number; output: number }
export interface GateSummary { passed: boolean | null | undefined; tail: string }
export interface PlannedStage { stage: string; agent: string; model?: string; profile?: string }

export interface DetailAttempt {
  id: string;
  parent_id: string | null;
  title: string | null;
  state: string;
  kind: string | null;
  stage: string | null;
  agent: string;
  model: string | null;
  account: string | null;
  account_label: string | null;
  node: string | null;
  branch: string | null;
  created_at: number | null;
  ended_at: number | null;
  gate: GateSummary | null;
  exit_code: number | null;
  /** The outcome kind (merged, done, abandoned) on a root; not a review verdict. */
  verdict: string | null;
  resolution: { kind: string; [k: string]: unknown } | null;
  result_excerpt: string;
  tokens: Tokens | null;
}

export interface WorkDetail {
  root_id: string;
  title: string | null;
  state: string | null;
  repo: string | null;
  chain: PlannedStage[] | string | null;
  attempts: DetailAttempt[];
  tokens: Tokens;
}

export interface AskResponse { answer: string; citations: number[]; model: string; local: boolean }

// ---------- stages and the chain strip ----------

/** Canonical order of the chain strip. Stages not listed sort between final and merge. */
export const STAGE_ORDER = ["spec", "build", "gate", "fix", "review", "re-review", "final", "merge"] as const;
export const MAX_GATE_ROUNDS = 3;
const UNKNOWN_RANK = STAGE_ORDER.indexOf("final") + 0.5;

const STAGE_ALIAS: Record<string, string> = { "final-review": "final", "security-review": "review" };

export function normStage(raw: string): string {
  const v = raw.trim().toLowerCase().replace(/_/g, "-");
  return STAGE_ALIAS[v] ?? v;
}

/** Which stage an attempt belongs to: its stage, else its kind, else its link, else "build". */
export function stageOf(a: { stage?: string | null; kind?: string | null; link?: string | null }): string {
  return normStage(a.stage || a.kind || a.link || "build");
}

export const STAGE_LABEL: Record<string, string> = {
  spec: "Spec", build: "Build", gate: "Gate", fix: "Fix", review: "Review", "re-review": "Re-review", final: "Final", merge: "Merge",
};
export const stageLabel = (s: string): string => STAGE_LABEL[s] ?? (s.charAt(0).toUpperCase() + s.slice(1));

const rankOf = (s: string): number => { const i = (STAGE_ORDER as readonly string[]).indexOf(s); return i < 0 ? UNKNOWN_RANK : i; };

const ACTIVE_STATES = new Set(["queued", "awaiting_approval", "starting", "working", "blocked"]);
const BAD_STATES = new Set(["failed", "interrupted", "cancelled"]);
const RUNNING_STATES = new Set(["queued", "starting", "working"]);

export type StageStatus = "done" | "now" | "bad" | "plan";

export interface ChainStage {
  name: string;
  label: string;
  status: StageStatus;
  model: string | null;
  account: string | null;
  node: string | null;
  attempts: number;
  /** "1 attempt", "attempt 2, working", "failed, 1 of 3", "planned". */
  detail: string;
  /** Ids of the attempts in this stage (empty when only planned). */
  taskIds: string[];
  /** The stage's latest attempt. */
  taskId: string | null;
  planned: boolean;
  /** Parsed review verdict of the stage's latest attempt, when it has one. */
  verdict: Verdict | null;
}

export function plannedChain(chain: WorkDetail["chain"]): PlannedStage[] {
  let value: unknown = chain;
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch { value = null; }
  }
  if (!Array.isArray(value)) return [];
  return value.filter((s): s is PlannedStage => !!s && typeof s === "object" && typeof (s as PlannedStage).stage === "string")
    .map((s) => ({ ...s, stage: normStage(s.stage) }));
}

const modelOf = (a: DetailAttempt): string | null => a.model || a.agent || null;
const accountOf = (a: DetailAttempt): string | null => a.account_label || a.account || null;
const stateWord = (s: string): string => s.replace(/_/g, " ");

/** Gate evidence across attempts: rounds run, failures, and the latest result. */
export function gateRounds(attempts: DetailAttempt[]): { rounds: number; failed: number; last: DetailAttempt | null; max: number } {
  const gated = attempts.filter((a) => a.gate);
  return { rounds: gated.length, failed: gated.filter((a) => a.gate?.passed === false).length, last: gated[gated.length - 1] ?? null, max: MAX_GATE_ROUNDS };
}

/**
 * The chain strip: one stage per name, actual attempts first, then planned stages that have not started.
 * Order is STAGE_ORDER, then first seen. Status comes from the stage's latest attempt; the gate stage from the
 * gate results (a failed round is "failed, 1 of 3").
 */
export function deriveChain(detail: Pick<WorkDetail, "attempts" | "chain">): ChainStage[] {
  const groups = new Map<string, DetailAttempt[]>();
  for (const a of detail.attempts) {
    const s = stageOf(a);
    groups.set(s, [...(groups.get(s) ?? []), a]);
  }
  const planned = plannedChain(detail.chain);
  const plan = new Map(planned.map((p) => [p.stage, p]));
  const gate = gateRounds(detail.attempts);
  const names = new Set<string>([...groups.keys(), ...plan.keys()]);
  if (gate.rounds > 0) names.add("gate");
  const first = new Map([...names].map((n, i) => [n, i]));

  const stages: ChainStage[] = [...names].map((name): ChainStage => {
    const p = plan.get(name);
    if (name === "gate" && gate.rounds > 0) return gateStage(gate, detail.attempts);
    const list = groups.get(name) ?? [];
    if (list.length === 0) {
      return { name, label: stageLabel(name), status: "plan", model: p?.model ?? p?.agent ?? null, account: p?.profile ?? null, node: null,
        attempts: 0, detail: "planned", taskIds: [], taskId: null, planned: true, verdict: null };
    }
    const last = list[list.length - 1];
    const status: StageStatus = ACTIVE_STATES.has(last.state) ? "now" : BAD_STATES.has(last.state) ? "bad" : "done";
    const n = list.length;
    const detailText = status === "now" ? `${n > 1 ? `attempt ${n}, ` : ""}${stateWord(last.state)}`
      : status === "bad" ? `${stateWord(last.state)}${n > 1 ? `, attempt ${n}` : ""}` : `${n} attempt${n > 1 ? "s" : ""}`;
    return { name, label: stageLabel(name), status, model: modelOf(last), account: accountOf(last), node: last.node, attempts: n, detail: detailText,
      taskIds: list.map((a) => a.id), taskId: last.id, planned: false, verdict: parseVerdict(last.result_excerpt) };
  });
  return stages.sort((a, b) => rankOf(a.name) - rankOf(b.name) || (first.get(a.name) ?? 0) - (first.get(b.name) ?? 0));
}

function gateStage(g: ReturnType<typeof gateRounds>, attempts: DetailAttempt[]): ChainStage {
  const last = g.last as DetailAttempt;
  const passed = last.gate?.passed;
  const active = ACTIVE_STATES.has(last.state);
  const status: StageStatus = passed === true ? "done" : passed === false ? "bad" : active ? "plan" : "now";
  const detail = passed === false ? `failed, ${g.failed} of ${g.max}` : passed === true ? `passed, round ${g.rounds}` : active ? "waiting for build" : "running";
  return { name: "gate", label: "Gate", status, model: "fleet-gate", account: last.node, node: last.node, attempts: g.rounds, detail,
    taskIds: attempts.filter((a) => a.gate).map((a) => a.id), taskId: last.id, planned: false, verdict: null };
}

/** The stage the work is at: the first one running, else the first failed, else the first planned; null when all done. */
export function currentStage(chain: ChainStage[]): ChainStage | null {
  return chain.find((s) => s.status === "now") ?? chain.find((s) => s.status === "bad") ?? chain.find((s) => s.status === "plan") ?? null;
}

export function chainSummary(chain: ChainStage[]): string {
  const cur = currentStage(chain);
  const done = chain.filter((s) => s.status === "done").length;
  return cur ? `${cur.label}: ${cur.detail} (${done} of ${chain.length} stages done)` : `All ${chain.length} stages done`;
}

// ---------- "Why?" ----------

export interface DecisionLike { id: number; task_id: string | null; decision: string; reason: string }

const STAGE_WHY: Record<string, string> = {
  spec: "Specs and plans are guidance work, so they go to the model that reviews rather than builds (paid-model rule 0).",
  build: "Builds default to Sonnet; Opus and Codex are for hard or security-critical builds only.",
  gate: "The runner runs the gate once after a build or fix. A failed round goes back to the builder; after 3 failed rounds it escalates to you.",
  fix: "Fix rounds keep the builder's model so the session context carries over. Max 3 gate rounds.",
  review: "The reviewer must differ from the builder. One paid reviewer per round; Codex only for security-sensitive changes.",
  "re-review": "Re-reviews go to the same reviewer and see only the delta since the last review.",
  final: "One final whole-branch review per batch, by Codex at medium or high effort.",
  merge: "Merges are mechanical, so Haiku or Sonnet, never Codex. They run after review and final are green.",
};

/** Controller decisions about a stage's tasks (budget refusals, queue waits, reroutes), then the standing rule for the stage. */
export function whyStage(stage: ChainStage, decisions: DecisionLike[]): { lines: string[]; source: string } {
  const mine = decisions.filter((d) => d.task_id && stage.taskIds.includes(d.task_id));
  const lines = mine.map((d) => `${d.decision}: ${d.reason}`);
  if (STAGE_WHY[stage.name]) lines.push(STAGE_WHY[stage.name]);
  if (stage.planned) lines.push("Planned only: nothing has started for this stage yet.");
  const source = mine.length ? `budget decision${mine.length > 1 ? "s" : ""} ${mine.map((d) => `#${d.id}`).join(", ")}` : stage.taskId ? `task ${stage.taskId}` : "work item chain";
  return { lines, source };
}

/** Anomaly: the same model built and reviewed (spec 6a). Null when there is no overlap. */
export function sameModelWarning(chain: ChainStage[]): string | null {
  const built = chain.filter((s) => (s.name === "build" || s.name === "fix") && !s.planned).map((s) => s.model).filter(Boolean);
  const reviewed = chain.filter((s) => ["review", "re-review", "final"].includes(s.name) && !s.planned).map((s) => s.model).filter(Boolean);
  const clash = reviewed.find((m) => built.includes(m));
  return clash ? `${clash} built and reviewed this work. A reviewer must differ from the builder unless the waiver is recorded.` : null;
}

// ---------- findings and verdicts from review text ----------

export type Severity = "HIGH" | "MED" | "LOW";
export interface Finding { severity: Severity; title: string; detail: string; cites: number[] }
export type Verdict = "approved" | "changes" | "rejected" | "abstain";

// opening bracket or bold (optional), severity, closing bracket and/or ":" or dash (optional), title. A bare
// "High level overview" has neither opener nor delimiter, so it is not a finding.
const FINDING_RE = /^\s*(?:[-*•]\s*)?([[(*#]+\s*)?(HIGH|MED(?:IUM)?|LOW)\b([\]):*]*\s*[:\-–—]?)\s*(.*)$/i;
const CITE_RE = /\[L(\d+)\]/g;

export function citesIn(text: string): number[] {
  return [...new Set([...text.matchAll(CITE_RE)].map((m) => Number(m[1])))].sort((a, b) => a - b);
}

/** HIGH/MED/LOW blocks from review output: "HIGH: title", an indented line or two of detail. */
export function parseFindings(text: string | null | undefined): Finding[] {
  const out: Finding[] = [];
  let open: Finding | null = null;
  for (const line of (text ?? "").split("\n")) {
    const m = FINDING_RE.exec(line);
    if (m && ((m[1] ?? "").trim() || (m[3] ?? "").trim())) {
      const sev = m[2].toUpperCase();
      open = { severity: sev.startsWith("MED") ? "MED" : (sev as Severity), title: (m[4] ?? "").replace(/\*+/g, "").trim(), detail: "", cites: [] };
      out.push(open);
    } else if (open && /^\s+\S/.test(line)) {
      open.detail = `${open.detail}${open.detail ? " " : ""}${line.trim()}`;
    } else if (!line.trim()) {
      continue;
    } else {
      open = null;
    }
  }
  for (const f of out) f.cites = citesIn(`${f.title} ${f.detail}`);
  return out;
}

export function severityCounts(findings: Finding[]): Record<Severity, number> {
  return { HIGH: findings.filter((f) => f.severity === "HIGH").length, MED: findings.filter((f) => f.severity === "MED").length, LOW: findings.filter((f) => f.severity === "LOW").length };
}

const VERDICT_RE = /verdict\s*[:=-]\s*\**\s*(approve[sd]?|lgtm|ship|pass(?:ed)?|changes[ _-]requested|request[ _-]changes|needs[ _-]changes|reject(?:ed)?|block(?:ed)?|abstain)/i;

export function parseVerdict(text: string | null | undefined): Verdict | null {
  const m = VERDICT_RE.exec(text ?? "");
  if (!m) return null;
  const v = m[1].toLowerCase();
  if (v.startsWith("approve") || v === "lgtm" || v === "ship" || v.startsWith("pass")) return "approved";
  if (v.startsWith("abstain")) return "abstain";
  if (v.startsWith("reject") || v.startsWith("block")) return "rejected";
  return "changes";
}

export const VERDICT_LABEL: Record<Verdict, string> = { approved: "Approved", changes: "Changes requested", rejected: "Rejected", abstain: "Abstained" };

// ---------- citations in answers ----------

export type AnswerPart = { kind: "text"; text: string } | { kind: "cite"; line: number };

/** Split an answer around `[L123]` so the cites can be buttons. */
export function splitCitations(answer: string): AnswerPart[] {
  const out: AnswerPart[] = [];
  let at = 0;
  for (const m of answer.matchAll(CITE_RE)) {
    const i = m.index ?? 0;
    if (i > at) out.push({ kind: "text", text: answer.slice(at, i) });
    out.push({ kind: "cite", line: Number(m[1]) });
    at = i + m[0].length;
  }
  if (at < answer.length) out.push({ kind: "text", text: answer.slice(at) });
  return out;
}

export const ASK_SUGGESTIONS = ["Why did the gate fail?", "What did the latest attempt change?", "What is left to do?"];

// ---------- diff files ----------

export interface DiffFile { path: string; added: number; removed: number; text: string }

/** Split a unified diff into files for the tree. Falls back to one pseudo-file when there are no file headers. */
export function splitDiff(diff: string): DiffFile[] {
  if (!diff.trim()) return [];
  const files: DiffFile[] = [];
  let cur: DiffFile | null = null;
  for (const line of diff.split("\n")) {
    const head = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (head) {
      cur = { path: head[2], added: 0, removed: 0, text: "" };
      files.push(cur);
    }
    if (!cur) {
      cur = { path: "(changes)", added: 0, removed: 0, text: "" };
      files.push(cur);
    }
    cur.text += `${line}\n`;
    if (line.startsWith("+") && !line.startsWith("+++")) cur.added++;
    else if (line.startsWith("-") && !line.startsWith("---")) cur.removed++;
  }
  return files;
}

// ---------- work list ----------

export type GroupId = "needs" | "running" | "paused" | "merge" | "done";
export const GROUP_LABEL: Record<GroupId, string> = { needs: "Needs you", running: "Running", paused: "Paused", merge: "Waiting merge", done: "Done" };
export const GROUP_ORDER: GroupId[] = ["needs", "running", "paused", "merge", "done"];

export const latestAttempt = (item: WorkItem) => item.attempts[item.attempts.length - 1];

/** The group a work item sits in, from its outcome and its latest attempt as the controller displays it. */
export function groupOf(item: WorkItem): GroupId {
  if (item.outcome) return "done";
  const last = latestAttempt(item);
  const tone = last?.display?.tone ?? item.display.tone;
  if (last && RUNNING_STATES.has(last.state)) return "running";
  if (tone === "attention") return "needs";
  if (tone === "paused") return "paused";
  if (tone === "ok" && last?.state === "review") return "merge";
  if (tone === "resolved" || tone === "muted") return "done";
  if (last && ACTIVE_STATES.has(last.state)) return "needs";
  return "done";
}

export function groupWork(items: WorkItem[]): { id: GroupId; label: string; items: WorkItem[] }[] {
  return GROUP_ORDER.map((id) => ({ id, label: GROUP_LABEL[id], items: items.filter((i) => groupOf(i) === id) })).filter((g) => g.items.length > 0);
}

/** One mark per attempt for the mini chain in the list: d done, b failed, n current, p planned (none yet). */
export function miniChain(item: WorkItem): ("d" | "b" | "n")[] {
  return item.attempts.map((a) => (RUNNING_STATES.has(a.state) || a.state === "blocked" || a.state === "awaiting_approval" ? "n" : BAD_STATES.has(a.state) ? "b" : "d"));
}

export const modelsUsed = (item: WorkItem): string[] => [...new Set(item.attempts.map((a) => a.model || a.agent).filter(Boolean))];

export interface WorkFilters { q: string; model: string; account: string; kind: string; view: string }
export const NO_FILTERS: WorkFilters = { q: "", model: "", account: "", kind: "", view: "all" };

export const VIEWS: { id: string; label: string; test: (i: WorkItem) => boolean }[] = [
  { id: "all", label: "All work", test: () => true },
  { id: "needs", label: "Needs me", test: (i) => groupOf(i) === "needs" },
  { id: "running", label: "Running", test: (i) => groupOf(i) === "running" },
  { id: "merge", label: "Waiting merge", test: (i) => groupOf(i) === "merge" },
  { id: "codex", label: "Codex-bound", test: (i) => i.attempts.some((a) => a.agent === "codex") },
  { id: "done", label: "Done", test: (i) => groupOf(i) === "done" },
];

export function filterWork(items: WorkItem[], f: WorkFilters): WorkItem[] {
  const q = f.q.trim().toLowerCase();
  const view = VIEWS.find((v) => v.id === f.view) ?? VIEWS[0];
  return items.filter((i) => {
    if (!view.test(i)) return false;
    if (q && !`${i.root_id} ${i.title} ${i.attempts.map((a) => `${a.id} ${a.title}`).join(" ")}`.toLowerCase().includes(q)) return false;
    if (f.model && !i.attempts.some((a) => (a.model || a.agent) === f.model)) return false;
    if (f.account && !i.attempts.some((a) => a.account_label === f.account)) return false;
    if (f.kind && !i.attempts.some((a) => (a.kind || a.link) === f.kind)) return false;
    return true;
  });
}

export function filterOptions(items: WorkItem[]): { models: string[]; accounts: string[]; kinds: string[] } {
  const uniq = (xs: (string | null | undefined)[]) => [...new Set(xs.filter((x): x is string => !!x))].sort();
  const all = items.flatMap((i) => i.attempts);
  return { models: uniq(all.map((a) => a.model || a.agent)), accounts: uniq(all.map((a) => a.account_label)), kinds: uniq(all.map((a) => a.kind || a.link)) };
}

/** The work item holding a task id (root or any attempt), for the legacy `?task=` deep link. */
export function findWorkRoot(items: WorkItem[], taskId: string): string | null {
  return items.find((i) => i.root_id === taskId || i.attempts.some((a) => a.id === taskId))?.root_id ?? null;
}

/** One sentence under the page title. */
export function workSummary(items: WorkItem[]): string {
  const g = (id: GroupId) => items.filter((i) => groupOf(i) === id).length;
  if (items.length === 0) return "No work items yet.";
  return `${items.length} work item${items.length === 1 ? "" : "s"}. ${g("needs")} need you, ${g("running")} running, ${g("merge")} waiting to merge.`;
}

// ---------- tokens ----------

export const tokenTotal = (t: Tokens | null | undefined): number => (t ? t.input + t.output + t.cache_read + t.cache_write : 0);
