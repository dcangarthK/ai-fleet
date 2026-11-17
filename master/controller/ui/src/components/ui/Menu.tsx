import { useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
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

  const enabled = () => Array.from(wrap.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)') ?? []);

  useEffect(() => {
    if (!open) return;
    enabled()[0]?.focus();
    const away = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

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
        <div className={cx("menu", align === "left" && "left")} role="menu" id={id} onKeyDown={onKey}>
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
