// The Live page (docs/SPEC-live-monitor.md 5): types for GET /api/live and the pure helpers the page renders with.
import { clockTime } from "./budget";
import { ago, duration } from "./format";

export type Tone = "ok" | "warn" | "bad" | "info" | "accent" | "quiet";

export interface Session {
  session: string;
  model: string | null;
  account_label: string | null;
  last_seen: number;
  stale: boolean;
  waiting_on: string[] | null;
  last_wait: { ids: string[]; ago_s: number } | null;
}

export interface Worker {
  task_id: string | null;
  agent: string;
  node: string | null;
  node_name?: string | null;
  profile: string | null;
  account_label: string | null;
  // running rows
  title?: string;
  model?: string | null;
  kind?: string | null;
  effort?: string | null;
  stage?: string | null;
  work_item?: string;
  branch?: string | null;
  started_at?: number | null;
  elapsed_s?: number | null;
  independence?: "waived" | null;
  waiver_reason?: string | null;
  // idle rows
  idle_since?: number | null;
  limited_until?: number | null;
  signed_in?: boolean | null;
  offline?: string | null;
}

export interface QueueRow {
  task_id: string;
  title: string;
  agent: string;
  model: string | null;
  account_label: string | null;
  kind: string | null;
  stage: string | null;
  node: string;
  wait: "approval" | "limit" | "slot" | "node" | "starting";
  reason: string | null;
  until: number | null;
  queued_s: number;
}

export interface Stage {
  stage: string;
  state: "done" | "current" | "next" | "failed" | "superseded";
  count?: number;
  task_id?: string | null;
  agent?: string;
  model?: string;
}

export interface Chain {
  pipeline_id?: string;
  root_id?: string;
  title: string;
  state?: string;
  declared?: boolean;
  stages: Stage[];
}

export interface Account {
  account: string;
  agent: string;
  label: string | null;
  last_limit: { at: number; resets_at: number | null } | null;
  limited_until: number | null;
  reset_at: number | null;
  since: number;
  tokens_since: { input: number | null; output: number | null; cached: number | null };
  self_reported_tokens: { input: number; output: number; cached: number };
  running: number;
  status: string;
}

export interface Activity {
  id: string;
  ts: number;
  ended_at: number | null;
  title: string;
  kind: string;
  account_label: string;
  model: string | null;
  work_item: string | null;
  status: "running" | "done" | "failed";
  tokens: { input?: number; output?: number; cached?: number } | null;
  note: string | null;
  self_reported: true;
}

export interface Machine {
  node: string;
  name: string | null;
  age_s: number | null;
  unknown: boolean;
  mem_total_gb: number | null;
  mem_used_gb: number | null;
  swap_used_gb: number | null;
  disk_free_gb: number | null;
  load5: number | null;
  cores: number | null;
  ollama_up: boolean | null;
  loaded: string[];
  warm: string[];
  warnings: ("swap" | "disk" | "load")[];
  offline: string | null;
  heartbeat: "ok" | "lost, polling" | null;
}

export interface Alert {
  id: number;
  ts: number;
  task_id: string | null;
  type: string;
  title: string | null;
  node: string | null;
  state: string | null;
}

export interface Live {
  generated_at: number;
  orchestrator: Session[];
  workers: Worker[];
  queue: QueueRow[];
  chains: Chain[];
  accounts: Account[];
  self_reported: Activity[];
  machines: Machine[];
  alerts: Alert[];
}

/** 20 s fallback poll (overrides the console's 30 s), and nothing at all while the tab is hidden (§5). */
export const LIVE_POLL_MS = 20_000;
export function liveQueryOptions(visible: boolean) {
  return { enabled: visible, refetchInterval: visible ? LIVE_POLL_MS : (false as const) };
}

const short = (model: string | null | undefined): string =>
  !model ? "default model" : model.replace(/^claude-/, "").replace(/-(\d+)-(\d+)$/, " $1.$2");

/** "Orchestrator (opus 5.5, configured; spare), last seen 2m ago, waiting on t-1". */
export function presenceText(s: Session, now = Date.now() / 1000): string {
  const who = [s.model ? `${short(s.model)}, configured` : null, s.account_label].filter(Boolean).join("; ");
  const parts = [`Orchestrator${who ? ` (${who})` : ""}`, `last seen ${ago(s.last_seen, now)}`];
  if (s.waiting_on?.length) parts.push(`waiting on ${s.waiting_on.join(", ")}`);
  else if (s.last_wait) parts.push(`last waited on ${s.last_wait.ids.join(", ")} ${duration(s.last_wait.ago_s)} ago`);
  return parts.join(", ");
}

export interface RowText {
  primary: string;
  secondary: string;
  meta: string;
  tone: Tone;
  flag?: string;
}

