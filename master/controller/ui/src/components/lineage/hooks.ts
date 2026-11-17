import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { MIN_SPAN, MAX_SPAN, panView, zoomView, type TimeView } from "../../lib/lineageGraph";

/** Seconds since the epoch, refreshed every `ms` while `enabled`. */
export function useNow(enabled: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    setNow(Date.now() / 1000);
    if (!enabled) return;
    const id = setInterval(() => setNow(Date.now() / 1000), ms);
    return () => clearInterval(id);
  }, [enabled, ms]);
  return now;
}

export function useMedia(query: string): boolean {
  const get = () => (typeof window !== "undefined" && window.matchMedia ? window.matchMedia(query).matches : false);
  const [match, setMatch] = useState(get);
  useEffect(() => {
    if (!window.matchMedia) return;
    const mq = window.matchMedia(query);
    const on = () => setMatch(mq.matches);
    on();
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, [query]);
  return match;
}

export const useReducedMotion = (): boolean => useMedia("(prefers-reduced-motion: reduce)");

/** Content width of an element, kept current with a ResizeObserver. */
export function useWidth(ref: RefObject<HTMLElement>): number {
  const [w, setW] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setW(el.clientWidth);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setW(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

export interface TimeViewApi {
  view: TimeView;
  follow: boolean;
  setFollow: (on: boolean) => void;
  zoom: (factor: number, anchor?: number) => void;
  pan: (dt: number) => void;
  fit: () => void;
}

/**
 * One shared time window for the river and the agent strips. While following, the right edge tracks `now`;
 * panning (or scrubbing) turns follow off and the window stays where the owner put it.
 */
export function useTimeView(extent: TimeView, now: number, initialFollow: boolean): TimeViewApi {
  const [follow, setFollowState] = useState(initialFollow);
  const [span, setSpan] = useState<number | null>(null);
  const [manual, setManual] = useState<TimeView | null>(null);
  const baseSpan = Math.min(MAX_SPAN, Math.max(MIN_SPAN, extent.t1 - extent.t0));

  const view = useMemo<TimeView>(() => {
    if (follow) {
      const s = span ?? baseSpan;
      const t1 = now + s * 0.04;
      return { t0: t1 - s, t1 };
    }
    return manual ?? extent;
  }, [follow, span, baseSpan, now, manual, extent]);

  const viewRef = useRef(view);
  viewRef.current = view;

  const setFollow = useCallback((on: boolean) => {
    if (on) { setManual(null); setSpan(viewRef.current.t1 - viewRef.current.t0); }
    else setManual(viewRef.current);
    setFollowState(on);
  }, []);
  const zoom = useCallback((factor: number, anchor = 0.5) => {
    const next = zoomView(viewRef.current, factor, follow ? 1 : anchor); // following keeps the right edge on "now"
    if (follow) setSpan(next.t1 - next.t0); else setManual(next);
  }, [follow]);
  const pan = useCallback((dt: number) => {
    setManual(panView(viewRef.current, dt));
    setFollowState(false);
  }, []);
  const fit = useCallback(() => { setManual(null); setSpan(null); setFollowState(false); }, []);
  return { view, follow, setFollow, zoom, pan, fit };
}

/**
 * Wheel (with Ctrl/⌘, which is also how a trackpad pinch arrives) zooms about the pointer, a horizontal wheel or
 * Shift+wheel pans, one pointer drags, two pointers pinch. Returns a ref that is true right after a drag, so a bar's
 * click handler can ignore the click that ends a pan.
 */
export function usePanZoom(ref: RefObject<HTMLElement | SVGElement>, opts: {
  plotLeft: number; plotWidth: number; view: TimeView; zoom: (factor: number, anchor: number) => void; pan: (dt: number) => void;
}): { justDragged: RefObject<boolean>; dragging: boolean } {
  const o = useRef(opts);
  o.current = opts;
  const justDragged = useRef(false);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const anchorAt = (clientX: number) => {
      const r = el.getBoundingClientRect();
      const { plotLeft, plotWidth } = o.current;
      return Math.min(1, Math.max(0, (clientX - r.left - plotLeft) / Math.max(1, plotWidth)));
    };
    const secPerPx = () => (o.current.view.t1 - o.current.view.t0) / Math.max(1, o.current.plotWidth);

    const onWheel = (e: Event) => {
      const w = e as WheelEvent;
      if (w.ctrlKey || w.metaKey) {
        w.preventDefault();
        o.current.zoom(Math.exp(w.deltaY * 0.0025), anchorAt(w.clientX));
      } else if (w.shiftKey || Math.abs(w.deltaX) > Math.abs(w.deltaY)) {
        w.preventDefault();
        o.current.pan((w.shiftKey && !w.deltaX ? w.deltaY : w.deltaX) * secPerPx());
      }
    };

    const pointers = new Map<number, { x: number; y: number }>();
    let startX = 0, last = 0, moved = false, pinch = 0;
    const onDown = (e: Event) => {
      const p = e as PointerEvent;
      if (p.button !== 0 && p.pointerType === "mouse") return;
      if ((p.target as Element).closest?.("[data-nodrag]")) return;
      pointers.set(p.pointerId, { x: p.clientX, y: p.clientY });
      if (pointers.size === 1) { startX = last = p.clientX; moved = false; }
      if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = Math.abs(a.x - b.x); }
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
      window.addEventListener("pointercancel", onUp);
    };
    const onMove = (e: PointerEvent) => {
      const prev = pointers.get(e.pointerId);
      if (!prev) return;
      pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
      if (pointers.size === 2) {
        const [a, b] = [...pointers.values()];
        const gap = Math.abs(a.x - b.x);
        if (pinch > 8 && gap > 8) o.current.zoom(pinch / gap, anchorAt((a.x + b.x) / 2));
        pinch = gap;
        moved = true;
        setDragging(true);
        return;
      }
      if (!moved && Math.abs(e.clientX - startX) < 4) return;
      moved = true;
      setDragging(true);
      o.current.pan(-(e.clientX - last) * secPerPx());
      last = e.clientX;
    };
    const onUp = (e: PointerEvent) => {
      pointers.delete(e.pointerId);
      if (pointers.size === 0) {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        window.removeEventListener("pointercancel", onUp);
        setDragging(false);
        if (moved) { justDragged.current = true; setTimeout(() => { justDragged.current = false; }, 0); }
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    el.addEventListener("pointerdown", onDown);
    return () => {
      el.removeEventListener("wheel", onWheel);
      el.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [ref]);

  return { justDragged, dragging };
}
