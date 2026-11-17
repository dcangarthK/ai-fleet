import { describe, expect, it } from "vitest";
import {
  agentRows, buildNarrative, criticalPath, findAnomalies, findBottleneck, flagMap, layoutRiver, layoutTrace, marksFor,
  focusSet, mergeIntervals, niceTicks, packLane, snapshotAt, spanOf, stageOf, stageRank, STAGES, stepTimes, zoomView, panView,
  type LEvent, type LNode,
} from "./lineageGraph";

const T0 = 1_700_000_000;
let seq = 0;
function node(over: Partial<LNode> & { id: string }): LNode {
  seq += 1;
  return {
    root_id: "t-root", parent_id: null, link: null, title: "Greeting revamp", kind: "build", stage: "build-1", agent: "claude",
    model: "claude-sonnet", account: "claude/work", node: "master", lane: "master/claude/work", state: "done",
    created_at: T0 + seq, started_at: null, ended_at: null, failure_reason: null, gate_failed: false, gate: null,
    branch: null, budget_wait: null, resets_at: null, tokens: null, ...over,
  };
}

describe("stage ordering", () => {
  it("keeps the spec order and maps kinds and stage labels", () => {
    expect([...STAGES]).toEqual(["spawn", "build", "gate", "review", "fix", "re-review", "merge", "done"]);
    expect(stageOf({ kind: "build", stage: null, parent_id: null })).toBe("build");
    expect(stageOf({ kind: "re_review", stage: null, parent_id: "x" })).toBe("re-review");
    expect(stageOf({ kind: "final_review", stage: null, parent_id: "x" })).toBe("review");
    expect(stageOf({ kind: null, stage: "fix-2", parent_id: "x" })).toBe("fix");
    expect(stageOf({ kind: null, stage: null, parent_id: null })).toBe("build");
    expect(stageRank("build")).toBeLessThan(stageRank("review"));
    expect(stageRank("fix")).toBeLessThan(stageRank("merge"));
  });
});

describe("lane packing", () => {
  it("never overlaps two items on one row and reuses freed rows", () => {
    const items = [
      { id: "a", from: 0, to: 10 }, { id: "b", from: 5, to: 15 }, { id: "c", from: 10, to: 20 },
      { id: "d", from: 15, to: 30 }, { id: "e", from: 31, to: 32 },
    ];
    const { rows, count } = packLane(items);
    expect(count).toBe(2);
    for (const x of items) for (const y of items) {
      if (x.id < y.id && rows[x.id] === rows[y.id]) expect(x.to <= y.from || y.to <= x.from).toBe(true);
    }
    expect(rows.c).toBe(rows.a); // a ended exactly when c began
  });

  it("honours the minimum gap", () => {
    const { count } = packLane([{ id: "a", from: 0, to: 10 }, { id: "b", from: 10, to: 20 }], 5);
    expect(count).toBe(2);
  });

  it("lays lanes out by node with rows tall enough for their packing", () => {
    const now = T0 + 1000;
    const nodes = [
      node({ id: "a", started_at: T0 + 1, ended_at: T0 + 100 }),
      node({ id: "b", started_at: T0 + 50, ended_at: T0 + 150 }),
      node({ id: "c", lane: "laptop/codex/x", node: "laptop", account: "codex/x", started_at: T0, ended_at: T0 + 10 }),
    ];
    const lanes = [
      { id: "master/claude/work", node: "master", account: "claude/work", agent: "claude" },
      { id: "laptop/codex/x", node: "laptop", account: "codex/x", agent: "codex" },
    ];
    const layout = layoutRiver(nodes, lanes, now);
    expect(layout.groups.map((g) => g.node)).toEqual(["laptop", "master"]);
    const master = layout.groups[1].lanes[0];
    expect(master.rows).toBe(2);
    expect(layout.place.a.y).not.toBe(layout.place.b.y);
    expect(layout.height).toBeGreaterThan(master.y + master.h - 1);
  });
});

