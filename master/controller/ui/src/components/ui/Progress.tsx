/** Thin progress bar with the progressbar role. `value` is 0..1, or null for an indeterminate bar. */
export function ProgressBar({ value, label }: { value: number | null; label?: string }) {
  return (
    <span className="bar" role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100}
          aria-valuenow={value == null ? undefined : Math.round(value * 100)}>
      <span style={{ width: value == null ? "30%" : `${Math.round(value * 100)}%` }} className={value == null ? "indeterminate" : ""} />
    </span>
  );
}
