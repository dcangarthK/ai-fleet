import { useEffect, useRef, useState } from "react";

/** True while `signal` keeps changing; false `timeout` ms after the last change.
 *  The first value is the baseline and does not count as an update. */
export function useLiveSignal(signal: unknown, timeout = 3000): boolean {
  const [live, setLive] = useState(false);
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    setLive(true);
    const id = setTimeout(() => setLive(false), timeout);
    return () => clearTimeout(id);
  }, [signal, timeout]);
  return live;
}
