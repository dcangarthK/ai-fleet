// Straight-line forecasts for the Budget page (docs/SPEC-ui-revamp.md 6a "Forecasts"). Pure functions; times are
// epoch seconds like the controller's `rate_limits` and task timestamps. Grounded in one snapshot per window: the
// burn rate is the average since the window opened, so it is an estimate and the page says so.

export interface WindowIn { used_percent: number; resets_at: number; window_minutes: number | null }

export interface Forecast {
  /** Hours since the window opened. */
  elapsedH: number;
  /** Percentage points per hour, averaged since the window opened. Null when too little time has passed to say. */
  burnPerHour: number | null;
  /** When the window reaches 100% at this burn, if that is before it resets. */
  hitsAt: number | null;
  /** Projected used % at reset (can pass 100; callers clamp for display). Null with no burn estimate. */
  atReset: number | null;
  windowStart: number;
  resetsAt: number;
  used: number;
  /** Number of seconds the window lasts in total. */
  windowS: number;
}

/** Below this much elapsed time the average is noise, so no projection is offered. */
export const MIN_ELAPSED_S = 10 * 60;

export function windowForecast(w: WindowIn | null | undefined, now: number): Forecast | null {
  if (!w || !w.window_minutes || w.window_minutes <= 0) return null;
  const windowS = w.window_minutes * 60;
  const windowStart = w.resets_at - windowS;
  const elapsedS = Math.min(windowS, Math.max(0, now - windowStart));
  const used = Math.max(0, w.used_percent);
  const base = { elapsedH: elapsedS / 3600, windowStart, resetsAt: w.resets_at, used, windowS };
  if (now >= w.resets_at) return { ...base, burnPerHour: null, hitsAt: null, atReset: null };
  if (elapsedS < MIN_ELAPSED_S) return { ...base, burnPerHour: null, hitsAt: used >= 100 ? now : null, atReset: null };
  const burn = used / (elapsedS / 3600);
  const remainingH = (w.resets_at - now) / 3600;
  const atReset = used + burn * remainingH;
  let hitsAt: number | null = null;
  if (used >= 100) hitsAt = now;
  else if (burn > 0) {
    const t = now + ((100 - used) / burn) * 3600;
    if (t <= w.resets_at) hitsAt = t;
  }
  return { ...base, burnPerHour: burn, hitsAt, atReset };
}

export type ForecastTone = "ok" | "warn" | "bad" | "quiet";

export function forecastTone(f: Forecast | null): ForecastTone {
  if (!f || f.atReset == null) return f && f.used >= 100 ? "bad" : "quiet";
  if (f.hitsAt != null) return "bad";
  return f.atReset >= 85 ? "warn" : "ok";
}

/** "1h 48m", "3d 2h", "now" for a number of seconds. */
export function countdown(seconds: number): string {
  if (seconds <= 0) return "now";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${Math.max(1, m)}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

/** One plain sentence about a forecast, for the card under the chart. */
export function forecastSentence(f: Forecast | null, now: number, when: (t: number) => string): string {
  if (!f) return "No rate-limit window recorded yet, so there is nothing to project.";
  if (f.used >= 100) return `Window is full (${Math.round(f.used)}% used); resets in ${countdown(f.resetsAt - now)}.`;
  if (f.burnPerHour == null) return `Window opened ${countdown(f.elapsedH * 3600)} ago; too early to project a burn rate.`;
  const rate = `${f.burnPerHour.toFixed(2)} points/hour`;
  if (f.hitsAt != null) return `At ${rate} this window reaches 100% at ${when(f.hitsAt)}, ${countdown(f.resetsAt - f.hitsAt)} before it resets.`;
  return `At ${rate} this window ends near ${Math.round(Math.min(f.atReset ?? f.used, 100))}% when it resets in ${countdown(f.resetsAt - now)}.`;
}

export interface Pt { x: number; y: number }

/** Chart geometry in 0..1 space (x: window start to reset, y: 0 to 100%). Callers scale to their SVG box. */
export function chartGeometry(f: Forecast, now: number): { start: Pt; now: Pt; end: Pt | null; hit: Pt | null } {
  const x = (t: number) => Math.min(1, Math.max(0, (t - f.windowStart) / f.windowS));
  const y = (p: number) => Math.min(1, Math.max(0, p / 100));
  const nowPt = { x: x(now), y: y(f.used) };
  const hit = f.hitsAt != null ? { x: x(f.hitsAt), y: 1 } : null;
  const end = f.atReset == null ? null : hit ?? { x: 1, y: y(f.atReset) };
  return { start: { x: 0, y: 0 }, now: nowPt, end, hit };
}

/** Today's tokens against a daily budget: when would the budget be used up at today's average pace. */
export function dailyForecast(used: number, limit: number | null | undefined, now: number, dayStart: number): { hitsAt: number | null; atDayEnd: number | null } {
  if (!limit || limit <= 0) return { hitsAt: null, atDayEnd: null };
  const elapsed = now - dayStart;
  if (elapsed < MIN_ELAPSED_S || used <= 0) return { hitsAt: used >= limit ? now : null, atDayEnd: null };
  const rate = used / elapsed;
  const dayEnd = dayStart + 86400;
  const hit = used >= limit ? now : now + (limit - used) / rate;
  return { hitsAt: hit <= dayEnd ? hit : null, atDayEnd: used + rate * (dayEnd - now) };
}
