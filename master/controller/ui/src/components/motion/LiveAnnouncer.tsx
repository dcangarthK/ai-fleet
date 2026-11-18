import { useEffect, useRef, useState, type CSSProperties } from "react";
import { createThrottle } from "../../lib/motion";

const HIDDEN: CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  overflow: "hidden",
  clip: "rect(0 0 0 0)",
  whiteSpace: "nowrap",
};

/** Polite live region. `message` is the data source; at most one announcement per 5s
 *  (messages inside the window are dropped). */
export function LiveAnnouncer({ message }: { message: string }) {
  const [text, setText] = useState("");
  const allow = useRef(createThrottle());
  useEffect(() => {
    if (message && allow.current()) setText(message);
  }, [message]);
  return (
    <div aria-live="polite" role="status" style={HIDDEN}>
      {text}
    </div>
  );
}
