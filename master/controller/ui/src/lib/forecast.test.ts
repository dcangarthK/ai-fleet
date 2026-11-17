import { describe, expect, it } from "vitest";
import { chartGeometry, countdown, dailyForecast, forecastSentence, forecastTone, windowForecast } from "./forecast";

const H = 3600;
// A 5h window that opened at t=0 and resets at t=5h.
const win = (used: number) => ({ used_percent: used, resets_at: 5 * H, window_minutes: 300 });

describe("windowForecast", () => {
  it("returns null without a usable window", () => {
    expect(windowForecast(null, 0)).toBeNull();
    expect(windowForecast({ used_percent: 5, resets_at: 100, window_minutes: null }, 0)).toBeNull();
  });
  it("projects a straight line from the window start", () => {
    const f = windowForecast(win(40), 2 * H)!; // 20 points/hour
    expect(f.burnPerHour).toBeCloseTo(20);
    expect(f.atReset).toBeCloseTo(100);
    expect(f.hitsAt).toBeCloseTo(5 * H); // 60 more points = 3h later = exactly reset
  });
  it("finds the time the limit is hit before reset", () => {
    const f = windowForecast(win(60), 2 * H)!; // 30/h, 40 left => 4/3h
    expect(f.hitsAt).toBeCloseTo(2 * H + (40 / 30) * H);
    expect(forecastTone(f)).toBe("bad");
  });
  it("has no hit when the burn is slow", () => {
    const f = windowForecast(win(10), 2 * H)!;
    expect(f.hitsAt).toBeNull();
    expect(f.atReset).toBeCloseTo(25);
    expect(forecastTone(f)).toBe("ok");
  });
  it("refuses to project too early in the window", () => {
    const f = windowForecast(win(5), 60)!;
    expect(f.burnPerHour).toBeNull();
    expect(f.atReset).toBeNull();
    expect(forecastTone(f)).toBe("quiet");
  });
  it("treats a full window as bad and a past reset as nothing to project", () => {
    expect(windowForecast(win(100), 2 * H)!.hitsAt).toBe(2 * H);
    expect(forecastTone(windowForecast(win(100), 2 * H))).toBe("bad");
    expect(windowForecast(win(50), 6 * H)!.atReset).toBeNull();
  });
  it("warns when it ends close to the limit", () => {
    expect(forecastTone(windowForecast(win(34), 2 * H))).toBe("warn"); // 17/h => 85 at reset
  });
});

describe("chartGeometry", () => {
  it("normalises into 0..1 and ends at the hit when there is one", () => {
    const now = 2 * H;
    const f = windowForecast(win(60), now)!;
    const g = chartGeometry(f, now);
    expect(g.now.x).toBeCloseTo(0.4);
    expect(g.now.y).toBeCloseTo(0.6);
    expect(g.hit!.y).toBe(1);
    expect(g.end).toEqual(g.hit);
  });
  it("ends at the reset edge otherwise", () => {
    const f = windowForecast(win(10), 2 * H)!;
    const g = chartGeometry(f, 2 * H);
    expect(g.hit).toBeNull();
    expect(g.end!.x).toBe(1);
    expect(g.end!.y).toBeCloseTo(0.25);
  });
});

describe("text", () => {
  it("counts down", () => {
    expect(countdown(0)).toBe("now");
    expect(countdown(48 * 60)).toBe("48m");
    expect(countdown(H + 60 * 8)).toBe("1h 08m");
    expect(countdown(72 * H)).toBe("3d 0h");
  });
  it("says what it cannot do", () => {
    expect(forecastSentence(null, 0, String)).toMatch(/nothing to project/);
    const early = windowForecast(win(5), 60);
    expect(forecastSentence(early, 60, String)).toMatch(/too early/);
  });
  it("names the hit time", () => {
    const f = windowForecast(win(60), 2 * H)!;
    expect(forecastSentence(f, 2 * H, (t) => `T${Math.round(t)}`)).toMatch(/reaches 100% at T/);
  });
});

describe("dailyForecast", () => {
  it("projects the daily budget by today's pace", () => {
    const r = dailyForecast(500, 1000, 6 * H, 0); // 500 per 6h => 1000 at 12h
    expect(r.hitsAt).toBeCloseTo(12 * H);
    expect(r.atDayEnd).toBeCloseTo(2000);
  });
  it("is empty with no budget or no usage", () => {
    expect(dailyForecast(10, null, 6 * H, 0)).toEqual({ hitsAt: null, atDayEnd: null });
    expect(dailyForecast(0, 1000, 6 * H, 0).hitsAt).toBeNull();
  });
});
