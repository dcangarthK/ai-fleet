import "./work-motion.css";

/** Tiny tokens/sec sparkline. `values` is the series from lib/workMotion tokenRateSeries (real samples).
 *  Renders nothing with fewer than two points or no activity. */
export function Sparkline({ values, width = 56, height = 14, label = "Tokens per second" }: { values: readonly number[]; width?: number; height?: number; label?: string }) {
  if (values.length < 2) return null;
  const max = Math.max(...values);
  if (!(max > 0)) return null;
  const step = width / (values.length - 1);
  const pad = 1.5;
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(height - pad - (v / max) * (height - 2 * pad)).toFixed(1)}`).join(" ");
  const last = values[values.length - 1];
  return (
    <svg className="wm-spark" width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label}: ${Math.round(last)}`}>
      <polyline points={pts} fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}
