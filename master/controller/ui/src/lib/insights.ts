// The intelligence layer behind the Now page (docs/SPEC-ui-revamp.md 6a). Pure rules over data the console already
// has: tasks, /api/live, /api/budget/status, /api/tokens. No model is called. Every insight carries evidence and a
// confidence, and a proposed action is only a request description: nothing here executes anything.
import type { Task } from "./api";
import { clockTime, modelLabel, type BudgetStatus, type ModelStatus } from "./budget";
import { duration } from "./format";
import type { Live, TokenRow, TokenSpend } from "./live";
import { displayOf } from "./lineage";

export interface Evidence { label: string; to?: string; href?: string }
export interface ApiRequest { method: "POST" | "PUT" | "DELETE"; path: string; body?: unknown }
export type InsightTone = "ok" | "warn" | "bad" | "accent";

export interface Inputs {
  now: number;
  tasks: Task[];
  live: Live | null;
  budget: BudgetStatus | null;
  tokenRows?: TokenRow[];
  spend?: TokenSpend | null;
  /** Approvals are admin-only; a non-admin sees the proposal without its button. */
  admin?: boolean;
}

export interface Proposal {
  id: string;
  kind: string;
  title: string;
  why: string;
  cost: string;
  confidence: number;
  rank: number;
  tone: InsightTone;
  evidence: Evidence[];
  action?: { label: string; request: ApiRequest; danger?: boolean };
}

const workLink = (t: Pick<Task, "id" | "root_id">): Evidence => ({ label: t.id, to: `/work/${t.root_id ?? t.id}` });
const visible = (tasks: Task[]) => tasks.filter((t) => !t.hidden);
const childrenOf = (tasks: Task[], id: string) => tasks.filter((t) => t.parent_id === id);
const RESUMABLE = new Set(["review", "failed", "blocked", "interrupted", "capped"]);
export const SONNET = "claude-sonnet-5-5";
export const FABLE = "claude-fable-5-2";

/** Reviewer must differ from the builder (CLAUDE.md pipeline 5): Sonnet built -> Fable; anything else -> Sonnet. */
export function pickReviewer(builder: Pick<Task, "agent" | "model">): { agent: "claude"; model: string } {
  return { agent: "claude", model: builder.agent === "claude" && /sonnet/i.test(builder.model ?? "") ? FABLE : SONNET };
}

export function reviewRequest(t: Task, reviewer = pickReviewer(t), kind = "review"): ApiRequest {
  return {
    method: "POST",
    path: "/api/tasks",
    body: {
      agent: reviewer.agent, model: reviewer.model, kind, parent_id: t.id, link: "review", review_of: t.id,
      mode: "read-only", repo: t.repo, title: `Review ${t.id}: ${t.title}`.slice(0, 120),
      prompt: `Review the diff of task ${t.id} (${t.title}). Report findings as HIGH/MED/LOW blocks. Do not edit files.`,
    },
  };
}

export function mergeRequest(t: Task): ApiRequest {
  return {
    method: "POST",
    path: "/api/tasks",
    body: {
      agent: "claude", model: SONNET, kind: "merge", parent_id: t.id, link: "merge", repo: t.repo,
      title: `Merge ${t.id}: ${t.title}`.slice(0, 120),
      prompt: `Merge branch ${t.branch ?? t.id} into main. Follow the Builder checklist in CLAUDE.md. Do not change files outside the merge.`,
    },
  };
}

const TRAP = /onboard|nodes\/runner|build\.py/i;
const tail = (s: string, n = 800) => (s.length > n ? s.slice(-n) : s);

export function gateFixRequest(t: Task): { request: ApiRequest; trap: boolean } {
  const out = t.gate_result?.output_tail ?? "";
  const trap = TRAP.test(out);
  const hint = trap ? "\nHint: if nodes/runner.py changed, run `python3 nodes/build.py` and commit onboard.sh and onboard.ps1." : "";
  return {
    trap,
    request: { method: "POST", path: `/api/tasks/${t.id}/message`, body: { text: `The gate failed. Fix it and commit on your branch.${hint}\n\n${tail(out)}` } },
  };
}

