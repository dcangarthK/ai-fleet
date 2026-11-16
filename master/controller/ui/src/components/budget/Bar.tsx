import type { Tone } from "../ui";

const COLOUR: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", info: "var(--info)", accent: "var(--accent)", quiet: "var(--ink-3)" };

/** Percent of a limit used. Colour is never the only signal: the label always carries the number. */
export function Bar({ pct, tone = "ok", label, valueText }: { pct: number | null; tone?: Tone; label: string; valueText?: string }) {
  const p = pct == null ? 0 : Math.min(100, Math.max(0, pct));
  return (
    <div
      role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct == null ? undefined : Math.round(p)}
      aria-valuetext={valueText}
      style={{ height: 8, borderRadius: 4, background: "var(--sunken)", border: "1px solid var(--line)", overflow: "hidden", width: "100%" }}
    >
      <span style={{ display: "block", height: "100%", width: `${p}%`, background: COLOUR[tone], minWidth: p > 0 ? 2 : 0 }} />
    </div>
  );
}

/** Tone for a used percentage: warn from 70, bad from 90. */
export function usedTone(pct: number): Tone {
  return pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok";
}
