import { describe, expect, it, vi } from "vitest";
import { countDuration, createAnnouncer, createThrottle, easeOut, interpolateCount, isArrival, planCount } from "./motion";

describe("interpolation", () => {
  it("starts at from, ends at to, eases out", () => {
    expect(interpolateCount(10, 20, 0)).toBe(10);
    expect(interpolateCount(10, 20, 1)).toBe(20);
    expect(interpolateCount(10, 20, 0.5)).toBeGreaterThan(15);
    expect(easeOut(2)).toBe(1);
    expect(easeOut(-1)).toBe(0);
  });
  it("works downward", () => {
    expect(interpolateCount(20, 10, 1)).toBe(10);
    expect(interpolateCount(20, 10, 0.5)).toBeLessThan(15);
  });
});

describe("duration", () => {
  it("is 0 for no change, base for small jumps", () => {
    expect(countDuration(5, 5)).toBe(0);
    expect(countDuration(10, 11)).toBe(600);
  });
  it("scales for big jumps but caps at 800ms", () => {
    expect(countDuration(1, 20)).toBeGreaterThan(600);
    expect(countDuration(0, 1_000_000)).toBe(800);
  });
  it("snaps (0) under reduced motion", () => {
    expect(planCount(0, 100, true)).toBe(0);
    expect(planCount(0, 100, false)).toBeGreaterThan(0);
  });
});

describe("throttle", () => {
  it("allows at most one call per 5s", () => {
    let t = 1000;
    const allow = createThrottle(5000, () => t);
    expect(allow()).toBe(true);
    t += 4999;
    expect(allow()).toBe(false);
    t += 1;
    expect(allow()).toBe(true);
    t += 100;
    expect(allow()).toBe(false);
  });
});

describe("arrivals", () => {
  it("flags only ids not present initially", () => {
    const initial = new Set(["a", "b"]);
    expect(isArrival(initial, "a")).toBe(false);
    expect(isArrival(initial, "c")).toBe(true);
  });
});

describe("createAnnouncer", () => {
  it("emits first at once, keeps only the latest inside the window, flushes after it", () => {
    vi.useFakeTimers();
    let t = 0;
    const out: string[] = [];
    const a = createAnnouncer((m) => out.push(m), 5000, () => t);
    a.announce("one");
    t = 1000; vi.setSystemTime(1000); a.announce("two");
    t = 2000; a.announce("three");
    expect(out).toEqual(["one"]);
    t = 5000; vi.advanceTimersByTime(4000);
    expect(out).toEqual(["one", "three"]);
    a.dispose();
    vi.useRealTimers();
  });
});