const gateFailed = (t: Task) => t.gate_result?.state === "failed";
const isReviewKind = (t: Task) => t.link === "review" || /review/.test(t.kind ?? "");

/** Ranked next best actions: approvals, gate failures, merges, reviews, offline-node drains. Highest rank first. */
export function nextActions(inp: Inputs): Proposal[] {
  const tasks = visible(inp.tasks);
  const out: Proposal[] = [];

  for (const t of tasks) {
    if (t.state === "awaiting_approval" || t.state === "blocked") {
      out.push({
        id: `approve:${t.id}`, kind: "Needs approval", rank: 90, confidence: 0.95, tone: "warn",
        title: `${t.state === "blocked" ? "Unblock" : "Approve"} ${t.id}: ${t.title}`,
        why: t.state === "blocked" ? "The task stopped after a denied tool and is waiting for you." : `It wants to start in ${t.mode} mode and cannot run until approved.`,
        cost: "No extra cost; it starts or resumes the task.",
        evidence: [workLink(t)],
        action: inp.admin ? { label: "Approve", request: { method: "POST", path: `/api/tasks/${t.id}/approve` } } : undefined,
      });
    }
    if (gateFailed(t) && RESUMABLE.has(t.state) && !t.outcome) {
      const { request, trap } = gateFixRequest(t);
      out.push({
        id: `gate:${t.id}`, kind: "Gate failed", rank: 85, confidence: trap ? 0.9 : 0.75, tone: "bad",
        title: `Send the gate failure on ${t.id} back to its builder`,
        why: trap ? "The gate output matches a known trap: the runner was changed but not re-embedded (run nodes/build.py)." : "The gate failed; the builder has the output and one fix round left to use.",
        cost: "One builder turn on the same session.",
        evidence: [workLink(t)],
        action: { label: "Send to builder", request },
      });
    }
    const buildish = !isReviewKind(t) && t.link !== "merge" && t.link !== "check" && !t.outcome;
    if (buildish && t.state === "review" && displayOf(t).tone === "ok" && t.gate_result?.state !== "failed") {
      const kids = childrenOf(tasks, t.id);
      const review = kids.find((k) => k.link === "review" && ["done", "review"].includes(k.state) && displayOf(k).tone === "ok");
      const pending = kids.some((k) => k.link === "review" && !["done", "review", "failed", "cancelled"].includes(k.state));
      const merging = kids.some((k) => k.link === "merge");
      if (review && !merging) {
        out.push({
          id: `merge:${t.id}`, kind: "Ready to merge", rank: 80, confidence: t.gate_result?.state === "passed" ? 0.8 : 0.6, tone: "ok",
          title: `Merge ${t.id}: ${t.title}`,
          why: `${t.gate_result?.state === "passed" ? "Gate passed" : "No gate recorded"} and ${review.id} (${review.model ?? review.agent}) finished clean.`,
          cost: "One Sonnet merge task.", evidence: [workLink(t), workLink(review)],
          action: { label: "Spawn merge", request: mergeRequest(t) },
        });
      } else if (!review && !pending) {
        const r = pickReviewer(t);
        out.push({
          id: `review:${t.id}`, kind: "Needs review", rank: 70, confidence: 0.7, tone: "accent",
          title: `Review ${t.id} with ${modelLabel(`${r.agent}:${r.model}`)}`,
          why: `Built by ${t.model ?? t.agent}; the reviewer must differ from the builder.`,
          cost: "One paid review round.", evidence: [workLink(t)],
          action: { label: "Spawn review", request: reviewRequest(t, r) },
        });
      }
    }
  }

  const queueByNode = new Map<string, number>();
  for (const q of inp.live?.queue ?? []) queueByNode.set(q.node, (queueByNode.get(q.node) ?? 0) + 1);
  for (const m of inp.live?.machines ?? []) {
    const queued = queueByNode.get(m.node) ?? 0;
    if (m.offline && queued > 0) {
      out.push({
        id: `drain:${m.node}`, kind: "Node offline", rank: 60, confidence: 0.6, tone: "warn",
        title: `Drain ${m.name ?? m.node}`,
        why: `${m.name ?? m.node} is offline (${m.offline}) and ${queued} queued task${queued === 1 ? "" : "s"} are waiting on it. Draining lets the scheduler place them elsewhere.`,
        cost: "No cost; approve the node again when it is back.",
        evidence: [{ label: m.name ?? m.node, to: `/nodes/${m.node}` }],
        action: { label: "Drain node", request: { method: "POST", path: `/api/nodes/${encodeURIComponent(m.node)}/drain` }, danger: true },
      });
    }
  }
  return out.sort((a, b) => b.rank - a.rank || a.id.localeCompare(b.id));
}

