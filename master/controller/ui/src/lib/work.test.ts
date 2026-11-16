import { describe, expect, it } from "vitest";
import { citesIn, deriveChain, filterWork, findWorkRoot, normStage, parseFindings, parseVerdict, splitCitations, splitDiff, type DetailAttempt } from "./work";
import type { WorkItem } from "./lineage";

const att = (o: Partial<DetailAttempt>): DetailAttempt => ({
  id: "t", parent_id: null, title: null, state: "done", kind: null, stage: null, agent: "claude", model: null, account: null, account_label: null,
  node: null, branch: null, created_at: 1, ended_at: 2, gate: null, exit_code: 0, verdict: null, resolution: null, result_excerpt: "", tokens: null, ...o,
});

const item = {
  root_id: "r", title: "Hello", outcome: null, display: { tone: "running", label: "Working" }, updated_at: 1,
  attempts: [{ id: "a1", title: "t", agent: "codex", model: null, link: null, state: "working", display: { tone: "running", label: "Working" } }],
} as unknown as WorkItem;
const none = { q: "", model: "", account: "", kind: "", view: "all" };

describe("work", () => {
  it("normalises stages", () => { expect(normStage("Final_Review")).toBe("final"); });
  it("derives chain with planned stages after actual", () => {
    const c = deriveChain({ attempts: [att({ id: "a", stage: "build" })], chain: [{ stage: "build", agent: "x" }, { stage: "merge", agent: "y" }] });
    expect(c.map((s) => [s.name, s.status])).toEqual([["build", "done"], ["merge", "plan"]]);
  });
  it("marks failed gate", () => {
    const c = deriveChain({ attempts: [att({ stage: "build", gate: { passed: false, tail: "" } })], chain: null });
    expect(c.find((s) => s.name === "gate")?.detail).toBe("failed, 1 of 3");
  });
  it("parses findings and cites", () => {
    const f = parseFindings("HIGH: bad thing [L12]\n  more detail\nLOW: nit");
    expect(f).toHaveLength(2);
    expect(f[0]).toMatchObject({ severity: "HIGH", cites: [12] });
  });
  it("parses verdict", () => { expect(parseVerdict("Verdict: approved")).toBe("approved"); expect(parseVerdict("none")).toBeNull(); });
  it("splits citations", () => {
    expect(splitCitations("see [L3].")).toEqual([{ kind: "text", text: "see " }, { kind: "cite", line: 3 }, { kind: "text", text: "." }]);
    expect(citesIn("[L2] [L2] [L1]")).toEqual([1, 2]);
  });
  it("splits diff", () => { expect(splitDiff("diff --git a/x b/x\n+a\n-b\n")[0]).toMatchObject({ path: "x", added: 1, removed: 1 }); });
  it("finds root by attempt id", () => {
    expect(findWorkRoot([item], "a1")).toBe("r");
    expect(findWorkRoot([item], "zz")).toBeNull();
  });
  it("filters", () => {
    expect(filterWork([item], { ...none, q: "hell" })).toHaveLength(1);
    expect(filterWork([item], { ...none, view: "codex" })).toHaveLength(1);
    expect(filterWork([item], { ...none, view: "merge" })).toHaveLength(0);
  });
});
