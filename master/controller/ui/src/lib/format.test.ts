import { describe, expect, it } from "vitest";
import type { FleetNode, Task } from "./api";
import { errorText } from "./api";
import { ago, duration, inTab, nodeSignal, taskActions } from "./format";

const node = (over: Partial<FleetNode>): FleetNode => ({
  id: "n", name: "n", ip: "100.1.1.1", os: "macOS", engine: "ollama", port: 11434, status: "approved",
  models: [], healthy: true, runner_ok: true, runner: null, inventory: null, ceiling: null,
  effective_ceiling: null, last_seen: 0, fails: 0, ...over,
});

const task = (over: Partial<Task>): Task => ({
  id: "t", parent_id: null, title: "t", agent: "codex", model: null, prompt: "p", mode: "auto-edit",
  repo: null, gate: null, node_id: "master", state: "working", created_at: 0, updated_at: 0, started_at: null,
  ended_at: null, session_id: null, result: null, denials: [], gate_result: null, exit_code: null,
  branch: null, cwd: null, runs: 0, error: null, agent_seconds: 0, ...over,
});

describe("format", () => {
  it("ago", () => {
    expect(ago(null)).toBe("never");
    expect(ago(100, 130)).toBe("30s ago");
    expect(ago(0.5, 7200.5)).toBe("2h ago");
    expect(ago(200, 100)).toBe("0s ago");
  });

  it("duration", () => {
    expect(duration(0)).toBe("—");
    expect(duration(42)).toBe("42s");
    expect(duration(420)).toBe("7m");
    expect(duration(5400)).toBe("1.5h");
  });

  it("nodeSignal", () => {
    expect(nodeSignal(node({}))).toBe("up");
    expect(nodeSignal(node({ status: "pending" }))).toBe("pending");
    expect(nodeSignal(node({ healthy: false }))).toBe("degraded");
    expect(nodeSignal(node({ engine: null, healthy: false }))).toBe("up"); // runner-only node
    expect(nodeSignal(node({ healthy: false, runner_ok: false }))).toBe("down");
  });

  it("tabs and actions", () => {
    expect(inTab(task({ state: "blocked" }), "waiting")).toBe(true);
    expect(inTab(task({ state: "blocked" }), "active")).toBe(false);
    expect(inTab(task({ state: "done" }), "all")).toBe(true);
    const a = taskActions(task({ state: "review", branch: "fleet/t" }));
    expect(a).toMatchObject({ approve: false, cancel: false, complete: true, cleanup: true, message: true, diff: true });
    expect(taskActions(task({ state: "awaiting_approval" })).approve).toBe(true);
  });

  it("errorText", () => {
    expect(errorText("nope", 400)).toBe("nope");
    expect(errorText([{ msg: "field required" }, { msg: "too short" }], 422)).toBe("field required; too short");
    expect(errorText(undefined, 401)).toMatch(/login/);
  });
});

import { explainReason, progressFraction } from "./format";

describe("install helpers", () => {
  it("explains runner reasons", () => {
    expect(explainReason("engine_upgrade: requires a newer version of Ollama")).toMatch(/Update Ollama/);
    expect(explainReason("no_space: 3GB free")).toMatch(/disk/);
    expect(explainReason("wont_fit: needs 20GB")).toMatch(/memory/);
    expect(explainReason("something else")).toBe("something else");
    expect(explainReason(null)).toBe("");
  });
  it("progress", () => {
    expect(progressFraction(null)).toBeNull();
    expect(progressFraction({ completed: 1, total: 4 })).toBe(0.25);
    expect(progressFraction({ completed: 9, total: 4 })).toBe(1);
  });
});
