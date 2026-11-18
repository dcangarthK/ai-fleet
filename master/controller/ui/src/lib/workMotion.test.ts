import { describe, expect, it } from "vitest";
import { expectedDuration, parseGateResults, progressFraction, pushSample, settleOrder, severityOrder, staggerDelay, tokenRateSeries } from "./workMotion";
import type { Finding } from "./work";

describe("progressFraction", () => {
  it("scales and clamps to 0..0.95", () => {
    expect(progressFraction(30, 60)).toBe(0.5);
    expect(progressFraction(600, 60)).toBe(0.95);
    expect(progressFraction(-5, 60)).toBe(0);
    expect(progressFraction(10, 0)).toBe(0);
    expect(progressFraction(NaN, 60)).toBe(0);
  });
});

describe("expectedDuration", () => {
  it("is the median of usable durations", () => {
    expect(expectedDuration([10, 30, 20])).toBe(20);
    expect(expectedDuration([10, 20, 0, -3, NaN])).toBe(15);
  });
  it("falls back when empty", () => {
    expect(expectedDuration([], 99)).toBe(99);
    expect(expectedDuration([])).toBe(600);
  });
});

describe("tokenRateSeries", () => {
  it("computes tokens per second between samples", () => {
    expect(tokenRateSeries([{ t: 0, total: 0 }, { t: 2, total: 100 }, { t: 4, total: 100 }])).toEqual([50, 0]);
  });
  it("skips non-increasing time, floors drops at 0, caps length", () => {
    expect(tokenRateSeries([{ t: 1, total: 5 }, { t: 1, total: 9 }, { t: 2, total: 1 }])).toEqual([0]);
    const many = Array.from({ length: 50 }, (_, i) => ({ t: i, total: i * 10 }));
    expect(tokenRateSeries(many, 5)).toHaveLength(5);
  });
  it("is empty with fewer than two samples", () => {
    expect(tokenRateSeries([])).toEqual([]);
    expect(tokenRateSeries([{ t: 0, total: 1 }])).toEqual([]);
  });
});

describe("pushSample", () => {
  it("ignores stale times and caps history", () => {
    const a = pushSample([], { t: 1, total: 1 });
    expect(pushSample(a, { t: 1, total: 2 })).toHaveLength(1);
    let s = a;
    for (let i = 2; i < 10; i++) s = pushSample(s, { t: i, total: i }, 3);
    expect(s).toHaveLength(3);
  });
});

describe("gate results", () => {
  it("parses unittest verbose and PASS/FAIL lines in order", () => {
    const tail = "test_a (m.C) ... ok\ntest_b (m.C) ... FAIL\nPASS test_c\n✗ test_d\nRan 4 tests";
    expect(parseGateResults(tail)).toEqual([
      { name: "test_a (m.C)", ok: true }, { name: "test_b (m.C)", ok: false }, { name: "test_c", ok: true }, { name: "test_d", ok: false },
    ]);
    expect(parseGateResults(null)).toEqual([]);
  });
  it("settles one by one with a capped delay", () => {
    const r = settleOrder(Array.from({ length: 20 }, (_, i) => ({ name: `t${i}`, ok: true })));
    expect(r[0].delay).toBe(0);
    expect(r[1].delay).toBe(70);
    expect(r[19].delay).toBe(700);
    expect(r[3].name).toBe("t3");
  });
});

describe("severityOrder", () => {
  it("orders HIGH, MED, LOW and is stable", () => {
    const f = (severity: Finding["severity"], title: string): Finding => ({ severity, title, detail: "", cites: [] });
    expect(severityOrder([f("LOW", "a"), f("HIGH", "b"), f("MED", "c"), f("HIGH", "d")]).map((x) => x.title)).toEqual(["b", "d", "c", "a"]);
  });
  it("staggerDelay caps", () => {
    expect(staggerDelay(2)).toBe(180);
    expect(staggerDelay(100)).toBe(720);
  });
});
