/** Pure helpers for the Work and Work detail motion (docs/UI-MOTION-SCOPE-2026-11-18.md, M3). No DOM. */
import type { Finding, Severity } from "./work";

export const PROGRESS_MAX = 0.95;
export const DEFAULT_EXPECTED_S = 600;

/** Progress 0..0.95 from elapsed vs expected seconds. Never reaches 1: only a real state change completes work. */
export function progressFraction(elapsedS: number, expectedS: number): number {
  if (!Number.isFinite(elapsedS) || !Number.isFinite(expectedS) || elapsedS <= 0 || expectedS <= 0) return 0;
  return Math.min(PROGRESS_MAX, Math.max(0, elapsedS / expectedS));
}

/** Median of the positive finite durations (seconds); `fallback` when there are none. */
export function expectedDuration(durations: readonly number[], fallback = DEFAULT_EXPECTED_S): number {
  const d = durations.filter((x) => Number.isFinite(x) && x > 0).sort((a, b) => a - b);
  if (d.length === 0) return fallback;
  const m = d.length >> 1;
  return d.length % 2 ? d[m] : (d[m - 1] + d[m]) / 2;
}

export interface TokenSample { t: number; total: number }

/** Tokens per second between consecutive samples (t in seconds). Skips non-increasing time; a drop counts as 0.
 *  Keeps the last `max` points. */
export function tokenRateSeries(samples: readonly TokenSample[], max = 24): number[] {
  const out: number[] = [];
  for (let i = 1; i < samples.length; i++) {
    const dt = samples[i].t - samples[i - 1].t;
    if (!(dt > 0)) continue;
    out.push(Math.max(0, (samples[i].total - samples[i - 1].total) / dt));
  }
  return out.slice(-max);
}

/** Appends a sample when its time moves forward; caps history. */
export function pushSample(samples: readonly TokenSample[], next: TokenSample, cap = 40): TokenSample[] {
  const last = samples[samples.length - 1];
  if (last && next.t <= last.t) return samples.slice();
  return [...samples, next].slice(-cap);
}

export interface GateResult { name: string; ok: boolean }

/** Pulls per-test results out of a gate tail, in the order they appear. Understands unittest-verbose
 *  ("test_x (m.C) ... ok" / "FAIL" / "ERROR") and "PASS name" / "FAIL name" / "✓ name" / "✗ name". */
export function parseGateResults(tail: string | null | undefined): GateResult[] {
  const out: GateResult[] = [];
  for (const raw of (tail ?? "").split("\n")) {
    const line = raw.trim();
    let m = /^(.+?)\s+\.\.\.\s+(ok|FAIL|FAILED|ERROR)$/i.exec(line);
    if (m) { out.push({ name: m[1], ok: m[2].toLowerCase() === "ok" }); continue; }
    m = /^(PASS|PASSED|FAIL|FAILED|✓|✔|✗|✕)\s+(.+)$/.exec(line);
    if (m) out.push({ name: m[2], ok: /^(PASS|PASSED|✓|✔)$/.test(m[1]) });
  }
  return out;
}

/** Settle order: as parsed (one by one), each with a stagger delay in ms, capped so long lists finish quickly. */
export function settleOrder(results: readonly GateResult[], stepMs = 70, capMs = 700): (GateResult & { delay: number })[] {
  return results.map((r, i) => ({ ...r, delay: Math.min(capMs, i * stepMs) }));
}

const RANK: Record<Severity, number> = { HIGH: 0, MED: 1, LOW: 2 };

/** Findings HIGH, then MED, then LOW; stable inside a severity. */
export function severityOrder(findings: readonly Finding[]): Finding[] {
  return findings.map((f, i) => ({ f, i })).sort((a, b) => RANK[a.f.severity] - RANK[b.f.severity] || a.i - b.i).map((x) => x.f);
}

/** Stagger delay for the i-th arriving item. */
export const staggerDelay = (i: number, stepMs = 90, capMs = 720): number => Math.min(capMs, Math.max(0, i) * stepMs);
