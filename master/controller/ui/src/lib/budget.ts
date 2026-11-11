// Budget page logic (docs/SPEC-paid-budget.md section 5). Pure functions only, so they're testable.

export const KINDS = [
  "build", "fix", "review", "re_review", "final_review", "security_review", "merge", "benchmark", "check", "draft",
] as const;
export type Kind = (typeof KINDS)[number];
export const EFFORTS = ["low", "medium", "high"] as const;
export const TIERS = ["cheap", "standard", "premium", "scarce"] as const;

export interface ModelRule {
  tier: (typeof TIERS)[number];
  max_concurrent?: number;
  kinds?: string[];
  default_effort?: Record<string, string>;
  daily_tokens_soft?: number | null;
  daily_tokens_hard?: number | null;
}

export interface BudgetRules {
  models: Record<string, ModelRule>;
  unlisted: "allow" | "refuse";
  prompt: { max_kb: number; hard_max_kb: number; max_line_chars: number; exclude_globs: string[] };
  limits: {
    on_limit: "queue" | "fallback" | "refuse";
    fallbacks: Record<string, string[]>;
    // A task that stops at a usage limit (docs/SPEC-limit-handoff.md 4.2); kept as stored by the form.
    on_task_limit?: Record<string, "handoff" | "wait">;
    max_handoffs?: number;
    allow_cross_vendor?: boolean;
  };
  effort_ladder: Record<string, string[]>;
  limit_scope: "model" | "account";
  review_exec: { tools: string[]; max_turns: number | null; task_minutes: number | null };
  reviews: {
    stages: string[];
    max_paid_rounds: number;
    max_codex_rounds: number;
    paid_skip_when: { max_changed_lines?: number; paths_only?: string[] } | null;
    security_paths: string[];
  };
}

export interface RulesVersion { version: number; body: BudgetRules; author: string | null; created_at: number | null }

export interface ModelStatus {
  key: string;
  agent: string;
  tier: string | null;
  state: "ok" | "limited" | "soft";
  limited_until: number | null;
  limit_scope: "model" | "account" | "daily" | null;
  running: number;
  max_concurrent: number | null;
  today_tokens: number;
  soft: number | null;
  hard: number | null;
}

export interface BudgetStatus {
  fleet_only: boolean;
  models: ModelStatus[];
  rules_version: number;
  rules_invalid?: boolean;
  rules_error?: string | null;
  profiles?: { node: string; name: string; codex_profile: string | null; claude_profile: string | null }[];
}

export interface BudgetUsageRow {
  model?: string;
  kind?: string;
  day?: string;
  agent: string | null;
  tasks: number;
  input: number;
  output: number;
  cached: number;
  cost_usd: number | null;
  limit_hits: number;
  local: boolean;
  avg_prompt_kb: number | null;
}

export interface BudgetUsage { days: number; group: string; rows: BudgetUsageRow[] }

export interface Savings {
  days: number;
  local_tokens: number;
  paid_tokens: number;
  local_reviews: number;
  paid_rounds_avoided: number;
  paid_reviews_skipped: number;
  prompt_bytes_stripped: number;
  delta_reviews: number;
  delta_bytes_saved: number;
}

export interface Decision {
  id: number;
  ts: number;
  task_id: string | null;
  decision: "refused" | "queued" | "rerouted";
  reason: string;
  title?: string;
  kind?: string;
}

export interface LocalReviewer {
  model: string;
  roles: string[];
  trusted: boolean;
  g2_precision: number | null;
  g2_recall: number | null;
  live: { findings: number; confirmed: number; misses: number; precision: number | null; recall: number | null };
}

// ---------- formatting ----------

