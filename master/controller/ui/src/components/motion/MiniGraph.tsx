import { seriesPoints } from "../../lib/pageMotion";
import "./pages-motion.css";

/** Settle for a saved/sent action: a check that draws in once. Mount it when the action resolved (that is its source). */
export function SettleCheck({ label = "Saved" }: { label?: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" role="img" aria-label={label} style={{ verticalAlign: "-2px", marginRight: 4 }}>
      <path className="check-draw" d="M2 7.5 L5.5 11 L12 3.5" fill="none" stroke="var(--ok)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Tiny SVG series graph. `values` is the data source; with fewer than 2 points it renders nothing.
 *  Static (no animation): a new sample just redraws the line. */
export function MiniGraph({ values, width = 96, height = 24, label }: { values: readonly number[] | undefined; width?: number; height?: number; label: string }) {
  if (!values || values.length < 2) return null;
  const pts = seriesPoints(values, width, height);
  return (
    <svg className="mini-graph" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label}>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
