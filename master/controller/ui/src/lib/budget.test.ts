import { describe, expect, it } from "vitest";
import {
  formToRules, kindRows, meter, rateWindows, ruleSummary, rulesDiff, rulesFormClass, rulesToForm, stackDays, statusCard,
  type BudgetRules, type BudgetUsageRow, type ModelStatus,
} from "./budget";

// Same body the controller seeds (master/controller/budget.py DEFAULT_RULES).
const DEFAULT: BudgetRules = {
  models: {
    codex: { tier: "scarce", max_concurrent: 1, kinds: ["final_review", "security_review", "build"],
      default_effort: { final_review: "high", security_review: "high", build: "high", "*": "medium" },
      daily_tokens_soft: 2000000, daily_tokens_hard: null },
    "claude:claude-opus-*": { tier: "premium", max_concurrent: 1, kinds: ["build", "final_review", "security_review", "review"] },
    "claude:claude-fable-*": { tier: "premium", max_concurrent: 2, kinds: ["review", "re_review", "final_review", "security_review"] },
    "claude:claude-sonnet-*": { tier: "standard", max_concurrent: 2, kinds: ["*"] },
    "claude:claude-haiku-*": { tier: "cheap", max_concurrent: 2, kinds: ["*"] },
  },
  unlisted: "allow",
  prompt: { max_kb: 40, hard_max_kb: 120, max_line_chars: 1000,
    exclude_globs: ["master/controller/ui/dist/**", "**/*.min.js", "**/*.bundle.js", "**/package-lock.json", "evals/results/*.json"] },
  limits: { on_limit: "queue", fallbacks: { codex: ["claude:claude-fable-5-2"], "claude:claude-opus-*": ["claude:claude-sonnet-5-5"] } },
  effort_ladder: { build: ["medium", "medium", "high"], fix: ["low", "medium", "high"], re_review: ["low", "medium"] },
  limit_scope: "model",
  review_exec: { tools: ["Read", "Grep", "Glob"], max_turns: 8, task_minutes: 15 },
  reviews: { stages: ["local", "paid"], max_paid_rounds: 2, max_codex_rounds: 1, paid_skip_when: null,
    security_paths: ["master/controller/auth.py", "master/controller/policy.py", "nodes/runner.py", "master/mcp/**"] },
};

const status = (over: Partial<ModelStatus> = {}): ModelStatus => ({
  key: "codex", agent: "codex", tier: "scarce", state: "ok", limited_until: null, limit_scope: null, running: 0,
  max_concurrent: 1, today_tokens: 0, soft: 2000000, hard: null, ...over,
});

describe("rule summary", () => {
  it("says what each model is for in plain language", () => {
    expect(ruleSummary("codex", DEFAULT.models.codex))
      .toBe("Codex: final and security reviews and hard builds only, 1 at a time, high effort.");
    expect(ruleSummary("claude:claude-sonnet-*", DEFAULT.models["claude:claude-sonnet-*"]))
      .toBe("Claude Sonnet: any task, 2 at a time.");
    expect(ruleSummary("claude:claude-fable-*", DEFAULT.models["claude:claude-fable-*"]))
      .toBe("Claude Fable: reviews, re-reviews and final and security reviews only, 2 at a time.");
  });
});

describe("status cards and meters", () => {
  it("shows OK, limited until a time, account limits and the soft budget", () => {
    expect(statusCard(status())).toEqual({ tone: "ok", label: "OK" });
    const at = new Date(2026, 10, 11, 14, 30).getTime() / 1000;
    expect(statusCard(status({ state: "limited", limited_until: at, limit_scope: "model" })))
      .toEqual({ tone: "bad", label: "Limited until 2:30 PM" });
    expect(statusCard(status({ state: "limited", limited_until: at, limit_scope: "account" })).label)
      .toBe("Account limited until 2:30 PM");
    expect(statusCard(status({ state: "soft" }))).toEqual({ tone: "warn", label: "At soft budget" });
  });

  it("meters today's tokens against the soft budget", () => {
    expect(meter(500000, 2000000, null)).toEqual({ pct: 0.25, tone: "ok", label: "500k of 2.00M" });
    expect(meter(2500000, 2000000, null)).toEqual({ pct: 1, tone: "warn", label: "2.50M of 2.00M" });
    expect(meter(6000, 1000, 5000).tone).toBe("bad");
    expect(meter(1200, null, null)).toEqual({ pct: null, tone: "quiet", label: "1.2k today, no daily budget set" });
  });
});

