/** Pure motion helpers (docs/UI-MOTION-SCOPE-2026-11-18.md guardrails). No DOM. */

export const COUNT_BASE_MS = 600;
export const COUNT_MAX_MS = 800;
export const ANNOUNCE_GAP_MS = 5000;

export const easeOut = (t: number): number => {
  const c = Math.min(1, Math.max(0, t));
  return 1 - Math.pow(1 - c, 3);
};

/** Value at progress t (0..1) between from and to, eased out. */
export const interpolateCount = (from: number, to: number, t: number): number =>
  from + (to - from) * easeOut(t);

/** Duration grows with the size of the jump (relative to the starting value), never above `max`. */
export function countDuration(from: number, to: number, base = COUNT_BASE_MS, max = COUNT_MAX_MS): number {
  const delta = Math.abs(to - from);
  if (delta === 0) return 0;
  const rel = delta / Math.max(1, Math.abs(from));
  const scaled = rel > 1 ? base * (1 + Math.log10(rel) / 2) : base;
  return Math.min(max, Math.round(scaled));
}

/** Duration actually used: 0 (snap) under reduced motion. */
export const planCount = (from: number, to: number, reduced: boolean): number =>
  reduced ? 0 : countDuration(from, to);

/** Allows at most one call per `gapMs`; `now` is injectable for tests. */
export function createThrottle(gapMs = ANNOUNCE_GAP_MS, now: () => number = Date.now) {
  let last = -Infinity;
  return (): boolean => {
    const t = now();
    if (t - last < gapMs) return false;
    last = t;
    return true;
  };
}

/** True for ids that were not in the set seen on first render. */
export const isArrival = (initial: ReadonlySet<string>, id: string): boolean => !initial.has(id);
