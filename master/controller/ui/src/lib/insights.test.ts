import { describe, expect, it } from "vitest";
import type { Task } from "./api";
import type { BudgetStatus, ModelStatus } from "./budget";
import { anomalies, briefing, dailyForecast, forecasts, gateFixRequest, nextActions, pickReviewer, queueForecast, windowForecast, workForecasts, type Inputs } from "./insights";
import type { Live } from "./live";

const NOW = new Date(2026, 10, 16, 12, 0, 0).getTime() / 1000; // local noon
const task = (o: Partial<Task>): Task => ({
  id: "t-1", parent_id: null, title: "Feature", agent: "claude", model: "claude-sonnet-5-5", prompt: "", mode: "auto-edit", repo: "/r",
  gate: null, node_id: "n1", state: "working", created_at: NOW - 100, updated_at: NOW - 10, started_at: null, ended_at: null, session_id: null,
  result: null, denials: [], gate_result: null, exit_code: null, branch: "fleet/t-1", cwd: null, runs: 1, error: null, agent_seconds: 0, ...o,
}) as Task;
const ok = { tone: "ok" as const, label: "Ready for review" };
const live = (o: Partial<Live> = {}): Live => ({ generated_at: NOW, orchestrator: [], workers: [], queue: [], chains: [], accounts: [], self_reported: [], machines: [], alerts: [], ...o });
const model = (o: Partial<ModelStatus>): ModelStatus => ({ key: "codex", agent: "codex", tier: "scarce", state: "ok", limited_until: null, limit_scope: null, running: 0, max_concurrent: 1, today_tokens: 0, soft: null, hard: null, ...o });
const budget = (...models: ModelStatus[]): BudgetStatus => ({ fleet_only: false, models, rules_version: 1 });
const inp = (o: Partial<Inputs> = {}): Inputs => ({ now: NOW, tasks: [], live: live(), budget: budget(), admin: true, ...o });

describe("nextActions", () => {
  it("proposes approval, with the button only for admins", () => {
    const t = task({ id: "t-a", state: "awaiting_approval" });
    expect(nextActions(inp({ tasks: [t] }))[0].action?.request.path).toBe("/api/tasks/t-a/approve");
    expect(nextActions(inp({ tasks: [t], admin: false }))[0].action).toBeUndefined();
  });

  it("proposes a review with a different model than the builder", () => {
    const t = task({ id: "t-b", state: "review", display: ok, gate_result: { state: "passed", exit_code: 0, output_tail: "" } });
    const [a] = nextActions(inp({ tasks: [t] }));
    expect(a.id).toBe("review:t-b");
    const body = a.action?.request.body as Record<string, unknown>;
    expect(body).toMatchObject({ kind: "review", parent_id: "t-b", link: "review", review_of: "t-b", model: "claude-fable-5-2" });
    expect(pickReviewer({ agent: "codex", model: "gpt-5.6-terra" }).model).toBe("claude-sonnet-5-5");
  });

  it("proposes a merge once a clean review exists, and not again after a merge task", () => {
    const b = task({ id: "t-b", state: "review", display: ok, gate_result: { state: "passed", exit_code: 0, output_tail: "" } });
    const r = task({ id: "t-r", parent_id: "t-b", link: "review", state: "done", model: "claude-fable-5-2", display: ok });
    const [a] = nextActions(inp({ tasks: [b, r] }));
    expect(a.id).toBe("merge:t-b");
    expect(a.evidence.map((e) => e.label)).toEqual(["t-b", "t-r"]);
    const m = task({ id: "t-m", parent_id: "t-b", link: "merge" });
    expect(nextActions(inp({ tasks: [b, r, m] })).find((x) => x.id === "merge:t-b")).toBeUndefined();
  });

  it("sends a gate failure back and spots the runner re-embed trap", () => {
    const t = task({ id: "t-g", state: "review", gate_result: { state: "failed", exit_code: 1, output_tail: "FAILED test_onboard" } });
    const { trap, request } = gateFixRequest(t);
    expect(trap).toBe(true);
    expect((request.body as { text: string }).text).toContain("nodes/build.py");
    const [a] = nextActions(inp({ tasks: [t] }));
    expect(a.kind).toBe("Gate failed");
    expect(a.confidence).toBeGreaterThan(0.8);
  });

  it("ranks approvals above reviews and ignores hidden tasks", () => {
    const rv = task({ id: "t-b", state: "review", display: ok });
    const ap = task({ id: "t-a", state: "awaiting_approval" });
    const hid = task({ id: "t-h", state: "awaiting_approval", hidden: true });
    expect(nextActions(inp({ tasks: [rv, ap, hid] })).map((x) => x.id)).toEqual(["approve:t-a", "review:t-b"]);
  });

  it("proposes draining an offline node that has queued work", () => {
    const l = live({
      machines: [{ node: "n9", name: "laptop", offline: "runner unreachable" } as Live["machines"][number]],
      queue: [{ task_id: "t-q", node: "n9", wait: "node" } as Live["queue"][number]],
    });
    const [a] = nextActions(inp({ live: l }));
    expect(a.action?.request.path).toBe("/api/nodes/n9/drain");
    expect(a.action?.danger).toBe(true);
  });
});