// ---------- anomalies ----------

export interface Anomaly { id: string; severity: "warn" | "bad"; title: string; detail: string; confidence: number; evidence: Evidence[] }
export const STALL_S = 30 * 60;
export const FLAP_COUNT = 3;
export const FLAP_WINDOW_S = 3 * 3600;

export function anomalies(inp: Inputs): Anomaly[] {
  const tasks = visible(inp.tasks);
  const out: Anomaly[] = [];

  for (const t of tasks) {
    if ((t.state === "working" || t.state === "starting") && inp.now - t.updated_at > STALL_S) {
      out.push({
        id: `stall:${t.id}`, severity: "warn", confidence: 0.5, evidence: [workLink(t)],
        title: `${t.id} looks stalled`,
        detail: `Running with no update for ${duration(inp.now - t.updated_at)}. It may just be a long step; check the log before acting.`,
      });
    }
  }

  const gateGroups = new Map<string, Task[]>();
  for (const t of tasks) {
    if (!gateFailed(t)) continue;
    const sig = (t.gate_result?.output_tail ?? "").split("\n").map((l) => l.trim()).filter(Boolean).pop() ?? "";
    const key = `${t.root_id ?? t.id}|${sig.slice(0, 80)}`;
    gateGroups.set(key, [...(gateGroups.get(key) ?? []), t]);
  }
  for (const [key, ts] of gateGroups) {
    if (ts.length < 2) continue;
    out.push({
      id: `gate-repeat:${key}`, severity: "bad", confidence: 0.75, evidence: ts.map(workLink),
      title: `Gate keeps failing the same way on ${ts[0].root_id ?? ts[0].id}`,
      detail: `${ts.length} attempts ended on the same last gate line. The pipeline caps gate rounds at 3, then it needs you.`,
    });
  }

  const byNode = new Map<string, number>();
  for (const a of inp.live?.alerts ?? []) {
    if (a.type === "node.runner_offline" && a.node && inp.now - a.ts <= FLAP_WINDOW_S) byNode.set(a.node, (byNode.get(a.node) ?? 0) + 1);
  }
  for (const [node, n] of byNode) {
    if (n >= FLAP_COUNT) {
      out.push({
        id: `flap:${node}`, severity: "warn", confidence: 0.7, evidence: [{ label: node, to: `/nodes/${node}` }],
        title: `${node} is flapping`,
        detail: `Runner went offline ${n} times in the last ${duration(FLAP_WINDOW_S)}. Check its network or power before sending it more work.`,
      });
    }
  }

  for (const r of inp.tokenRows ?? []) {
    if (!r.alerts?.length) continue;
    out.push({
      id: `tokens:${r.task_id}`, severity: "warn", confidence: 0.9, evidence: [{ label: r.task_id, to: `/work/${r.task_id}` }],
      title: `Token use on ${r.task_id} is high`,
      detail: r.alerts.map((a) => (a === "turn1_context" ? "turn-1 context over the 8k lean target" : a.replace(/_80_percent$/, " past 80% of its cap"))).join("; "),
    });
  }

  const byId = new Map(tasks.map((t) => [t.id, t]));
  for (const t of tasks) {
    if (!isReviewKind(t) || !t.parent_id) continue;
    const parent = byId.get(t.parent_id);
    if (parent && parent.agent === t.agent && parent.model && parent.model === t.model) {
      out.push({
        id: `same-model:${t.id}`, severity: "warn", confidence: 0.9, evidence: [workLink(parent), workLink(t)],
        title: `Same model built and reviewed ${parent.id}`,
        detail: `${parent.model} did both. The pipeline requires a different reviewer, or a recorded waiver.`,
      });
    }
  }
  return out.sort((a, b) => (a.severity === b.severity ? b.confidence - a.confidence : a.severity === "bad" ? -1 : 1));
}

