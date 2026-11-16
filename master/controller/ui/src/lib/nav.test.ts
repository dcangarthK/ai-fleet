import { describe, expect, it } from "vitest";
import { breadcrumbs, GO_TARGETS, NAV_GROUPS, NAV_PAGES, pageForPath, resolveLegacy } from "./nav";

describe("nav", () => {
  it("matches the groups and pages in the spec", () => {
    expect(NAV_GROUPS.map((g) => [g.label, g.pages.map((p) => p.label)])).toEqual([
      ["Operate", ["Now", "Work", "Lineage", "Plan"]],
      ["Spend", ["Budget"]],
      ["Fleet", ["Nodes", "Models", "Roles"]],
      ["Configure", ["Policies", "Skills", "Evals", "Activity", "Settings"]],
    ]);
    expect(NAV_PAGES.map((p) => p.to)).toEqual([
      "/", "/work", "/lineage", "/plan", "/budget", "/nodes", "/models", "/roles", "/policies", "/skills", "/evals", "/activity", "/settings",
    ]);
  });

  it("has unique ids and go keys", () => {
    expect(new Set(NAV_PAGES.map((p) => p.id)).size).toBe(NAV_PAGES.length);
    const keys = NAV_PAGES.map((p) => p.go).filter(Boolean);
    expect(new Set(keys).size).toBe(keys.length);
    expect(GO_TARGETS).toMatchObject({ n: "/", w: "/work", l: "/lineage", b: "/budget" });
  });

  it("redirects legacy routes", () => {
    expect(resolveLegacy("/live")).toBe("/");
    expect(resolveLegacy("/overview")).toBe("/");
    expect(resolveLegacy("/pipelines")).toBe("/work");
    expect(resolveLegacy("/tasks")).toBe("/work");
    expect(resolveLegacy("/tasks/")).toBe("/work");
  });

  it("preserves ?task= when redirecting /tasks", () => {
    expect(resolveLegacy("/tasks", "?task=t-abc&tab=all")).toBe("/work?task=t-abc");
    expect(resolveLegacy("/tasks", "?tab=all")).toBe("/work");
    expect(resolveLegacy("/tasks", "?task=a b")).toBe("/work?task=a%20b");
  });

  it("does not redirect current routes", () => {
    for (const p of NAV_PAGES) expect(resolveLegacy(p.to)).toBeNull();
    expect(resolveLegacy("/work/abc")).toBeNull();
  });

  it("finds the page for a path", () => {
    expect(pageForPath("/")?.id).toBe("now");
    expect(pageForPath("/work/t-1")?.id).toBe("work");
    expect(pageForPath("/nodes/n1")?.id).toBe("nodes");
    expect(pageForPath("/workbench")).toBeUndefined();
    expect(pageForPath("/nope")).toBeUndefined();
  });

  it("builds breadcrumbs", () => {
    expect(breadcrumbs("/")).toEqual([{ label: "Operate" }, { label: "Now" }]);
    expect(breadcrumbs("/work/t-1")).toEqual([{ label: "Operate" }, { label: "Work", to: "/work" }, { label: "t-1" }]);
    expect(breadcrumbs("/zzz")).toEqual([{ label: "Not found" }]);
  });
});
