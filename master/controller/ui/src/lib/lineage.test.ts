import { describe, expect, it } from "vitest";
import type { Task } from "./api";
import { capped, displayOf, isAttention, outcomeLabel, overviewSets, readyForReview, pillText, timelineSteps, timelineText, toneClass, type Attempt, type Display } from "./lineage";

const task = (over: Partial<Task>): Task => ({
  id: "t", parent_id: null, title: "t", agent: "codex", model: null, prompt: "p", mode: "auto-edit",
  repo: null, gate: null, node_id: "master", state: "working", created_at: 0, updated_at: 0, started_at: null,
  ended_at: null, session_id: null, result: null, denials: [], gate_result: null, exit_code: null,
  branch: null, cwd: null, runs: 0, error: null, agent_seconds: 0, ...over,
});

const attempt = (over: Partial<Attempt>): Attempt => ({
  id: "t", title: "t", agent: "claude", model: null, link: null, state: "review",
  display: { tone: "ok", label: "Ready for review" }, failure_reason: null, started_at: null, ended_at: null, ...over,
});

describe("status pill", () => {
  it("maps each display tone to a colour class", () => {
    expect(toneClass("running")).toBe("tone-accent");
    expect(toneClass("ok")).toBe("tone-ok");
    expect(toneClass("paused")).toBe("tone-warn");
    expect(toneClass("attention")).toBe("tone-bad");
    expect(toneClass("resolved")).toBe("tone-quiet");
    expect(toneClass("muted")).toBe("tone-quiet");
  });
  it("shows the label, with a tick for resolved work", () => {
    expect(pillText({ tone: "paused", label: "Paused: limit, resets 2:30 PM" })).toBe("Paused: limit, resets 2:30 PM");
    expect(pillText({ tone: "resolved", label: "Resolved by Retry" })).toBe("✓ Resolved by Retry");
    expect(pillText({ tone: "muted", label: "Dismissed" })).toBe("Dismissed");
  });
  it("falls back to the task state when the controller sends no display", () => {
    expect(displayOf(task({ state: "working" }))).toEqual({ tone: "running", label: "Working" });
    expect(displayOf(task({ state: "failed" }))).toEqual({ tone: "attention", label: "Failed" });
    expect(displayOf(task({ state: "review" }))).toEqual({ tone: "ok", label: "Ready for review" });
    const given: Display = { tone: "paused", label: "Paused: usage limit" };
    expect(displayOf(task({ state: "failed", display: given }))).toBe(given);
  });
});

describe("timeline", () => {
  it("tells the story of a work item, including a resumed run and a merge", () => {
    const attempts = [
      attempt({ agent: "claude", model: "Sonnet", state: "failed", display: { tone: "paused", label: "Paused: limit, resets 2:30 PM" }, run: 1 }),
      attempt({ agent: "claude", model: "Sonnet", state: "review", display: { tone: "ok", label: "Ready for review" }, run: 2 }),
      attempt({ id: "r", agent: "claude", model: "Fable 5.2", link: "review", display: { tone: "ok", label: "Ready for review" } }),
    ];
    expect(timelineSteps(attempts, { kind: "merged", commit: "ec2ab20", at: 1 })).toEqual([
      "Sonnet task · Paused: limit, resets 2:30 PM",
      "resumed · Ready for review",
      "Fable 5.2 review · Ready for review",
      "merged ec2ab20",
    ]);
    expect(timelineText(attempts, null)).toBe(
      "Sonnet task · Paused: limit, resets 2:30 PM → resumed · Ready for review → Fable 5.2 review · Ready for review");
  });
  it("uses the agent when there is no model and labels outcomes", () => {
    expect(timelineSteps([attempt({ agent: "codex", link: "retry" })], null)).toEqual(["codex retry · Ready for review"]);
    expect(outcomeLabel({ kind: "merged", commit: "abc1234", at: 1 })).toBe("Merged abc1234");
    expect(outcomeLabel({ kind: "done", at: 1 })).toBe("Done");
    expect(outcomeLabel({ kind: "abandoned", at: 1 })).toBe("Abandoned");
    expect(outcomeLabel(null)).toBeNull();
  });
});

