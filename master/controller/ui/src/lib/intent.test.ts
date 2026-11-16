import { describe, expect, it } from "vitest";
import type { BudgetStatus } from "./budget";
import { budgetNote, parseIntent, type IntentCtx } from "./intent";

const ctx: IntentCtx = {
  tasks: [
    { id: "t-3f9a12c0", title: "Add Now page", agent: "claude", model: "claude-sonnet-5-5", repo: "/r", branch: "fleet/t-3f9a12c0", state: "review" },
    { id: "t-3f9b0000", title: "Other", agent: "codex", model: "gpt-5.6-terra", repo: "/r", branch: null, state: "review" },
    { id: "t-9c1e07b2", title: "Gate fix", agent: "codex", model: "gpt-5.6-terra", repo: "/r", branch: null, state: "failed" },
  ],
  nodes: [{ id: "n-1", name: "laptop" }, { id: "n-2", name: "master" }],
  budget: { fleet_only: false, rules_version: 1, models: [
    { key: "codex", agent: "codex", tier: "scarce", state: "soft", limited_until: null, limit_scope: null, running: 1, max_concurrent: 1, today_tokens: 0, soft: null, hard: null },
    { key: "claude:claude-sonnet-*", agent: "claude", tier: "standard", state: "ok", limited_until: null, limit_scope: null, running: 0, max_concurrent: 2, today_tokens: 0, soft: null, hard: null },
  ] } as BudgetStatus,
};
const body = (s: string) => parseIntent(s, ctx)?.request?.body as Record<string, unknown>;

describe("parseIntent", () => {
  it("parses a review with a model into the exact spawn body", () => {
    const i = parseIntent("review t-9c1e07b2 with fable", ctx)!;
    expect(i.request).toMatchObject({ method: "POST", path: "/api/tasks" });
    expect(i.request?.body).toMatchObject({ agent: "claude", model: "claude-fable-5-2", kind: "review", parent_id: "t-9c1e07b2", link: "review", review_of: "t-9c1e07b2", mode: "read-only" });
    expect(i.fields.find((f) => f.label === "Kind")?.value).toBe("review");
    expect(i.warnings).toEqual([]);
  });

  it("resolves a unique id prefix and picks a different reviewer by default", () => {
    expect(body("review t-3f9a")).toMatchObject({ parent_id: "t-3f9a12c0", model: "claude-fable-5-2" });
  });

  it("refuses an ambiguous prefix instead of guessing", () => {
    const i = parseIntent("review t-3f9", ctx)!;
    expect(i.request).toBeNull();
    expect(i.warnings[0]).toContain("ambiguous");
  });

  it("warns when the reviewer is the builder's model", () => {
    const i = parseIntent("review t-3f9a12c0 with sonnet", ctx)!;
    expect(i.request).not.toBeNull();
    expect(i.warnings[0]).toContain("independence");
  });

  it("codex reviews run on terra and final reviews at high effort", () => {
    expect(body("final review t-3f9a12c0 with codex")).toMatchObject({ agent: "codex", model: "gpt-5.6-terra", kind: "final_review", effort: "high" });
    expect(body("review t-3f9a12c0 with codex")).toMatchObject({ effort: "medium" });
  });

  it("parses merge, message, drain and task actions", () => {
    expect(body("merge t-3f9a12c0")).toMatchObject({ kind: "merge", link: "merge", model: "claude-sonnet-5-5" });
    expect(parseIntent("tell t-9c1e07b2: run nodes/build.py", ctx)!.request).toEqual({ method: "POST", path: "/api/tasks/t-9c1e07b2/message", body: { text: "run nodes/build.py" } });
    expect(parseIntent("drain lap", ctx)!.request).toEqual({ method: "POST", path: "/api/nodes/n-1/drain" });
    expect(parseIntent("cancel t-9c1e", ctx)!.request?.path).toBe("/api/tasks/t-9c1e07b2/cancel");
    expect(parseIntent("approve t-9c1e07b2", ctx)!.warnings[0]).toContain("admin");
  });

  it("parses spawn with kind and model, and rejects unknown kinds and models", () => {
    expect(body("spawn build with opus: add a retry button")).toMatchObject({ kind: "build", model: "claude-opus-5-5", prompt: "add a retry button" });
    expect(body("spawn: summarise the log")).toMatchObject({ kind: "draft", model: "claude-sonnet-5-5" });
    expect(parseIntent("spawn nonsense: x", ctx)!.request).toBeNull();
    expect(parseIntent("review t-9c1e07b2 with gpt9", ctx)!.warnings[0]).toContain("Unknown model");
  });

  it("returns null for anything else so the palette falls back to search", () => {
    for (const s of ["", "budget", "nodes", "review", "review nothing", "drain", "approve laptop"]) expect(parseIntent(s, ctx)).toBeNull();
  });

  it("reports an unknown task or node as understood but not runnable", () => {
    expect(parseIntent("review t-ffff", ctx)!.request).toBeNull();
    expect(parseIntent("drain mars", ctx)!.request).toBeNull();
  });
});

describe("budgetNote", () => {
  it("matches the rule that applies", () => {
    expect(budgetNote(ctx.budget, "codex", "gpt-5.6-terra")).toBe("codex (scarce): at soft budget, 1/1 running");
    expect(budgetNote(ctx.budget, "claude", "claude-sonnet-5-5")).toContain("claude:claude-sonnet-* (standard): OK");
    expect(budgetNote(ctx.budget, "claude", "claude-fable-5-2")).toContain("no rule listed");
    expect(budgetNote(null, "claude", null)).toContain("not loaded");
  });
});