describe("critical path", () => {
  it("follows the parents of the attempt that finished last", () => {
    const now = T0 + 5000;
    const nodes = [
      node({ id: "root", started_at: T0, ended_at: T0 + 100, created_at: T0 }),
      node({ id: "fix", parent_id: "root", link: "fix", kind: "fix", started_at: T0 + 110, ended_at: T0 + 200, created_at: T0 + 101 }),
      node({ id: "side", parent_id: "root", link: "review", kind: "review", started_at: T0 + 105, ended_at: T0 + 150, created_at: T0 + 102 }),
      node({ id: "merge", parent_id: "fix", link: "merge", kind: "merge", started_at: T0 + 210, ended_at: T0 + 300, created_at: T0 + 201 }),
    ];
    expect(criticalPath(nodes, now)).toEqual(["root", "fix", "merge"]);
  });

  it("treats a running attempt as ending now", () => {
    const now = T0 + 500;
    const nodes = [
      node({ id: "a", started_at: T0, ended_at: T0 + 400, created_at: T0 }),
      node({ id: "b", parent_id: "a", started_at: T0 + 410, ended_at: null, state: "working", created_at: T0 + 401 }),
      node({ id: "c", started_at: T0, ended_at: T0 + 450, created_at: T0 }),
    ];
    expect(criticalPath(nodes, now)).toEqual(["a", "b"]);
    expect(criticalPath([], now)).toEqual([]);
  });

  it("survives a parent cycle", () => {
    const nodes = [node({ id: "a", parent_id: "b", ended_at: T0 + 9 }), node({ id: "b", parent_id: "a", ended_at: T0 + 5 })];
    expect(criticalPath(nodes, T0 + 20).length).toBe(2);
  });
});

describe("lineage focus", () => {
  it("keeps the focused attempt plus every upstream and downstream dependency", () => {
    const nodes = [
      node({ id: "root" }),
      node({ id: "build", parent_id: "root" }),
      node({ id: "review", parent_id: "build" }),
      node({ id: "fix", parent_id: "build" }),
      node({ id: "merge", parent_id: "fix" }),
      node({ id: "other" }),
    ];
    const edges = [{ from: "review", to: "fix", link: "retry", reason: null }];
    expect([...focusSet(nodes, edges, "fix")].sort()).toEqual(["root", "build", "review", "fix", "merge"].sort());
  });

  it("returns every node when no attempt is focused and handles cycles", () => {
    const nodes = [node({ id: "a", parent_id: "b" }), node({ id: "b", parent_id: "a" }), node({ id: "c" })];
    expect([...focusSet(nodes, [], null)].sort()).toEqual(["a", "b", "c"]);
    expect([...focusSet(nodes, [], "a")].sort()).toEqual(["a", "b"]);
  });
});

describe("trace layout", () => {
  it("puts every child right of its parent and gives siblings different rows", () => {
    const nodes = [
      node({ id: "root", created_at: T0 }),
      node({ id: "rev", parent_id: "root", kind: "review", created_at: T0 + 1 }),
      node({ id: "fix", parent_id: "root", kind: "fix", created_at: T0 + 2 }),
      node({ id: "mrg", parent_id: "fix", kind: "merge", created_at: T0 + 3 }),
    ];
    const layout = layoutTrace(nodes);
    const at = Object.fromEntries(layout.items.map((i) => [i.id, i]));
    expect(at.rev.col).toBeGreaterThan(at.root.col);
    expect(at.mrg.col).toBeGreaterThan(at.fix.col);
    expect(at.rev.row).not.toBe(at.fix.row);
    expect(layout.columns[0].stage).toBe("spawn");
    expect(layout.columns.find((c) => c.col === at.mrg.col)?.stage).toBe("merge");
    expect(layout.done?.from).toBe("mrg"); // everything is done: a done pip follows the last attempt
  });

  it("adds no done pip while something is still running", () => {
    const nodes = [node({ id: "root", state: "working", ended_at: null })];
    expect(layoutTrace(nodes).done).toBeNull();
  });
});

