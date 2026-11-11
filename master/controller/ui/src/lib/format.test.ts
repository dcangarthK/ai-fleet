import { describe, expect, it } from "vitest";
import type { FleetNode, Task } from "./api";
import { errorText } from "./api";
import { ago, canonicalRef, duration, inTab, isTrustedModel, nodeSignal, trustReason, pctText, taskActions } from "./format";

describe("grounding formatting", () => {
  it("formats rates", () => {
    expect(pctText(null)).toBe("—");
    expect(pctText(0)).toBe("0%");
    expect(pctText(0.034)).toBe("3.4%");
    expect(pctText(0.5)).toBe("50%");
  });
  it("canonicalises references", () => {
    expect(canonicalRef("qwen3")).toBe("qwen3:latest");
    expect(canonicalRef("qwen3:8b")).toBe("qwen3:8b");
    expect(canonicalRef("host:5000/lib/m")).toBe("host:5000/lib/m:latest");
  });
  it("finds trusted models", () => {
    const cat = [{ reference: "glm:latest", trusted_reviewer: true }, { reference: "x:1b", trusted_reviewer: false }];
    expect(isTrustedModel(cat, "glm")).toBe(true);
    expect(isTrustedModel(cat, "x:1b")).toBe(false);
    expect(isTrustedModel(cat, "none")).toBe(false);
  });
  it("finds trusted derived models, not their base", () => {
    const cat = [{ reference: "qwen3:8b", trusted_reviewer: false, derived_model: "fleet-abc", derived_trusted_reviewer: true },
                 { reference: "y:1b", trusted_reviewer: false, derived_model: "fleet-y", derived_trusted_reviewer: false }];
    expect(isTrustedModel(cat, "fleet-abc")).toBe(true);
    expect(isTrustedModel(cat, "fleet-abc:latest")).toBe(true);
    expect(isTrustedModel(cat, "qwen3:8b")).toBe(false);
    expect(isTrustedModel(cat, "fleet-y")).toBe(false);
  });
  it("explains pipeline trust", () => {
    expect(trustReason({ kind: "agent", agent: "claude" }, { status: "valid", trusted: true })).toMatch(/agent/);
    expect(trustReason({ kind: "role", role: "reviewer" }, { status: "abstained", verdict: "abstain", trusted: true, trusted_by: "benchmark" })).toMatch(/benchmark/);
    expect(trustReason({ kind: "role", role: "reviewer" }, { status: "valid", trusted: true })).toMatch(/named/);
    expect(trustReason({ kind: "role", role: "reviewer" }, { status: "valid", fallback: true })).toMatch(/fallback/);
    expect(trustReason({ kind: "role", role: "reviewer" }, { status: "invalid" })).toMatch(/no usable/);
  });
});

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
