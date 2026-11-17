import { describe, expect, it } from "vitest";
import { fuzzyScore, getPaletteProviders, pagesProvider, rankItems, registerPaletteProvider } from "./palette";

describe("palette", () => {
  it("scores exact > prefix > substring > subsequence > none", () => {
    const exact = fuzzyScore("work", "Work");
    const prefix = fuzzyScore("wo", "Work");
    const sub = fuzzyScore("ork", "Work");
    const seq = fuzzyScore("wk", "Work");
    expect(exact).toBeGreaterThan(prefix);
    expect(prefix).toBeGreaterThan(sub);
    expect(sub).toBeGreaterThan(seq);
    expect(seq).toBeGreaterThan(0);
    expect(fuzzyScore("xyz", "Work")).toBe(0);
  });

  it("empty query matches everything", () => {
    expect(fuzzyScore("", "anything")).toBeGreaterThan(0);
  });

  it("ranks and filters items", () => {
    const items = [
      { id: "1", title: "Settings" },
      { id: "2", title: "Skills" },
      { id: "3", title: "Budget", keywords: "spend money" },
    ];
    expect(rankItems(items, "sk").map((i) => i.id)).toEqual(["2"]);
    expect(rankItems(items, "money").map((i) => i.id)).toEqual(["3"]);
    expect(rankItems(items, "zzz")).toEqual([]);
  });

  it("registers and unregisters providers", () => {
    const off = registerPaletteProvider({ id: "test", items: () => [] });
    expect(getPaletteProviders().some((p) => p.id === "test")).toBe(true);
    off();
    expect(getPaletteProviders().some((p) => p.id === "test")).toBe(false);
    expect(getPaletteProviders().some((p) => p.id === "pages")).toBe(true);
  });

  it("pages provider lists every page and filters by query", async () => {
    const all = await pagesProvider.items("");
    expect(all).toHaveLength(13);
    expect(all.find((i) => i.id === "page:work")?.to).toBe("/work");
    expect((await pagesProvider.items("bud")).map((i) => i.id)).toEqual(["page:budget"]);
  });
});
