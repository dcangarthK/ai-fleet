import { describe, expect, it } from "vitest";
import { bestScore, currentPlanItem, departed, doneFraction, timeGroup, createCrossingLatch, crossedSoft, eventsPerMinute, isNewBest, minuteTick, presenceChange, seriesPoints } from "./pageMotion";

describe("eventsPerMinute", () => {
  it("buckets oldest first and ignores out-of-window events", () => {
    expect(eventsPerMinute([1000, 990, 939, 880, 1001, 100], 1000, 3)).toEqual([1, 1, 2]);
  });
  it("is all zero when idle", () => {
    expect(eventsPerMinute([], 5, 4)).toEqual([0, 0, 0, 0]);
  });
});

describe("soft budget crossing", () => {
  it("detects an upward crossing only", () => {
    expect(crossedSoft(90, 100, 100)).toBe(true);
    expect(crossedSoft(100, 120, 100)).toBe(false);
    expect(crossedSoft(null, 120, 100)).toBe(false);
    expect(crossedSoft(10, 120, null)).toBe(false);
  });
  it("latch fires once per key", () => {
    const l = createCrossingLatch();
    expect(l.observe("a", 50, 100)).toBe(false);
    expect(l.observe("a", 100, 100)).toBe(true);
    expect(l.observe("a", 60, 100)).toBe(false);
    expect(l.observe("a", 110, 100)).toBe(false);
    expect(l.observe("b", 150, 100)).toBe(false); // baseline above soft: no animation on load
  });
});

describe("best score", () => {
  it("finds the best and skips junk", () => {
    expect(bestScore([0.2, null, 0.9, NaN, 0.5])).toBe(0.9);
    expect(bestScore([])).toBeNull();
  });
  it("flags only a strict improvement over a known best", () => {
    expect(isNewBest(0.8, 0.9)).toBe(true);
    expect(isNewBest(0.8, 0.8)).toBe(false);
    expect(isNewBest(null, 0.9)).toBe(false);
  });
});

describe("seriesPoints", () => {
  it("returns nothing for under 2 points", () => {
    expect(seriesPoints([], 10, 10)).toBe("");
    expect(seriesPoints([3], 10, 10)).toBe("");
  });
  it("spans the width", () => {
    const pts = seriesPoints([0, 5, 10], 100, 20).split(" ");
    expect(pts).toHaveLength(3);
    expect(pts[0].startsWith("0,")).toBe(true);
    expect(pts[2].startsWith("100,")).toBe(true);
  });
});

describe("plan helpers", () => {
  const items = [{ work_item: "a", status: "done" }, { work_item: "b", status: "blocked" }, { work_item: "c", status: "open" }];
  it("picks the first open, unblocked item", () => {
    expect(currentPlanItem(items)).toBe("c");
    expect(currentPlanItem([items[0]])).toBeNull();
  });
  it("computes the done fraction", () => {
    expect(doneFraction(items)).toBeCloseTo(1 / 3);
    expect(doneFraction([])).toBe(0);
  });
});

describe("timeGroup / departed", () => {
  const now = Math.floor(new Date(2026, 10, 18, 15, 0, 0).getTime() / 1000);
  it("groups by age", () => {
    expect(timeGroup(now - 600, now)).toBe("Last hour");
    expect(timeGroup(now - 3 * 3600, now)).toBe("Earlier today");
    expect(timeGroup(now - 30 * 3600, now)).toBe("Earlier");
  });
  it("finds rows that left", () => {
    expect(departed(["a", "b", "c"], ["b"])).toEqual(["a", "c"]);
  });
});

describe("presenceChange / minuteTick", () => {
  it("reports transitions", () => {
    expect(presenceChange(true, false)).toBe("offline");
    expect(presenceChange(false, true)).toBe("online");
    expect(presenceChange(true, true)).toBeNull();
    expect(presenceChange(null, false)).toBeNull();
  });
  it("ticks per minute", () => {
    expect(minuteTick(59_999)).toBe(0);
    expect(minuteTick(60_000)).toBe(1);
  });
});