describe("bottleneck and anomalies", () => {
  it("names the longest queue wait with its reason", () => {
    const now = T0 + 2000;
    const nodes = [
      node({ id: "a", created_at: T0, started_at: T0 + 20, ended_at: T0 + 60 }),
      node({ id: "b", created_at: T0 + 100, started_at: T0 + 700, ended_at: T0 + 760, budget_wait: "Codex limited until 16:42" }),
    ];
    const b = findBottleneck(nodes, now)!;
    expect(b.kind).toBe("queue");
    expect(b.id).toBe("b");
    expect(b.why).toContain("Codex limited until 16:42");
    expect(b.evidence.map((e) => e.id)).toEqual(["b", "b"]);
  });

  it("falls back to the slowest run, and to nothing when all is quick", () => {
    const now = T0 + 2000;
    const slow = [node({ id: "a", created_at: T0, started_at: T0, ended_at: T0 + 10 }), node({ id: "b", created_at: T0 + 11, started_at: T0 + 11, ended_at: T0 + 900 })];
    expect(findBottleneck(slow, now)?.kind).toBe("run");
    expect(findBottleneck([node({ id: "q", created_at: T0, started_at: T0 + 2, ended_at: T0 + 5 })], now)).toBeNull();
  });

  it("flags retries above two, gate rounds above three, stalls and cost outliers", () => {
    const nodes = [
      node({ id: "r0" }),
      ...["r1", "r2", "r3"].map((id) => node({ id, parent_id: "r0", link: "retry", gate: { passed: false, tail: "" } })),
      node({ id: "g4", gate: { passed: true, tail: "" } }),
      ...[10, 12, 11, 400].map((k, i) => node({ id: `k${i}`, root_id: `t-k${i}`, kind: "review", tokens: { turns: 1, input: k * 1000, output: 0, cache_read: 0, cache_write: 0 } })),
    ];
    const events: LEvent[] = [{ ts: T0 + 5, task_id: "r2", type: "task.loop_cut", mark: "stall" }];
    const kinds = findAnomalies(nodes, events).map((a) => a.kind);
    expect(kinds).toContain("retries");
    expect(kinds).toContain("gate_rounds");
    expect(kinds).toContain("stall");
    const cost = findAnomalies(nodes, events).filter((a) => a.kind === "cost");
    expect(cost.map((a) => a.ids[0])).toEqual(["k3"]);
    expect(cost[0].evidence[0].id).toBe("k3");
    expect(flagMap(findAnomalies(nodes, events)).get("k3")).toEqual(["cost"]);
  });

  it("stays quiet at the thresholds", () => {
    const nodes = [node({ id: "a" }), ...["b", "c"].map((id) => node({ id, parent_id: "a", link: "retry" }))];
    expect(findAnomalies(nodes, []).filter((a) => a.kind === "retries")).toEqual([]);
  });
});

describe("narrative", () => {
  const nodes = [
    node({ id: "t-root", root_id: "t-root", created_at: T0, started_at: T0 + 5, ended_at: T0 + 125, state: "failed", gate_failed: true, failure_reason: "gate_failed", kind: "build", model: "sonnet" }),
    node({ id: "t-fix", root_id: "t-root", parent_id: "t-root", link: "fix", kind: "fix", model: "sonnet", created_at: T0 + 130, started_at: T0 + 400, ended_at: T0 + 520, budget_wait: "Codex limited until 16:42" }),
    node({ id: "t-mrg", root_id: "t-root", parent_id: "t-fix", link: "merge", kind: "merge", model: "haiku", created_at: T0 + 530, started_at: T0 + 532, ended_at: T0 + 560 }),
  ];

  it("tells the story deterministically and cites the attempts", () => {
    const a = buildNarrative(nodes, T0 + 1000, true);
    const b = buildNarrative(nodes, T0 + 1000, true);
    expect(a).toEqual(b);
    expect(a.text).toContain("t-root was queued at");
    expect(a.text).toContain("failed after 2m 00s because its gate failed");
    expect(a.text).toContain("sent back for a fix as t-fix");
    expect(a.text).toContain("sent to merge as t-mrg");
    expect(a.text).toContain("t-fix waited 4m 30s in the queue (budget wait: Codex limited until 16:42)");
    expect(a.text).toContain("critical path was 3 steps (t-root → t-fix → t-mrg)");
    expect(a.evidence.map((e) => e.id)).toContain("t-fix");
  });

  it("says when it is still running and handles an empty item", () => {
    const live = [node({ id: "t-x", root_id: "t-x", state: "working", created_at: T0, started_at: T0 + 1, ended_at: null })];
    expect(buildNarrative(live, T0 + 90, true).text).toContain("still running after 1m 30s");
    expect(buildNarrative([], T0).text).toMatch(/Nothing has happened/);
  });
});

