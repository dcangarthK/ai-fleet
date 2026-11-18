/** Pure helpers for the M4 page motion (docs/UI-MOTION-SCOPE-2026-11-18.md). No DOM, no React. */

/** Events per minute: `minutes` buckets, oldest first, newest last. `times` and `now` are epoch seconds.
 *  Events outside the window (older, or in the future) are ignored. */
export function eventsPerMinute(times: readonly number[], now: number, minutes = 15): number[] {
  const out = new Array<number>(Math.max(0, minutes)).fill(0);
  for (const t of times) {
    const age = now - t;
    if (!Number.isFinite(age) || age < 0) continue;
    const idx = Math.floor(age / 60);
    if (idx >= out.length) continue;
    out[out.length - 1 - idx] += 1;
  }
  return out;
}

/** True when usage went from below the soft budget to at or above it. Needs a real previous value. */
export function crossedSoft(prev: number | null | undefined, next: number, soft: number | null | undefined): boolean {
  if (prev == null || soft == null || soft <= 0) return false;
  return prev < soft && next >= soft;
}

/** Remembers which keys already crossed, so the colour change fires once per key and never loops.
 *  `observe` returns true only on the first update that crosses. The first value seen is a baseline. */
export function createCrossingLatch() {
  const last = new Map<string, number>();
  const fired = new Set<string>();
  return {
    observe(key: string, value: number, soft: number | null | undefined): boolean {
      const prev = last.get(key);
      last.set(key, value);
      if (fired.has(key)) return false;
      if (crossedSoft(prev, value, soft)) {
        fired.add(key);
        return true;
      }
      return false;
    },
  };
}

/** Highest score, or null when there are none. Non-finite values are skipped. */
export function bestScore(scores: readonly (number | null | undefined)[]): number | null {
  let best: number | null = null;
  for (const s of scores) if (typeof s === "number" && Number.isFinite(s) && (best == null || s > best)) best = s;
  return best;
}

/** True when `next` beats the best score seen before. No previous best means a baseline, not a record. */
export function isNewBest(prevBest: number | null | undefined, next: number | null | undefined): boolean {
  if (prevBest == null || next == null || !Number.isFinite(next)) return false;
  return next > prevBest;
}

/** SVG polyline points for a series in a w x h box (y flipped, padded 1px). Empty when under 2 points. */
export function seriesPoints(values: readonly number[], w: number, h: number): string {
  if (values.length < 2) return "";
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;
  const step = w / (values.length - 1);
  return values
    .map((v, i) => `${+(i * step).toFixed(2)},${+(h - 1 - ((v - min) / span) * (h - 2)).toFixed(2)}`)
    .join(" ");
}

/** Node connectivity transition: "offline" on the update where it stops being online, "online" on recovery. */
export function presenceChange(prevOnline: boolean | null | undefined, online: boolean): "offline" | "online" | null {
  if (prevOnline == null || prevOnline === online) return null;
  return online ? "online" : "offline";
}

/** The plan's current item: the first one that is neither done nor blocked, in stored order. Null when none. */
export function currentPlanItem(items: readonly { work_item: string; status: string }[]): string | null {
  const it = items.find((i) => i.status !== "done" && i.status !== "blocked");
  return it ? it.work_item : null;
}

/** Fraction (0..1) of items that are done. */
export const doneFraction = (items: readonly { status: string }[]): number =>
  items.length === 0 ? 0 : items.filter((i) => i.status === "done").length / items.length;

/** Group label for a feed row, used by the sticky time headers. `ts` and `now` are epoch seconds. */
export function timeGroup(ts: number, now: number): "Last hour" | "Earlier today" | "Earlier" {
  const age = now - ts;
  if (age < 3600) return "Last hour";
  const startOfDay = new Date(now * 1000);
  startOfDay.setHours(0, 0, 0, 0);
  return ts >= startOfDay.getTime() / 1000 ? "Earlier today" : "Earlier";
}

/** Ids in `prev` that are missing from `next` (rows that left a list). */
export function departed(prev: readonly string[], next: readonly string[]): string[] {
  const keep = new Set(next);
  return prev.filter((id) => !keep.has(id));
}

/** Whole minutes since epoch, used so reset countdowns re-render once a minute instead of every second. */
export const minuteTick = (nowMs: number): number => Math.floor(nowMs / 60000);
