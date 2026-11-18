import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("react", async () => (await import("./testHarness")).reactMock);

import { renderHook } from "./testHarness";
import { useCountUp } from "./useCountUp";
import { useLiveSignal } from "./useLiveSignal";
import { useVisible } from "./useVisible";
import { withViewTransition } from "./viewTransition";

const g = globalThis as Record<string, unknown>;

function setMotion(reduced: boolean) {
  g.window = {
    matchMedia: () => ({ matches: reduced, addEventListener() {}, removeEventListener() {} }),
  };
}

let frames: Array<(t: number) => void> = [];
const flush = (ts: number) => {
  const f = frames;
  frames = [];
  f.forEach((cb) => cb(ts));
};

beforeEach(() => {
  frames = [];
  g.requestAnimationFrame = (cb: (t: number) => void) => frames.push(cb);
  g.cancelAnimationFrame = () => {
    frames = [];
  };
});
afterEach(() => {
  delete g.window;
  delete g.document;
  delete g.requestAnimationFrame;
  delete g.cancelAnimationFrame;
  vi.useRealTimers();
});

describe("useCountUp", () => {
  it("starts at the value, not 0, and interpolates from the previous value", () => {
    setMotion(false);
    const h = renderHook((v: number) => useCountUp(v), 100);
    expect(h.result.current).toBe(100);
    h.rerender(110);
    flush(1000);
    flush(1300); // half of 600ms
    const mid = h.result.current;
    expect(mid).toBeGreaterThan(100);
    expect(mid).toBeLessThan(110);
    flush(1700);
    expect(h.result.current).toBe(110);
  });
  it("snaps under reduced motion", () => {
    setMotion(true);
    const h = renderHook((v: number) => useCountUp(v), 5);
    h.rerender(50);
    expect(h.result.current).toBe(50);
    expect(frames.length).toBe(0);
  });
});

describe("useLiveSignal", () => {
  it("is true while updates arrive and false after the timeout", () => {
    vi.useFakeTimers();
    const h = renderHook((s: number) => useLiveSignal(s, 1000), 1);
    expect(h.result.current).toBe(false);
    h.rerender(2);
    expect(h.result.current).toBe(true);
    vi.advanceTimersByTime(900);
    h.rerender(3);
    vi.advanceTimersByTime(900);
    expect(h.result.current).toBe(true);
    vi.advanceTimersByTime(200);
    expect(h.result.current).toBe(false);
  });
});

describe("useVisible", () => {
  it("follows document.visibilityState", () => {
    const listeners: Array<() => void> = [];
    const doc = {
      visibilityState: "visible",
      addEventListener: (_: string, cb: () => void) => listeners.push(cb),
      removeEventListener() {},
    };
    g.document = doc;
    const h = renderHook(() => useVisible(), undefined);
    expect(h.result.current).toBe(true);
    doc.visibilityState = "hidden";
    listeners.forEach((l) => l());
    expect(h.result.current).toBe(false);
  });
});

describe("withViewTransition", () => {
  it("falls back to a direct call when unsupported", () => {
    setMotion(false);
    g.document = {};
    const fn = vi.fn();
    withViewTransition(fn);
    expect(fn).toHaveBeenCalledTimes(1);
  });
  it("uses startViewTransition when supported", () => {
    setMotion(false);
    const start = vi.fn((cb: () => void) => cb());
    g.document = { startViewTransition: start };
    const fn = vi.fn();
    withViewTransition(fn);
    expect(start).toHaveBeenCalledTimes(1);
    expect(fn).toHaveBeenCalledTimes(1);
  });
  it("skips the transition under reduced motion", () => {
    setMotion(true);
    const start = vi.fn();
    g.document = { startViewTransition: start };
    const fn = vi.fn();
    withViewTransition(fn);
    expect(start).not.toHaveBeenCalled();
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
