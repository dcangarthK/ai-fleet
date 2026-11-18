import { useRef, type ReactNode } from "react";
import { isArrival } from "../../lib/motion";

/** Remembers the ids present on first render; returns a test for "arrived later". `ids` is the data source. */
export function useArrivals(ids: readonly string[]): (id: string) => boolean {
  const initial = useRef<ReadonlySet<string> | null>(null);
  if (initial.current === null) initial.current = new Set(ids);
  const set = initial.current;
  return (id) => isArrival(set, id);
}

/** Wrapper for one item: adds `.arrive` only when `isNew(id)` (id not seen on first render).
 *  Render it with `key={id}` so each item keeps its own animation. */
export function Arrive({
  id,
  isNew,
  className = "",
  children,
}: {
  id: string;
  isNew: (id: string) => boolean;
  className?: string;
  children: ReactNode;
}) {
  const cls = isNew(id) ? `${className} arrive`.trim() : className;
  return (
    <div className={cls || undefined} data-arrive-id={id}>
      {children}
    </div>
  );
}
