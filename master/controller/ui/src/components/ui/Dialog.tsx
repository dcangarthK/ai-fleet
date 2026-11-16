import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Button } from "./Button";
import { cx } from "./util";

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';

/** Modal dialog: focus moves in, Tab is trapped, Esc and scrim click close, focus returns to the opener. */
export function Dialog({ open, onClose, title, hideTitle, size = "md", footer, children, initialFocus, label }: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  hideTitle?: boolean;
  size?: "md" | "lg" | "palette";
  footer?: ReactNode;
  children?: ReactNode;
  /** CSS selector inside the dialog to focus first; defaults to the first focusable element. */
  initialFocus?: string;
  /** aria-label for dialogs without a visible title. */
  label?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const root = ref.current;
    const target = (initialFocus && root?.querySelector<HTMLElement>(initialFocus)) || root?.querySelector<HTMLElement>(FOCUSABLE) || root;
    target?.focus();
    return () => {
      if (opener && document.contains(opener)) opener.focus();
    };
  }, [open, initialFocus]);

  if (!open) return null;

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === "Escape") {
      e.stopPropagation();
      closeRef.current();
      return;
    }
    if (e.key !== "Tab") return;
    const items = Array.from(ref.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []).filter((el) => el.offsetParent !== null || el === document.activeElement);
    if (items.length === 0) { e.preventDefault(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  return createPortal(
    <div className="dlg-scrim" onMouseDown={(e) => e.target === e.currentTarget && closeRef.current()}>
      <div
        ref={ref} role="dialog" aria-modal="true" tabIndex={-1} className={cx("dlg", size === "lg" && "lg", size === "palette" && "palette")}
        aria-labelledby={title && !hideTitle ? titleId : undefined} aria-label={!title || hideTitle ? label : undefined}
        onKeyDown={onKeyDown}
      >
        {title && !hideTitle && (
          <div className="dlg-head">
            <h2 id={titleId}>{title}</h2>
            <Button variant="ghost" size="sm" aria-label="Close dialog" onClick={onClose}>Close</Button>
          </div>
        )}
        <div className="dlg-body" style={size === "palette" ? { padding: 0 } : undefined}>{children}</div>
        {footer && <div className="dlg-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}

export interface ConfirmOptions {
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  /** Styles the confirm button as destructive. */
  danger?: boolean;
}

type Ask = (o: ConfirmOptions) => Promise<boolean>;
const ConfirmContext = createContext<Ask | null>(null);

/** Mount once near the root; `useConfirm()` anywhere below returns a promise-based replacement for window.confirm. */
export function ConfirmProvider({ children }: { children?: ReactNode }) {
  const [state, setState] = useState<{ opts: ConfirmOptions; resolve: (v: boolean) => void } | null>(null);
  const ask = useCallback<Ask>((opts) => new Promise<boolean>((resolve) => setState({ opts, resolve })), []);
  const settle = (v: boolean) => {
    state?.resolve(v);
    setState(null);
  };
  return (
    <ConfirmContext.Provider value={ask}>
      {children}
      <Dialog
        open={!!state} onClose={() => settle(false)} title={state?.opts.title}
        initialFocus="[data-confirm]"
        footer={state && (
          <>
            <Button onClick={() => settle(false)}>{state.opts.cancelLabel ?? "Cancel"}</Button>
            <Button data-confirm variant={state.opts.danger ? "danger" : "primary"} onClick={() => settle(true)}>{state.opts.confirmLabel ?? "Confirm"}</Button>
          </>
        )}
      >
        {state?.opts.message}
      </Dialog>
    </ConfirmContext.Provider>
  );
}

export function useConfirm(): Ask {
  const ask = useContext(ConfirmContext);
  if (!ask) throw new Error("useConfirm needs <ConfirmProvider> above it");
  return ask;
}
