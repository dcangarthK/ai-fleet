import { useEffect, useRef, useState } from "react";
import { interpolateCount, planCount } from "../../lib/motion";
import { useReducedMotion } from "../lineage/hooks";

/** Animates a number toward `value` (the data source) from the previously shown value, never from 0.
 *  Snaps under reduced motion. */
export function useCountUp(value: number): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(value);
  const current = useRef(value);
  useEffect(() => {
    const from = current.current;
    const ms = planCount(from, value, reduced);
    if (ms === 0 || typeof requestAnimationFrame === "undefined") {
      current.current = value;
      setShown(value);
      return;
    }
    let raf = 0;
    let start = 0;
    const step = (ts: number) => {
      if (!start) start = ts;
      const t = (ts - start) / ms;
      const v = t >= 1 ? value : interpolateCount(from, value, t);
      current.current = v;
      setShown(v);
      if (t < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, reduced]);
  return shown;
}