describe("overview sets", () => {
  const mixed = [
    task({ id: "w", state: "working" }),
    task({ id: "q", state: "queued" }),
    task({ id: "s", state: "starting" }),
    task({ id: "limit", state: "failed", display: { tone: "paused", label: "Paused: limit, resets 2:30 PM" } }),
    task({ id: "bad", state: "failed", display: { tone: "attention", label: "Failed: gate" } }),
    task({ id: "blocked", state: "blocked", display: { tone: "attention", label: "Blocked: needs approval" } }),
    task({ id: "ask", state: "awaiting_approval", display: { tone: "attention", label: "Needs your approval" } }),
    task({ id: "old", state: "failed", display: { tone: "resolved", label: "Resolved by x" } }),
    task({ id: "dismissed", state: "failed", display: { tone: "muted", label: "Dismissed" } }),
    task({ id: "check", state: "working", hidden: true }),
    task({ id: "hiddenfail", state: "failed", hidden: true, display: { tone: "attention", label: "Failed" } }),
    task({ id: "legacy", state: "interrupted" }),
    task({ id: "done", state: "done" }),
  ];
  it("keeps working, needs-attention and paused apart", () => {
    const sets = overviewSets(mixed);
    expect(sets.working.map((t) => t.id)).toEqual(["w", "q", "s"]);
    expect(sets.attention.map((t) => t.id)).toEqual(["bad", "blocked", "ask", "legacy"]);
    expect(sets.paused.map((t) => t.id)).toEqual(["limit"]);
  });
  it("never lets a non-running state into working", () => {
    for (const t of overviewSets(mixed).working) expect(["queued", "starting", "working"]).toContain(t.state);
  });
});

describe("needs-attention cap", () => {
  const many = Array.from({ length: 8 }, (_, i) =>
    task({ id: `f${i}`, state: "failed", display: { tone: "attention", label: "Failed: error" } }));
  it("shows at most five and counts the rest", () => {
    const { shown, more } = capped(overviewSets(many).attention);
    expect(shown.map((t) => t.id)).toEqual(["f0", "f1", "f2", "f3", "f4"]);
    expect(more).toBe(3);
  });
  it("has nothing extra under the cap", () => {
    expect(capped([1, 2, 3])).toEqual({ shown: [1, 2, 3], more: 0 });
  });
  it("matches the filter the Tasks page uses", () => {
    expect(many.every(isAttention)).toBe(true);
    expect(isAttention(task({ state: "failed", display: { tone: "paused", label: "Paused: usage limit" } }))).toBe(false);
    expect(isAttention(task({ state: "failed", hidden: true, display: { tone: "attention", label: "Failed" } }))).toBe(false);
  });
  it("keeps an expired limit in attention, not paused", () => {
    const expired = task({ state: "failed", display: { tone: "attention", label: "Limit reset at 2:30 PM: resume or dismiss" } });
    const sets = overviewSets([expired]);
    expect(sets.attention).toHaveLength(1);
    expect(sets.paused).toHaveLength(0);
  });
});

describe("ready for review", () => {
  it("leaves out reviews that were dismissed or done elsewhere", () => {
    const list = [
      task({ id: "ok", state: "review", display: { tone: "ok", label: "Ready for review" } }),
      task({ id: "legacy", state: "review" }),
      task({ id: "dismissed", state: "review", display: { tone: "muted", label: "Dismissed" } }),
      task({ id: "elsewhere", state: "review", display: { tone: "resolved", label: "Done elsewhere by Sonnet (ec2ab20)" } }),
      task({ id: "hidden", state: "review", hidden: true }),
      task({ id: "failed", state: "failed", display: { tone: "attention", label: "Failed" } }),
    ];
    expect(readyForReview(list).map((t) => t.id)).toEqual(["ok", "legacy"]);
  });
});
