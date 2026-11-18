type VtDoc = Document & { startViewTransition?: (cb: () => void) => unknown };

/** Runs `fn` inside a view transition when supported and motion is allowed; otherwise calls it directly. */
export function withViewTransition(fn: () => void): void {
  const doc = typeof document === "undefined" ? undefined : (document as VtDoc);
  const reduced =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!doc || typeof doc.startViewTransition !== "function" || reduced) {
    fn();
    return;
  }
  doc.startViewTransition(fn);
}