// ---------- forecasts ----------

export interface Forecast {
  id: string;
  label: string;
  headline: string;
  detail: string;
  tone: InsightTone;
  confidence: number;
  /** Seconds until the limit at the current burn rate; null when it will not be reached or is unknown. */
  etaS: number | null;
  evidence: Evidence[];
}

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
export const MIN_ELAPSED_S = 1800;

function startOfDay(now: number): number {
  const d = new Date(now * 1000);
  d.setHours(0, 0, 0, 0);
  return d.getTime() / 1000;
}

/** Daily-token forecast for one model: time to its soft (else hard) budget at today's average burn. */
export function dailyForecast(m: ModelStatus, now: number): Forecast | null {
  const limit = m.soft ?? m.hard;
  if (!limit) return null;
  const which = m.soft != null ? "soft" : "hard";
  const base = { id: `daily:${m.key}`, label: modelLabel(m.key), evidence: [{ label: "Budget", to: "/budget" }] };
  if (m.today_tokens >= limit) {
    return { ...base, headline: `At ${which} budget now`, detail: `${m.today_tokens.toLocaleString()} of ${limit.toLocaleString()} tokens used today.`, tone: "bad", confidence: 1, etaS: 0 };
  }
  const elapsed = now - startOfDay(now);
  if (m.today_tokens <= 0 || elapsed < MIN_ELAPSED_S) {
    return { ...base, headline: "Not enough data yet", detail: "Needs about 30 minutes of today's usage to project.", tone: "ok", confidence: 0.2, etaS: null };
  }
  const rate = m.today_tokens / elapsed;
  const eta = (limit - m.today_tokens) / rate;
  const confidence = clamp(elapsed / 21_600, 0.2, 0.85);
  if (now + eta >= startOfDay(now) + 86_400) {
    return { ...base, headline: `On track to stay under the ${which} budget`, detail: `Burning ${Math.round(rate * 3600).toLocaleString()} tokens/h; the day ends first.`, tone: "ok", confidence, etaS: null };
  }
  return { ...base, headline: `${which === "soft" ? "Soft" : "Hard"} budget in ${duration(eta)}`, detail: `At ${Math.round(rate * 3600).toLocaleString()} tokens/h, around ${clockTime(now + eta)}.`, tone: eta < 3600 ? "bad" : "warn", confidence, etaS: eta };
}

