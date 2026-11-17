import type { ReactNode } from "react";
import { Button } from "./Button";
import { cx } from "./util";

/** Placeholder rows while data loads. Announces itself once to assistive tech. */
export function Loading({ rows = 3, label = "Loading" }: { rows?: number; label?: string }) {
  return (
    <div role="status" aria-busy="true" className="stack" style={{ padding: "var(--s3) 0" }}>
      <span className="sr-only">{label}…</span>
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} className="skel" aria-hidden="true" style={{ height: 16, width: `${92 - ((i * 17) % 40)}%` }} />
      ))}
    </div>
  );
}

export function Empty({ title, children, action }: { title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="state-box">
      <b>{title}</b>
      {children && <p>{children}</p>}
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry, title = "Couldn't load this" }: { error?: unknown; onRetry?: () => void; title?: string }) {
  const msg = error instanceof Error ? error.message : error ? String(error) : undefined;
  return (
    <div className={cx("state-box", "error")} role="alert">
      <b>{title}</b>
      {msg && <p className="wrap-any">{msg}</p>}
      {onRetry && <Button onClick={onRetry}>Try again</Button>}
    </div>
  );
}
