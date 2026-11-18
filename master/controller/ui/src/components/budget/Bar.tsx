import { useEffect, useState } from "react";
import type { Tone } from "../ui";
import "../motion/pages-motion.css";

const COLOUR: Record<string, string> = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", info: "var(--info)", accent: "var(--accent)", quiet: "var(--ink-3)" };

/** Percent of a limit used. Colour is never the only signal: the label always carries the number.
 *  The fill grows by scaleX (ease-out) from 0 on load and from its previous value on update.
 *  `crossed` (soft budget just crossed, from the status snapshot) plays one settle, never a loop. */
export function Bar({ pct, tone = "ok", label, valueText, crossed = false }: { pct: number | null; tone?: Tone; label: string; valueText?: string; crossed?: boolean }) {
  const p = pct == null ? 0 : Math.min(100, Math.max(0, pct));
  const [shown, setShown] = useState(0);
  useEffect(() => { setShown(p); }, [p]);
  return (
    <div
      role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct == null ? undefined : Math.round(p)}
      aria-valuetext={valueText}
      style={{ height: 8, borderRadius: 4, background: "var(--sunken)", border: "1px solid var(--line)", overflow: "hidden", width: "100%" }}
    >
      <span
        className={crossed ? "bar-fill warn-once" : "bar-fill"}
        style={{ display: "block", height: "100%", width: "100%", transform: `scaleX(${shown / 100})`, background: COLOUR[tone] }}
      />
    </div>
  );
}

/** Tone for a used percentage: warn from 70, bad from 90. */
export function usedTone(pct: number): Tone {
  return pct >= 90 ? "bad" : pct >= 70 ? "warn" : "ok";
}
