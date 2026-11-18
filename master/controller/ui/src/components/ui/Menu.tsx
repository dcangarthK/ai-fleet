import { useEffect, useId, useRef, useState, type CSSProperties, type KeyboardEvent, type ReactNode } from "react";
import { Button, type ButtonProps } from "./Button";
import { cx } from "./util";

export interface MenuItem {
  id: string;
  label: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
}

/** Button that opens a menu: arrows move, Home/End jump, Enter selects, Esc closes and refocuses the button. */
export function Menu({ label, items, align = "right", variant = "secondary", size = "md" }: {
  label: ReactNode;
  items: MenuItem[];
  align?: "left" | "right";
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const id = useId();
  // Fixed positioning keeps the popup from being clipped by a scrolling panel or table wrapper.
  const [pos, setPos] = useState<CSSProperties>({});

  const enabled = () => Array.from(wrap.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)') ?? []);

  useEffect(() => {
    if (!open) return;
    const r = btn.current?.getBoundingClientRect();
    if (r) setPos(align === "left" ? { top: r.bottom + 4, left: r.left } : { top: r.bottom + 4, right: window.innerWidth - r.right });
    enabled()[0]?.focus();
    const away = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    const close = () => setOpen(false);
    document.addEventListener("mousedown", away);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", away);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [open, align]);

  function onKey(e: KeyboardEvent) {
    const els = enabled();
    const i = els.indexOf(document.activeElement as HTMLElement);
    if (e.key === "Escape") { e.stopPropagation(); setOpen(false); btn.current?.focus(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); els[(i + 1) % els.length]?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); els[(i - 1 + els.length) % els.length]?.focus(); }
    else if (e.key === "Home") { e.preventDefault(); els[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); els[els.length - 1]?.focus(); }
    else if (e.key === "Tab") setOpen(false);
  }

  return (
    <div className="menu-wrap" ref={wrap}>
      <Button
        ref={btn} variant={variant} size={size} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? id : undefined}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => { if (e.key === "ArrowDown" && !open) { e.preventDefault(); setOpen(true); } }}
      >
        {label}
      </Button>
      {open && (
        <div className="menu" style={pos} role="menu" id={id} onKeyDown={onKey}>
          {items.map((it) => (
            <button
              key={it.id} type="button" role="menuitem" disabled={it.disabled} className={cx(it.danger && "danger")}
              onClick={() => { setOpen(false); btn.current?.focus(); it.onSelect(); }}
            >
              {it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
