import type { CSSProperties } from "react";
import { useAnnouncement } from "./announce";

const HIDDEN: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
};

/** Polite live region, mount once (Shell). Text comes from announce(); throttled to 1 per 5s, latest kept. */
export function LiveAnnouncer() {
  const text = useAnnouncement();
  return (
    <div aria-live="polite" role="status" style={HIDDEN}>
      {text}
    </div>
  );
}
