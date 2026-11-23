import { describe, expect, it } from "vitest";
import { decisionKinds, extraEventText, filterDecisions, isShadowMismatch, shadowMismatchCount, brief, type Decision } from "./decisions";

const d = (o: Partial<Decision>): Decision => ({ id: 1, task_id: "t", kind: "placement", inputs: null, candidates: null, rule: null, chosen: { node: "a" }, shadow_choice: null, ts: 1, ...o });

describe("decisions", () => {
  it("flags a shadow choice that differs", () => {
    expect(isShadowMismatch(d({ shadow_choice: { node: "b" } }))).toBe(true);
    expect(isShadowMismatch(d({ shadow_choice: { node: "a" } }))).toBe(false);
    expect(isShadowMismatch(d({}))).toBe(false);
  });
  it("counts and filters", () => {
    const rows = [d({ id: 1 }), d({ id: 2, kind: "limit.park", rule: "r1", shadow_choice: "x", chosen: "y" })];
    expect(shadowMismatchCount(rows)).toBe(1);
    expect(decisionKinds(rows)).toEqual(["limit.park", "placement"]);
    expect(filterDecisions(rows, { kind: "limit.park" }).map((r) => r.id)).toEqual([2]);
    expect(filterDecisions(rows, { rule: "r1" }).length).toBe(1);
    expect(filterDecisions(rows, { mismatchOnly: true }).length).toBe(1);
    expect(filterDecisions(rows, {}).length).toBe(2);
  });
  it("shortens values", () => {
    expect(brief(null)).toBe("—");
    expect(brief("x".repeat(100), 10)).toHaveLength(10);
  });
  it("labels the new events", () => {
    expect(extraEventText("task.auto_close_failed", { title: "T", stage: "cleanup" })).toContain("failed at cleanup");
    expect(extraEventText("task.auto_closed", { title: "T", cleaned: true })).toContain("worktree removed");
    expect(extraEventText("task.limit_wait", {})).toContain("usage limit");
    expect(extraEventText("task.other", {})).toBeNull();
  });
});