/** The short text of one worker row; the detail goes in its title (hover/tap). */
export function workerText(w: Worker, now = Date.now() / 1000): RowText {
  if (w.task_id) {
    const account = w.account_label ?? w.profile ?? (w.agent === "ollama" ? "local" : "fleet");
    return {
      primary: w.title ?? w.task_id,
      secondary: `${account} · ${w.agent === "claude" ? short(w.model) : `${w.agent}${w.model ? ` ${w.model}` : ""}`}`,
      meta: [w.kind, w.effort, w.stage].filter(Boolean).join(" · ") || "—",
      tone: "info",
      flag: w.independence === "waived" ? "independence waived" : undefined,
    };
  }
  const where = w.node_name ?? w.node ?? "no node";
  const who = `${w.agent}${w.account_label ? ` (${w.account_label})` : w.profile ? ` (${w.profile})` : ""}`;
  if (w.offline) return { primary: who, secondary: where, meta: w.offline === "no node has Codex" ? "no node has Codex" : "offline", tone: "quiet" };
  if (w.signed_in === false) return { primary: who, secondary: where, meta: "not signed in", tone: "bad" };
  if (w.limited_until && w.limited_until > now)
    return { primary: who, secondary: where, meta: `limited until ${clockTime(w.limited_until)}`, tone: "bad" };
  return { primary: who, secondary: where, meta: w.idle_since ? `idle since ${ago(w.idle_since, now)}` : "idle", tone: "ok" };
}

export const QUEUE_LABEL: Record<QueueRow["wait"], string> = {
  approval: "needs your approval",
  limit: "waiting for a limit reset",
  slot: "waiting for a slot",
  node: "node can't take it yet",
  starting: "starting",
};

export function queueText(q: QueueRow): string {
  if (q.wait === "limit" && q.until) return `limit until ${clockTime(q.until)}`;
  if (q.wait === "approval" || !q.reason) return QUEUE_LABEL[q.wait];
  if (q.wait === "slot" || q.reason.toLowerCase().startsWith("waiting")) return q.reason;  // already says it
  return `${QUEUE_LABEL[q.wait]}: ${q.reason}`;
}

/** A stage chip: its class (current highlighted, failed red) and its label, with "×2" for a repeat. */
export function stageChip(s: Stage): { className: string; label: string } {
  const repeat = (s.count ?? 0) > 1 ? ` ×${s.count}` : "";
  return { className: `stage stage-${s.state}`, label: `${s.stage}${repeat}` };
}

export function accountTone(a: Account): Tone {
  if (a.status.startsWith("limited")) return "bad";
  if (a.status.startsWith("limit hit")) return "warn";
  if (a.status.startsWith("reset by owner")) return "info";
  return "ok";
}

const WARNING_LABEL = { swap: "Swap high", disk: "Disk low", load: "Load high" } as const;

/** What the machine row warns about; stale data says "unknown" rather than an old value. */
export function machineWarnings(m: Machine): { tone: Tone; label: string }[] {
  if (m.offline) return [{ tone: "bad", label: "Offline" }];
  if (m.unknown) return [{ tone: "quiet", label: m.age_s == null ? "No metrics yet" : `Unknown (data ${duration(m.age_s)} old)` }];
  const out: { tone: Tone; label: string }[] = m.warnings.map((w) => ({ tone: "bad" as Tone, label: WARNING_LABEL[w] }));
  if (m.heartbeat === "lost, polling") out.push({ tone: "warn", label: "Heartbeat lost, polling" });
  return out;
}

/** A meter's fill (0..1) for used of total; null when either is unknown. */
export function fill(used: number | null, total: number | null): number | null {
  if (used == null || !total) return null;
  return Math.max(0, Math.min(1, used / total));
}

export const ALERT_LABEL: Record<string, string> = {
  "task.awaiting_approval": "Needs approval",
  "task.blocked": "Blocked",
  "task.failed": "Failed",
  "budget.limit": "Usage limit",
  "budget.rules_invalid": "Budget rules invalid",
  "budget.limit_unrecognised": "Unrecognised limit message",
  "node.swap_high": "Swap high",
  "node.disk_low": "Disk low",
  "node.runner_offline": "Runner offline",
};

export function alertText(a: Alert): string {
  const what = ALERT_LABEL[a.type] ?? a.type;
  const cleared = a.state === "cleared" ? " (cleared)" : "";
  return `${what}${cleared}${a.title ? `: ${a.title}` : a.node ? `: ${a.node}` : ""}`;
}

/** Rows collapse to cards at phone width (no horizontal page scroll, §5). */
export const rowsClass = (phone: boolean) => (phone ? "live-rows live-cards" : "live-rows");