/** Codex-style rate-limit windows: project used_percent to 100% before the window resets. */
export function windowForecast(m: ModelStatus, now: number): Forecast | null {
  let best: { label: string; used: number; resets: number; etaS: number | null; confidence: number } | null = null;
  for (const a of m.accounts ?? []) {
    for (const [name, w] of [["5-hour", a.rate_limits?.primary], ["weekly", a.rate_limits?.secondary]] as const) {
      if (!w) continue;
      const minutes = w.window_minutes ?? (name === "5-hour" ? 300 : 10080);
      const elapsed = now - (w.resets_at - minutes * 60);
      let etaS: number | null = null;
      if (elapsed >= MIN_ELAPSED_S && w.used_percent > 0) {
        const eta = ((100 - w.used_percent) / (w.used_percent / elapsed));
        if (now + eta < w.resets_at) etaS = eta;
      }
      const cand = { label: name, used: w.used_percent, resets: w.resets_at, etaS, confidence: clamp(elapsed / (minutes * 60), 0.2, 0.8) };
      if (!best || (cand.etaS !== null && (best.etaS === null || cand.etaS < best.etaS)) || (cand.etaS === null && best.etaS === null && cand.used > best.used)) best = cand;
    }
  }
  if (!best) return null;
  const base = { id: `window:${m.key}`, label: `${modelLabel(m.key)} ${best.label} window`, confidence: best.confidence, etaS: best.etaS, evidence: [{ label: "Budget", to: "/budget" }] };
  const used = `${Math.round(best.used)}% used, resets ${clockTime(best.resets)}`;
  if (best.etaS !== null) return { ...base, headline: `Limit in ${duration(best.etaS)}`, detail: `${used}. At the current rate it runs out before the reset.`, tone: best.etaS < 3600 ? "bad" : "warn" };
  return { ...base, headline: `${Math.round(best.used)}% of ${best.label} used`, detail: `${used}. The window resets before it runs out at this rate.`, tone: best.used >= 80 ? "warn" : "ok" };
}

export function queueForecast(live: Live | null, now: number): Forecast | null {
  const q = live?.queue ?? [];
  if (!q.length) return null;
  const count = (w: string) => q.filter((r) => r.wait === w).length;
  const limited = q.filter((r) => r.wait === "limit" && r.until);
  const release = limited.length ? Math.max(...limited.map((r) => r.until as number)) : null;
  const oldest = Math.max(...q.map((r) => r.queued_s));
  const parts = [["approval", "need approval"], ["limit", "wait for a limit reset"], ["slot", "wait for a slot"], ["node", "wait on a node"]]
    .map(([w, l]) => (count(w) ? `${count(w)} ${l}` : "")).filter(Boolean);
  return {
    id: "queue", label: "Queue", headline: `${q.length} queued, oldest ${duration(oldest)}`,
    detail: `${parts.join(", ")}${release ? `. Last limit clears ${clockTime(release)}.` : "."}`,
    tone: count("approval") ? "warn" : "ok", confidence: release ? 0.7 : 0.4,
    etaS: release ? Math.max(0, release - now) : null, evidence: [{ label: "Live queue", to: "/" }],
  };
}

const STAGE_KIND: Record<string, string> = { build: "build", fix: "fix", review: "review", final: "final_review", merge: "merge" };

/** Per work item: remaining stages and a typical-token estimate from today's spend by kind. */
export function workForecasts(live: Live | null, spend: TokenSpend | null | undefined, limit = 3): Forecast[] {
  const out: Forecast[] = [];
  for (const c of live?.chains ?? []) {
    const left = c.stages.filter((s) => s.state === "current" || s.state === "next");
    if (!left.length) continue;
    let tokens = 0;
    let known = 0;
    for (const s of left) {
      const row = spend?.by_kind?.[STAGE_KIND[s.stage] ?? ""];
      if (row && row.tasks > 0) { tokens += (row.input + row.output) / row.tasks; known += 1; }
    }
    const root = c.root_id ?? c.pipeline_id ?? "";
    out.push({
      id: `work:${root || c.title}`, label: c.title, headline: `${left.length} stage${left.length === 1 ? "" : "s"} left: ${left.map((s) => s.stage).join(", ")}`,
      detail: known ? `About ${Math.round(tokens / 1000).toLocaleString()}k tokens at typical cost (${known} of ${left.length} stages have history).` : "No spend history for these stages yet.",
      tone: "ok", confidence: known ? clamp(0.25 + 0.15 * known, 0.25, 0.6) : 0.15, etaS: null,
      evidence: root ? [{ label: root, to: `/work/${root}` }] : [],
    });
  }
  return out.slice(0, limit);
}

