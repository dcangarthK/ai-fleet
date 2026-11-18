import { useSyncExternalStore } from "react";
import { createAnnouncer } from "../../lib/motion";

let text = "";
const subs = new Set<() => void>();
const bus = createAnnouncer((m) => { text = m; subs.forEach((f) => f()); });

/** Queue a short polite announcement (one per 5s, latest kept). Data source: the caller's own arrivals. */
export const announce = (msg: string): void => bus.announce(msg);

export function useAnnouncement(): string {
  return useSyncExternalStore((f) => { subs.add(f); return () => { subs.delete(f); }; }, () => text, () => "");
}
