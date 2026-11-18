/** Minimal hook harness for the node test environment (no DOM, no extra dependencies).
 *  Use with: vi.mock("react", async () => (await import("./testHarness")).reactMock). Test-only. */

interface Effect {
  deps?: readonly unknown[];
  cleanup?: void | (() => void);
}

let slots: unknown[] = [];
let effects: Effect[] = [];
let pending: Array<() => void> = [];
let slot = 0;
let eff = 0;
let rerenderNow: () => void = () => {};
let rendering = false;

const same = (a?: readonly unknown[], b?: readonly unknown[]) =>
  !!a && !!b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));

export const reactMock = {
  useState<T>(init: T | (() => T)) {
    const i = slot++;
    if (!(i in slots)) slots[i] = typeof init === "function" ? (init as () => T)() : init;
    const set = (v: T | ((p: T) => T)) => {
      const next = typeof v === "function" ? (v as (p: T) => T)(slots[i] as T) : v;
      if (Object.is(next, slots[i])) return;
      slots[i] = next;
      if (!rendering) rerenderNow();
    };
    return [slots[i] as T, set] as const;
  },
  useRef<T>(init: T) {
    const i = slot++;
    if (!(i in slots)) slots[i] = { current: init };
    return slots[i] as { current: T };
  },
  useEffect(cb: () => void | (() => void), deps?: readonly unknown[]) {
    const i = eff++;
    const prev = effects[i];
    if (prev && same(prev.deps, deps)) return;
    pending.push(() => {
      if (prev && typeof prev.cleanup === "function") prev.cleanup();
      effects[i] = { deps, cleanup: cb() };
    });
    effects[i] = { deps, cleanup: prev?.cleanup };
  },
  useCallback<T>(fn: T) {
    return fn;
  },
  useMemo<T>(fn: () => T) {
    return fn();
  },
};

export function renderHook<P, R>(hook: (p: P) => R, initial: NoInfer<P>) {
  slots = [];
  effects = [];
  pending = [];
  let props = initial;
  const out = { current: undefined as unknown as R };
  const run = () => {
    rendering = true;
    slot = 0;
    eff = 0;
    out.current = hook(props);
    rendering = false;
    const todo = pending;
    pending = [];
    todo.forEach((f) => f());
  };
  rerenderNow = run;
  run();
  return {
    result: out,
    rerender(p: P) {
      props = p;
      run();
    },
    unmount() {
      effects.forEach((e) => typeof e.cleanup === "function" && e.cleanup());
    },
  };
}