describe("kind table", () => {
  it("formats tokens, prompt size and limit hits, busiest first", () => {
    const rows: BudgetUsageRow[] = [
      { kind: "review", agent: "claude", tasks: 4, input: 30000, output: 2000, cached: 0, cost_usd: 0.2, limit_hits: 1, local: false, avg_prompt_kb: 39.5 },
      { kind: "final_review", agent: "codex", tasks: 1, input: 120000, output: 9000, cached: 0, cost_usd: null, limit_hits: 0, local: false, avg_prompt_kb: null },
    ];
    expect(kindRows(rows)).toEqual([
      { kind: "final review", tasks: 1, tokens: "129k", avgPrompt: "—", limitHits: 0 },
      { kind: "review", tasks: 4, tokens: "32k", avgPrompt: "39.5 KB", limitHits: 1 },
    ]);
  });

  it("stacks days by model and folds small models into Other", () => {
    const row = (day: string, model: string, input: number): BudgetUsageRow =>
      ({ day, model, agent: null, tasks: 1, input, output: 0, cached: 0, cost_usd: null, limit_hits: 0, local: false, avg_prompt_kb: null });
    const out = stackDays([row("2026-11-10", "a", 10), row("2026-11-10", "b", 5), row("2026-11-11", "c", 3), row("2026-11-11", "d", 1)], 2);
    expect(out.series).toEqual(["a", "b", "Other"]);
    expect(out.days[1]).toEqual({ day: "2026-11-11", total: 4, parts: [{ model: "Other", tokens: 4 }] });
  });
});

describe("rules form", () => {
  it("round-trips: an unchanged form produces an identical body", () => {
    expect(formToRules(rulesToForm(DEFAULT))).toEqual(DEFAULT);
  });

  it("carries edits and turns the skip rule on", () => {
    const form = rulesToForm(DEFAULT);
    form.models[0].max_concurrent = "2";
    form.models[0].soft = "";
    form.skip_on = true;
    const body = formToRules(form);
    expect(body.models.codex.max_concurrent).toBe(2);
    expect(body.models.codex.daily_tokens_soft).toBeNull();
    expect(body.reviews.paid_skip_when).toEqual({ max_changed_lines: 40, paths_only: ["docs/**", "tests/**"] });
    expect(rulesDiff(DEFAULT, body).map((d) => d.path)).toEqual([
      "models.codex.daily_tokens_soft", "models.codex.max_concurrent", "reviews.paid_skip_when"]);
  });

  it("stacks to one column on a phone", () => {
    expect(rulesFormClass(true)).toBe("rules-form one-col");
    expect(rulesFormClass(false)).toBe("rules-form");
  });
});

describe("rateWindows", () => {
  it("gives both Codex windows, 5-hour then weekly", () => {
    const rows = rateWindows({
      primary: { used_percent: 3.2, resets_at: 1000, window_minutes: 300 },
      secondary: { used_percent: 56, resets_at: 2000, window_minutes: 10080 }, seen_at: 1,
    });
    expect(rows.map((r) => [r.label, r.used])).toEqual([["5-hour", "3% used"], ["Weekly", "56% used"]]);
  });
  it("is empty without a record and skips a missing window", () => {
    expect(rateWindows(undefined)).toEqual([]);
    expect(rateWindows({ primary: null, secondary: { used_percent: 1, resets_at: 5, window_minutes: null }, seen_at: null })).toHaveLength(1);
  });
});