export function forecasts(inp: Inputs): Forecast[] {
  const out: Forecast[] = [];
  for (const m of inp.budget?.models ?? []) {
    if (m.state === "limited") {
      out.push({
        id: `limited:${m.key}`, label: modelLabel(m.key), headline: `Limited${m.limited_until ? ` until ${clockTime(m.limited_until)}` : ""}`,
        detail: "Work for this model is queued or rerouted until the reset.", tone: "bad", confidence: 1,
        etaS: m.limited_until ? Math.max(0, m.limited_until - inp.now) : null, evidence: [{ label: "Budget", to: "/budget" }],
      });
      continue;
    }
    const f = windowForecast(m, inp.now) ?? dailyForecast(m, inp.now);
    if (f) out.push(f);
  }
  const q = queueForecast(inp.live, inp.now);
  if (q) out.push(q);
  out.push(...workForecasts(inp.live, inp.spend));
  return out;
}

// ---------- briefing ----------

export interface Briefing { sentences: string[]; confidence: number; evidence: Evidence[] }

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Three to five plain sentences. Derived only from the inputs, so the same data gives the same words. */
export function briefing(inp: Inputs): Briefing {
  const s: string[] = [];
  const tasks = visible(inp.tasks);
  const needs = tasks.filter((t) => t.state === "awaiting_approval" || t.state === "blocked" || (displayOf(t).tone === "attention" && !["working", "starting", "queued"].includes(t.state)));
  s.push(needs.length ? `${plural(needs.length, "item")} need${needs.length === 1 ? "s" : ""} you.` : "Nothing needs you.");

  const running = (inp.live?.workers ?? []).filter((w) => w.task_id).length;
  const queued = inp.live?.queue.length ?? 0;
  s.push(`${plural(running, "task")} running and ${queued} queued.`);

  for (const m of inp.budget?.models ?? []) {
    if (s.length >= 4) break;
    if (m.state === "limited") s.push(`${modelLabel(m.key)} is limited${m.limited_until ? ` until ${clockTime(m.limited_until)}` : ""}.`);
    else if (m.state === "soft") s.push(`${modelLabel(m.key)} is at its soft budget.`);
    else {
      const w = windowForecast(m, inp.now);
      const used = Math.max(0, ...(m.accounts ?? []).flatMap((a) => [a.rate_limits?.primary?.used_percent ?? 0, a.rate_limits?.secondary?.used_percent ?? 0]));
      if (w && used >= 70) s.push(`${modelLabel(m.key)} is at ${Math.round(used)}% of a rate window${w.etaS !== null ? `, limit in about ${duration(w.etaS)}` : ""}.`);
    }
  }

  const queueByNode = new Map<string, number>();
  for (const q of inp.live?.queue ?? []) queueByNode.set(q.node, (queueByNode.get(q.node) ?? 0) + 1);
  for (const m of inp.live?.machines ?? []) {
    if (s.length >= 4 || !m.offline) continue;
    const n = queueByNode.get(m.node) ?? 0;
    s.push(`${m.name ?? m.node} is offline${m.age_s ? ` (last data ${duration(m.age_s)} ago)` : ""}${n ? `, ${plural(n, "queued task")} waiting on it` : ""}.`);
  }

  const an = anomalies(inp).length;
  if (an) s.push(`${plural(an, "anomaly", "anomalies")} flagged below.`);
  const known = inp.live !== null && inp.budget !== null;
  return { sentences: s.slice(0, 5), confidence: known ? 0.9 : 0.5, evidence: [{ label: "Live", to: "/" }, { label: "Budget", to: "/budget" }] };
}
