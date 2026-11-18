import { describe, expect, it } from "vitest";
import { layout, nodeHealth, nodeRadius, summary, type CInput } from "./constellation";

const input: CInput = {
  nodes: [{ id: "b", name: "B", health: "up", models: 2 }, { id: "a", name: "A", health: "down", models: 9 }],
  roles: [{ node_id: "a", role: "reviewer" }, { node_id: "a", role: "coder" }, { node_id: "a", role: "coder" }, { node_id: "b", role: "fast" }],
  agents: [{ id: "codex", label: "Codex" }, { id: "claude:fleet", label: "Claude" }],
  live: [{ task_id: "t1", agent: "codex", node_id: "b" }, { task_id: "t2", agent: "ghost", node_id: "b" }, { task_id: "t3", agent: "codex", node_id: "zz" }],
};

describe("constellation layout", () => {
  it("is deterministic and order-independent", () => {
    const rev: CInput = { ...input, nodes: [...input.nodes].reverse(), agents: [...input.agents].reverse() };
    expect(layout(input)).toEqual(layout(input));
    expect(layout(rev)).toEqual(layout(input));
  });
  it("positions nodes by sorted id inside the viewBox", () => {
    const l = layout(input);
    expect(l.nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(l.nodes[0].x).toBeLessThan(l.nodes[1].x);
    for (const p of [...l.nodes, ...l.agents]) {
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.x).toBeLessThanOrEqual(l.w);
      expect(p.y).toBeGreaterThanOrEqual(0);
      expect(p.y).toBeLessThanOrEqual(l.h);
    }
  });
  it("sizes nodes by models, capped", () => {
    expect(nodeRadius(0)).toBe(5);
    expect(nodeRadius(2)).toBeLessThan(nodeRadius(4));
    expect(nodeRadius(99)).toBe(nodeRadius(6));
  });
  it("builds unique role orbits per node", () => {
    const a = layout(input).nodes.find((n) => n.id === "a")!;
    expect(a.orbits.map((o) => o.role)).toEqual(["coder", "reviewer"]);
    expect(a.orbits[1].r).toBeGreaterThan(a.orbits[0].r);
  });
  it("links only live tasks with both ends present", () => {
    const l = layout(input);
    expect(l.links.map((x) => x.task_id)).toEqual(["t1"]);
    expect(l.links[0].path.startsWith("M")).toBe(true);
    expect(l.agents.find((a) => a.id === "codex")!.live).toBe(true);
    expect(l.anyLive).toBe(true);
  });
  it("is still when nothing runs", () => {
    const l = layout({ ...input, live: [] });
    expect(l.links).toEqual([]);
    expect(l.anyLive).toBe(false);
    expect(l.agents.every((a) => !a.live)).toBe(true);
  });
  it("handles an empty fleet", () => {
    const l = layout({ nodes: [], roles: [], agents: [], live: [] });
    expect(l.nodes).toEqual([]);
    expect(summary(l)).toBe("0 nodes, 0 agents, 0 live tasks.");
  });
  it("maps node health", () => {
    expect(nodeHealth({ status: "pending", healthy: true, runner_ok: true })).toBe("pending");
    expect(nodeHealth({ status: "drained", healthy: true, runner_ok: true })).toBe("drained");
    expect(nodeHealth({ status: "approved", healthy: false, runner_ok: true })).toBe("down");
    expect(nodeHealth({ status: "approved", healthy: true, runner_ok: false })).toBe("degraded");
    expect(nodeHealth({ status: "approved", healthy: true, runner_ok: true })).toBe("up");
  });
});
