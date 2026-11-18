import type { ReactNode } from "react";
import { cx } from "./util";

/** Bordered surface with optional header (title + actions) and footer. */
export function Card({ title, actions, footer, className, bodyClassName, children, as: As = "section" }: {
  title?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  className?: string;
  bodyClassName?: string;
  children?: ReactNode;
  as?: "section" | "div" | "article";
}) {
  return (
    <As className={cx("card", className)}>
      {(title || actions) && (
        <header>
          {title && <h2>{title}</h2>}
          {actions && <div className="row-actions">{actions}</div>}
        </header>
      )}
      <div className={cx("body", bodyClassName)}>{children}</div>
      {footer && <footer>{footer}</footer>}
    </As>
  );
}

/** Same surface without padding, for tables and lists. */
export function Panel({ className, children }: { className?: string; children?: ReactNode }) {
  return <div className={cx("card panel-clip", className)}>{children}</div>;
}

/** Equal-height card grid that never overflows: auto-fill with a min(100%, 280px) track. */
export function CardGrid({ children, min = 280 }: { children?: ReactNode; min?: number }) {
  return <div className="cards" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, ${min}px), 1fr))` }}>{children}</div>;
}