describe("anomalies", () => {
  it("flags a stalled task only after 30 minutes", () => {
    expect(anomalies(inp({ tasks: [task({ updated_at: NOW - 600 })] }))).toEqual([]);
    expect(anomalies(inp({ tasks: [task({ updated_at: NOW - 3600 })] }))[0].id).toBe("stall:t-1");
  });

  it("flags repeated gate failures on the same last line", () => {
    const g = (id: string) => task({ id, root_id: "t-root", state: "failed", gate_result: { state: "failed", exit_code: 1, output_tail: "a\nFAILED x" } });
    const out = anomalies(inp({ tasks: [g("t-1"), g("t-2")] }));
    expect(out[0].id).toMatch(/^gate-repeat:/);
    expect(out[0].severity).toBe("bad");
    expect(anomalies(inp({ tasks: [g("t-1")] }))).toEqual([]);
  });

  it("flags a flapping node and same-model review", () => {
    const alert = (id: number) => ({ id, ts: NOW - id * 60, task_id: null, type: "node.runner_offline", title: null, node: "n2", event_state: null, state: "open" as const, resolved_by: null });
    const out = anomalies(inp({ live: live({ alerts: [alert(1), alert(2), alert(3)] }) }));
    expect(out.map((x) => x.id)).toContain("flap:n2");
    const b = task({ id: "t-b", state: "review" });
    const r = task({ id: "t-r", parent_id: "t-b", link: "review", state: "done" });
    expect(anomalies(inp({ tasks: [b, r] })).map((x) => x.id)).toContain("same-model:t-r");
  });

  it("flags token alerts", () => {
    const row = { task_id: "t-9", turns: 1, peak_context: 1, turn1_context: 1, input: 1, output: 1, cache_read: 0, cache_write: 0, alerts: ["turn1_context"], caps: {}, usage: {} };
    expect(anomalies(inp({ tokenRows: [row as never] }))[0].id).toBe("tokens:t-9");
  });
});

describe("forecast maths", () => {
  it("projects time to the soft budget at the day's burn", () => {
    // noon: 12h elapsed, 600k used -> 50k/h; 400k left -> 8h; day ends in 12h, so the limit is hit in 8h
    const f = dailyForecast(model({ today_tokens: 600_000, soft: 1_000_000 }), NOW)!;
    expect(f.etaS).toBeCloseTo(8 * 3600, -1);
    expect(f.headline).toBe("Soft budget in 8.0h");
  });

  it("says on track when the day ends first, and not-enough-data early", () => {
    expect(dailyForecast(model({ today_tokens: 100_000, soft: 1_000_000 }), NOW)!.etaS).toBeNull();
    const early = new Date(2026, 10, 16, 0, 10, 0).getTime() / 1000;
    expect(dailyForecast(model({ today_tokens: 5, soft: 10 }), early)!.confidence).toBe(0.2);
  });

  it("reports at-budget as certain", () => {
    const f = dailyForecast(model({ today_tokens: 10, soft: 10 }), NOW)!;
    expect([f.etaS, f.confidence, f.tone]).toEqual([0, 1, "bad"]);
  });

  it("projects a rate-limit window and ignores one that resets first", () => {
    const m = (used: number, left: number) => model({ accounts: [{ account: "a", node: "n", label: null, state: "ok", limited_until: null, limit_scope: null, running: 0, rate_limits: { primary: null, seen_at: NOW, secondary: { used_percent: used, resets_at: NOW + left, window_minutes: 10080 } } }] });
    // weekly window 7d; 4d elapsed (resets in 3d), 80% used -> 20%/day => 1d left < 3d: limit hit in 1d
    const hit = windowForecast(m(80, 3 * 86400), NOW)!;
    expect(hit.etaS).toBeCloseTo(86400, -2);
    // 10% used after 4d -> would need 36d: resets first
    expect(windowForecast(m(10, 3 * 86400), NOW)!.etaS).toBeNull();
    expect(windowForecast(model({}), NOW)).toBeNull();
  });

  it("summarises the queue and the work items", () => {
    const l = live({
      queue: [{ task_id: "a", wait: "limit", until: NOW + 3600, queued_s: 120 } as Live["queue"][number], { task_id: "b", wait: "approval", until: null, queued_s: 30 } as Live["queue"][number]],
      chains: [{ root_id: "t-r", title: "Feature X", stages: [{ stage: "build", state: "done" }, { stage: "review", state: "current" }, { stage: "merge", state: "next" }] }],
    });
    const q = queueForecast(l, NOW)!;
    expect(q.headline).toBe("2 queued, oldest 2m");
    expect(q.etaS).toBe(3600);
    const spend = { by_model: {}, by_kind: { review: { tasks: 2, turns: 0, input: 80_000, cache_read: 0, cache_write: 0, output: 20_000 } } };
    const w = workForecasts(l, spend)[0];
    expect(w.headline).toBe("2 stages left: review, merge");
    expect(w.detail).toContain("50k tokens");
    expect(forecasts(inp({ live: l })).map((f) => f.id)).toEqual(["queue", "work:t-r"]);
  });
});

describe("briefing", () => {
  it("reads plainly and stays within five sentences", () => {
    const b = briefing(inp({
      tasks: [task({ state: "awaiting_approval" })],
      budget: budget(model({ state: "limited", limited_until: NOW + 3600 })),
      live: live({ machines: [{ node: "n9", name: "laptop", offline: "x", age_s: 10800 } as Live["machines"][number]], queue: [{ node: "n9", wait: "node" } as Live["queue"][number]] }),
    }));
    expect(b.sentences[0]).toBe("1 item need you.".replace("need", "needs"));
    expect(b.sentences.some((s) => s.startsWith("Codex is limited until"))).toBe(true);
    expect(b.sentences.some((s) => s.includes("laptop is offline") && s.includes("1 queued task waiting"))).toBe(true);
    expect(b.sentences.length).toBeLessThanOrEqual(5);
  });

  it("says nothing needs you, with lower confidence when data is missing", () => {
    expect(briefing(inp()).sentences[0]).toBe("Nothing needs you.");
    expect(briefing(inp({ live: null })).confidence).toBeLessThan(briefing(inp()).confidence);
  });
});
