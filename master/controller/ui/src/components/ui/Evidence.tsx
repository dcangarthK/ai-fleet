import { Link } from "react-router-dom";
import { cx } from "./util";

export interface Evidence {
  label: string;
  /** In-app route (may carry a `#L120` terminal anchor). */
  to?: string;
  /** Anything else. Opens in the same tab. */
  href?: string;
}

/** Confidence as three segments plus a word; `value` is 0..1. */
export function Confidence({ value }: { value: number }) {
  const v = Math.max(0, Math.min(1, value));
  const level = v >= 0.75 ? "high" : v >= 0.45 ? "med" : "low";
  const on = level === "high" ? 3 : level === "med" ? 2 : 1;
  const word = level === "high" ? "High" : level === "med" ? "Medium" : "Low";
  return (
    <span className={cx("conf", level)} role="img" aria-label={`Confidence ${word.toLowerCase()}, ${Math.round(v * 100)} percent`} title={`${Math.round(v * 100)}%`}>
      <span className="track" aria-hidden="true">
        {[0, 1, 2].map((i) => <span key={i} className={cx("seg", i < on && "on")} />)}
      </span>
      {word} confidence
    </span>
  );
}

/** Links to the events, log lines and tasks a claim rests on. */
export function EvidenceLinks({ items }: { items: Evidence[] }) {
  if (!items.length) return null;
  return (
    <ul className="evidence" aria-label="Evidence">
      {items.map((e, i) => (
        <li key={`${e.label}-${i}`}>
          {e.to ? <Link to={e.to}>{e.label}</Link> : e.href ? <a href={e.href}>{e.label}</a> : <span className="quiet">{e.label}</span>}
        </li>
      ))}
    </ul>
  );
}
