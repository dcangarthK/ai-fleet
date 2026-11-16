import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { createSequencer, isHelpKey, isPaletteHotkey, isTypingTarget, SHORTCUTS } from "../lib/keys";
import { getPaletteProviders, type PaletteItem } from "../lib/palette";
import { Dialog } from "./ui";

/** Extension point: later work registers intent, search and action providers here. See lib/palette.ts. */
export { registerPaletteProvider, type PaletteItem, type PaletteProvider } from "../lib/palette";

/** Command palette: type to filter, arrows move, Enter runs, Esc closes. Items come from registered providers. */
export function Palette({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<PaletteItem[]>([]);
  const [active, setActive] = useState(0);
  const listId = useId();
  const seq = useRef(0);

  useEffect(() => { if (open) { setQuery(""); setActive(0); } }, [open]);

  useEffect(() => {
    if (!open) return;
    const mine = ++seq.current;
    Promise.all(getPaletteProviders().map(async (p) => { try { return await p.items(query); } catch { return []; } }))
      .then((lists) => { if (mine === seq.current) { setItems(lists.flat()); setActive(0); } });
  }, [open, query]);

  const groups = useMemo(() => {
    const out: { name: string; items: { item: PaletteItem; index: number }[] }[] = [];
    items.forEach((item, index) => {
      const name = item.group ?? "Results";
      let g = out.find((x) => x.name === name);
      if (!g) out.push((g = { name, items: [] }));
      g.items.push({ item, index });
    });
    return out;
  }, [items]);

  function choose(item: PaletteItem | undefined) {
    if (!item) return;
    onClose();
    if (item.to) navigate(item.to);
    else item.run?.();
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => (items.length ? (a + 1) % items.length : 0)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => (items.length ? (a - 1 + items.length) % items.length : 0)); }
    else if (e.key === "Enter") { e.preventDefault(); choose(items[active]); }
  }

  useEffect(() => {
    document.getElementById(`${listId}-${active}`)?.scrollIntoView({ block: "nearest" });
  }, [active, listId]);

  return (
    <Dialog open={open} onClose={onClose} size="palette" label="Command palette" initialFocus="input">
      <input
        className="palette-input" role="combobox" aria-expanded="true" aria-controls={listId} aria-autocomplete="list"
        aria-activedescendant={items.length ? `${listId}-${active}` : undefined} aria-label="Search pages and actions"
        placeholder="Search pages and actions" value={query} autoComplete="off" spellCheck={false}
        onChange={(e) => setQuery(e.target.value)} onKeyDown={onKey}
      />
      <ul className="palette-list" role="listbox" id={listId} aria-label="Results">
        {groups.map((g) => (
          <li key={g.name} role="presentation">
            <h3 className="palette-group">{g.name}</h3>
            <ul role="presentation" style={{ listStyle: "none", margin: 0, padding: 0 }}>
              {g.items.map(({ item, index }) => (
                <li
                  key={item.id} id={`${listId}-${index}`} role="option" aria-selected={index === active} className="palette-item"
                  onMouseMove={() => setActive(index)} onClick={() => choose(item)}
                >
                  <span className="trunc" title={item.title}>{item.title}</span>
                  {item.hint && <span className="hint">{item.hint}</span>}
                </li>
              ))}
            </ul>
          </li>
        ))}
        {items.length === 0 && <li role="presentation" className="empty">Nothing matches “{query}”.</li>}
      </ul>
      <div className="palette-foot" aria-hidden="true">
        <span><kbd>↑</kbd> <kbd>↓</kbd> move</span>
        <span><kbd>Enter</kbd> open</span>
        <span><kbd>Esc</kbd> close</span>
      </div>
    </Dialog>
  );
}

export function HelpDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  return (
    <Dialog open={open} onClose={onClose} title="Keyboard shortcuts">
      <ul className="help-list">
        {SHORTCUTS.map((s) => (
          <li key={s.label}>
            <span>{s.label}</span>
            <span className="keys">{s.keys.map((k) => <kbd key={k}>{k}</kbd>)}</span>
          </li>
        ))}
      </ul>
    </Dialog>
  );
}

/** Global shortcuts: ⌘K / Ctrl K, `g n` style jumps, `?`. Ignored while typing in a field. */
export function useShortcuts({ openPalette, openHelp, blocked }: { openPalette: () => void; openHelp: () => void; blocked: boolean }) {
  const navigate = useNavigate();
  const seq = useMemo(() => createSequencer(), []);
  useEffect(() => {
    const on = (e: globalThis.KeyboardEvent) => {
      if (isPaletteHotkey(e)) { e.preventDefault(); openPalette(); return; }
      if (blocked || e.defaultPrevented || isTypingTarget(e.target as HTMLElement)) return;
      if (isHelpKey(e)) { e.preventDefault(); openHelp(); return; }
      const to = seq.feed(e);
      if (to) { e.preventDefault(); navigate(to); }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [navigate, openPalette, openHelp, blocked, seq]);
}
