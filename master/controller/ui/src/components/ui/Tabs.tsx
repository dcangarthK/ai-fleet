import { useRef, useId, type KeyboardEvent, type ReactNode } from "react";
import { cx } from "./util";

export interface TabItem {
  id: string;
  label: ReactNode;
  count?: number | string;
  disabled?: boolean;
}

/** Tab list with roving tabindex: arrows move and select, Home/End jump. Pair with <TabPanel>. */
export function Tabs({ tabs, value, onChange, label, className, idPrefix }: {
  tabs: TabItem[];
  value: string;
  onChange: (id: string) => void;
  label: string;
  className?: string;
  /** Needed only when two tab lists share a page and you render <TabPanel> with the same prefix. */
  idPrefix?: string;
}) {
  const auto = useId();
  const prefix = idPrefix ?? auto;
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const enabled = tabs.filter((t) => !t.disabled);

  function onKey(e: KeyboardEvent) {
    const i = enabled.findIndex((t) => t.id === value);
    let next: TabItem | undefined;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = enabled[(i + 1) % enabled.length];
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = enabled[(i - 1 + enabled.length) % enabled.length];
    else if (e.key === "Home") next = enabled[0];
    else if (e.key === "End") next = enabled[enabled.length - 1];
    if (!next) return;
    e.preventDefault();
    onChange(next.id);
    refs.current[next.id]?.focus();
  }

  return (
    <div className={cx("tabs", className)} role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => {
        const selected = t.id === value;
        return (
          <button
            key={t.id}
            ref={(el) => { refs.current[t.id] = el; }}
            type="button" role="tab" id={`${prefix}-tab-${t.id}`} aria-controls={`${prefix}-panel-${t.id}`}
            aria-selected={selected} tabIndex={selected ? 0 : -1} disabled={t.disabled}
            onClick={() => onChange(t.id)}
          >
            {t.label}{t.count !== undefined && <span className="n">{t.count}</span>}
          </button>
        );
      })}
    </div>
  );
}

export function TabPanel({ id, value, idPrefix, children }: { id: string; value: string; idPrefix: string; children?: ReactNode }) {
  if (id !== value) return null;
  return (
    <div role="tabpanel" id={`${idPrefix}-panel-${id}`} aria-labelledby={`${idPrefix}-tab-${id}`} tabIndex={0}>
      {children}
    </div>
  );
}