export function formatTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function clockTime(ts: number): string {
  const d = new Date(ts * 1000);
  const h = d.getHours();
  return `${h % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
}

/** "Codex", "Claude Opus", ... from a rules key. */
export function modelLabel(key: string): string {
  if (key === "codex") return "Codex";
  const [agent, glob] = key.split(":", 2);
  if (!glob) return agent.charAt(0).toUpperCase() + agent.slice(1);
  const family = glob.replace(/^claude-/, "").replace(/[-*]+$/, "").split("-")[0];
  const name = agent.charAt(0).toUpperCase() + agent.slice(1);
  return family && family !== "*" ? `${name} ${family.charAt(0).toUpperCase()}${family.slice(1)}` : name;
}

export type Tone = "ok" | "warn" | "bad" | "quiet";

/** One status card per paid model: OK, limited until a time, or at its soft budget. */
export function statusCard(s: ModelStatus): { tone: Tone; label: string } {
  if (s.state === "limited") {
    const when = s.limited_until ? ` until ${clockTime(s.limited_until)}` : "";
    if (s.limit_scope === "account") return { tone: "bad", label: `Account limited${when}` };
    if (s.limit_scope === "daily") return { tone: "bad", label: `Daily budget used${when}` };
    return { tone: "bad", label: `Limited${when}` };
  }
  if (s.state === "soft") return { tone: "warn", label: "At soft budget" };
  return { tone: "ok", label: "OK" };
}

/** Today's tokens against the soft (or hard) budget. pct is null when no budget is set. */
export function meter(used: number, soft: number | null, hard: number | null): { pct: number | null; tone: Tone; label: string } {
  const limit = soft ?? hard;
  if (!limit) return { pct: null, tone: "quiet", label: `${formatTokens(used)} today, no daily budget set` };
  const tone: Tone = hard != null && used >= hard ? "bad" : soft != null && used >= soft ? "warn" : "ok";
  return { pct: Math.min(1, used / limit), tone, label: `${formatTokens(used)} of ${formatTokens(limit)}` };
}

export function concurrencyText(s: ModelStatus): string {
  return `${modelLabel(s.key)} ${s.running}/${s.max_concurrent ?? "∞"}`;
}

export interface KindRow { kind: string; tasks: number; tokens: string; avgPrompt: string; limitHits: number }

/** The table by task kind, busiest first. */
export function kindRows(rows: BudgetUsageRow[]): KindRow[] {
  return [...rows]
    .sort((a, b) => b.input + b.output - (a.input + a.output) || (a.kind ?? "").localeCompare(b.kind ?? ""))
    .map((r) => ({
      kind: (r.kind ?? "unknown").replace(/_/g, " "),
      tasks: r.tasks,
      tokens: formatTokens(r.input + r.output),
      avgPrompt: r.avg_prompt_kb == null ? "—" : `${r.avg_prompt_kb} KB`,
      limitHits: r.limit_hits,
    }));
}

export const OTHER = "Other";

/** Per-day stacks of tokens by model: the biggest `maxSeries` models keep their own colour, the rest fold into Other. */
export function stackDays(rows: BudgetUsageRow[], maxSeries = 3): { series: string[]; days: { day: string; total: number; parts: { model: string; tokens: number }[] }[] } {
  const totals = new Map<string, number>();
  for (const r of rows) totals.set(r.model ?? "?", (totals.get(r.model ?? "?") ?? 0) + r.input + r.output);
  const ranked = [...totals.entries()].filter(([, t]) => t > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([m]) => m);
  const keep = ranked.slice(0, maxSeries);
  const series = ranked.length > maxSeries ? [...keep, OTHER] : keep;
  const byDay = new Map<string, Map<string, number>>();
  for (const r of rows) {
    const tokens = r.input + r.output;
    if (!tokens || !r.day) continue;
    const model = keep.includes(r.model ?? "?") ? (r.model ?? "?") : OTHER;
    const day = byDay.get(r.day) ?? new Map<string, number>();
    day.set(model, (day.get(model) ?? 0) + tokens);
    byDay.set(r.day, day);
  }
  const days = [...byDay.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([day, parts]) => ({
    day,
    total: [...parts.values()].reduce((s, t) => s + t, 0),
    parts: series.filter((m) => parts.has(m)).map((m) => ({ model: m, tokens: parts.get(m) ?? 0 })),
  }));
  return { series, days };
}

// ---------- plain-language summary ----------

const KIND_PHRASE: Record<string, string> = {
  build: "builds", fix: "fixes", review: "reviews", re_review: "re-reviews", final_review: "final reviews",
  security_review: "security reviews", merge: "merges", benchmark: "benchmarks", check: "checks", draft: "drafts",
};

function joinAnd(items: string[]): string {
  if (items.length <= 1) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** e.g. "Codex: final and security reviews and hard builds only, 1 at a time, high effort." */
export function ruleSummary(key: string, rule: ModelRule): string {
  const kinds = rule.kinds ?? ["*"];
  const parts: string[] = [];
  if (kinds.includes("*")) {
    parts.push("any task");
  } else {
    const phrases = kinds.map((k) => (k === "build" && rule.tier === "scarce" ? "hard builds" : KIND_PHRASE[k] ?? k));
    const fi = phrases.indexOf("final reviews");
    const si = phrases.indexOf("security reviews");
    if (fi >= 0 && si >= 0) {
      phrases.splice(Math.max(fi, si), 1);
      phrases[Math.min(fi, si)] = "final and security reviews";
    }
    parts.push(`${joinAnd(phrases)} only`);
  }
  if (rule.max_concurrent) parts.push(`${rule.max_concurrent} at a time`);
  const efforts = Object.entries(rule.default_effort ?? {})
    .filter(([k]) => k !== "*" && (kinds.includes("*") || kinds.includes(k))).map(([, e]) => e);
  if (efforts.length) {
    const counts = new Map<string, number>();
    efforts.forEach((e) => counts.set(e, (counts.get(e) ?? 0) + 1));
    parts.push(`${[...counts.entries()].sort((a, b) => b[1] - a[1])[0][0]} effort`);
  }
  return `${modelLabel(key)}: ${parts.join(", ")}.`;
}

// ---------- rules form (round-trips: an unchanged form gives back the same body) ----------

export interface ModelForm {
  key: string;
  tier: ModelRule["tier"];
  kinds: string[];
  max_concurrent: string;
  soft: string;
  hard: string;
  effort: Record<string, string>;
  orig: ModelRule;
}

export interface RulesForm {
  models: ModelForm[];
  unlisted: BudgetRules["unlisted"];
  max_kb: string;
  hard_max_kb: string;
  max_line_chars: string;
  exclude_globs: string;
  on_limit: BudgetRules["limits"]["on_limit"];
  fallbacks: string;
  limit_scope: BudgetRules["limit_scope"];
  stages: string[];
  max_paid_rounds: string;
  max_codex_rounds: string;
  security_paths: string;
  skip_on: boolean;
  skip_max_lines: string;
  skip_paths: string;
  base: BudgetRules;
}

const s = (v: number | null | undefined) => (v == null ? "" : String(v));
const lines = (v: string) => v.split("\n").map((x) => x.trim()).filter(Boolean);

export function rulesToForm(rules: BudgetRules): RulesForm {
  const skip = rules.reviews.paid_skip_when;
  return {
    models: Object.entries(rules.models).map(([key, m]) => ({
      key, tier: m.tier, kinds: [...(m.kinds ?? ["*"])], max_concurrent: s(m.max_concurrent),
      soft: s(m.daily_tokens_soft), hard: s(m.daily_tokens_hard), effort: { ...(m.default_effort ?? {}) }, orig: m,
    })),
    unlisted: rules.unlisted,
    max_kb: s(rules.prompt.max_kb),
    hard_max_kb: s(rules.prompt.hard_max_kb),
    max_line_chars: s(rules.prompt.max_line_chars),
    exclude_globs: rules.prompt.exclude_globs.join("\n"),
    on_limit: rules.limits.on_limit,
    fallbacks: Object.entries(rules.limits.fallbacks ?? {}).map(([from, to]) => `${from}: ${to.join(", ")}`).join("\n"),
    limit_scope: rules.limit_scope,
    stages: [...rules.reviews.stages],
    max_paid_rounds: s(rules.reviews.max_paid_rounds),
    max_codex_rounds: s(rules.reviews.max_codex_rounds),
    security_paths: rules.reviews.security_paths.join("\n"),
    skip_on: skip != null,
    skip_max_lines: s(skip?.max_changed_lines ?? 40),
    skip_paths: (skip?.paths_only ?? ["docs/**", "tests/**"]).join("\n"),
    base: rules,
  };
}

/** A number field: blank means null when the rules had the field, otherwise it stays absent. */
function numberField(value: string, had: boolean): number | null | undefined {
  if (value.trim() === "") return had ? null : undefined;
  return Number(value);
}

export function formToRules(f: RulesForm): BudgetRules {
  const models: Record<string, ModelRule> = {};
  for (const m of f.models) {
    const rule: ModelRule = { ...m.orig, tier: m.tier, kinds: [...m.kinds] };
    const mc = numberField(m.max_concurrent, "max_concurrent" in m.orig);
    if (mc == null) delete rule.max_concurrent; else rule.max_concurrent = mc;
    for (const [field, value] of [["daily_tokens_soft", m.soft], ["daily_tokens_hard", m.hard]] as const) {
      const n = numberField(value, field in m.orig);
      if (n === undefined) delete rule[field]; else rule[field] = n;
    }
    const effort = Object.fromEntries(Object.entries(m.effort).filter(([, e]) => e));
    if (Object.keys(effort).length || "default_effort" in m.orig) rule.default_effort = effort;
    models[m.key] = rule;
  }
  const fallbacks: Record<string, string[]> = {};
  for (const line of lines(f.fallbacks)) {
    const at = line.indexOf(": ");
    if (at > 0) fallbacks[line.slice(0, at).trim()] = line.slice(at + 2).split(",").map((x) => x.trim()).filter(Boolean);
  }
  return {
    ...f.base,
    models,
    unlisted: f.unlisted,
    prompt: { ...f.base.prompt, max_kb: Number(f.max_kb), hard_max_kb: Number(f.hard_max_kb),
              max_line_chars: Number(f.max_line_chars), exclude_globs: lines(f.exclude_globs) },
    limits: { ...f.base.limits, on_limit: f.on_limit, fallbacks },
    limit_scope: f.limit_scope,
    reviews: {
      ...f.base.reviews,
      stages: [...f.stages],
      max_paid_rounds: Number(f.max_paid_rounds),
      max_codex_rounds: Number(f.max_codex_rounds),
      security_paths: lines(f.security_paths),
      paid_skip_when: f.skip_on ? { max_changed_lines: Number(f.skip_max_lines), paths_only: lines(f.skip_paths) } : null,
    },
  };
}

/** Leaf-level differences between two rule bodies, e.g. models.codex.max_concurrent: 1 → 2. */
export function rulesDiff(before: unknown, after: unknown, path = ""): { path: string; before: string; after: string }[] {
  const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
  if (isObj(before) && isObj(after)) {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    return keys.flatMap((k) => rulesDiff(before[k], after[k], path ? `${path}.${k}` : k));
  }
  const a = JSON.stringify(before ?? null);
  const b = JSON.stringify(after ?? null);
  return a === b ? [] : [{ path, before: before === undefined ? "(unset)" : a, after: after === undefined ? "(unset)" : b }];
}

/** The rules form lays its fields out in a grid that becomes one column on a phone (styles.css, max-width 860px). */
export const RULES_FORM_CLASS = "rules-form";
export function rulesFormClass(phone: boolean): string {
  return phone ? `${RULES_FORM_CLASS} one-col` : RULES_FORM_CLASS;
}