describe("snapshots and events", () => {
  const nodes = [
    node({ id: "a", created_at: T0, started_at: T0 + 10, ended_at: T0 + 100, state: "done" }),
    node({ id: "b", parent_id: "a", created_at: T0 + 101, started_at: T0 + 120, ended_at: T0 + 200, state: "failed" }),
  ];
  const events: LEvent[] = [
    { ts: T0, task_id: "a", type: "created", to: "queued" },
    { ts: T0 + 10, task_id: "a", type: "started", to: "working" },
    { ts: T0 + 100, task_id: "a", type: "task.done", to: "done", from: "working" },
    { ts: T0 + 150, task_id: "b", type: "gate.bounced", mark: "gate" },
  ];

  it("hides the future and clips the unfinished", () => {
    expect(snapshotAt(nodes, events, T0 - 1)).toEqual([]);
    const mid = snapshotAt(nodes, events, T0 + 50);
    expect(mid.map((n) => n.id)).toEqual(["a"]);
    expect(mid[0].state).toBe("working");
    expect(mid[0].ended_at).toBeNull();
    const later = snapshotAt(nodes, events, T0 + 130);
    expect(later.find((n) => n.id === "a")?.state).toBe("done");
    expect(later.find((n) => n.id === "b")?.state).toBe("working");
    expect(later.find((n) => n.id === "b")?.ended_at).toBeNull();
  });

  it("lists marks per attempt and distinct step times in order", () => {
    expect(marksFor("b", events)).toEqual([{ ts: T0 + 150, kind: "gate" }]);
    expect(stepTimes([...events, { ts: T0, task_id: "z", type: "created" }])).toEqual([T0, T0 + 10, T0 + 100, T0 + 150]);
  });

  it("measures spans: queue wait, run and live", () => {
    const s = spanOf(nodes[0], T0 + 500);
    expect([s.wait, s.end - s.start!, s.live]).toEqual([10, 90, false]);
    const q = spanOf(node({ id: "q", created_at: T0, state: "queued" }), T0 + 40);
    expect([q.start, q.wait, q.live]).toEqual([null, 40, true]);
  });
});

describe("agent heartbeats", () => {
  it("merges busy time, finds idle gaps and spreads tokens over the run", () => {
    const now = T0 + 1000;
    const tk = { turns: 1, input: 600, output: 0, cache_read: 0, cache_write: 0 };
    const nodes = [
      node({ id: "a", started_at: T0, ended_at: T0 + 60, tokens: tk }),
      node({ id: "b", started_at: T0 + 30, ended_at: T0 + 120 }),
      node({ id: "c", started_at: T0 + 300, ended_at: null, state: "working" }),
      node({ id: "d", agent: "codex", model: "gpt", started_at: T0 + 10, ended_at: T0 + 20 }),
    ];
    const rows = agentRows(nodes, now, { t0: T0, t1: T0 + 600 }, 10);
    const sonnet = rows.find((r) => r.model === "claude-sonnet")!;
    expect(sonnet.busy).toEqual([{ from: T0, to: T0 + 120 }, { from: T0 + 300, to: T0 + 600 }]);
    expect(sonnet.gaps).toEqual([{ from: T0 + 120, to: T0 + 300 }]);
    expect(sonnet.runningNow).toBe(true);
    expect(sonnet.rate[0]).toBeCloseTo(600 / 1, 5); // 600 tokens inside the first 60 s bucket = 600 per minute
    expect(rows[0].label).toBe("claude-sonnet"); // running agents first
    expect(rows.find((r) => r.agent === "codex")?.runningNow).toBe(false);
    expect(mergeIntervals([{ from: 5, to: 6 }, { from: 0, to: 5 }])).toEqual([{ from: 0, to: 6 }]);
  });
});

describe("time view", () => {
  it("zooms about an anchor and clamps", () => {
    const v = zoomView({ t0: 0, t1: 1000 }, 0.5, 0.25);
    expect(v).toEqual({ t0: 125, t1: 625 });
    const tight = zoomView({ t0: 0, t1: 100 }, 0.0001);
    expect(tight.t1 - tight.t0).toBe(30);
    expect(panView({ t0: 0, t1: 10 }, 5)).toEqual({ t0: 5, t1: 15 });
  });

  it("makes ticks inside the window", () => {
    const ticks = niceTicks(T0, T0 + 3600, 8);
    expect(ticks.length).toBeGreaterThan(2);
    expect(ticks.length).toBeLessThanOrEqual(9);
    expect(ticks.every((t) => t.ts >= T0 && t.ts <= T0 + 3600)).toBe(true);
  });
});
