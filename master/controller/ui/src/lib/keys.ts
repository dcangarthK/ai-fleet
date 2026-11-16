/** Keyboard shortcut helpers (pure, so they can be tested without a DOM). */
import { GO_TARGETS } from "./nav";

export interface KeyLike {
  key: string;
  metaKey?: boolean;
  ctrlKey?: boolean;
  altKey?: boolean;
  shiftKey?: boolean;
}

export const isPaletteHotkey = (e: KeyLike): boolean =>
  !!(e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === "k";

export const isHelpKey = (e: KeyLike): boolean => e.key === "?" && !e.metaKey && !e.ctrlKey && !e.altKey;

/** True when keystrokes belong to a text field and must not trigger single-key shortcuts. */
export function isTypingTarget(t: { tagName?: string; isContentEditable?: boolean; getAttribute?: (n: string) => string | null } | null | undefined): boolean {
  if (!t) return false;
  const tag = (t.tagName ?? "").toUpperCase();
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
  if (t.isContentEditable) return true;
  return t.getAttribute?.("role") === "combobox" || t.getAttribute?.("role") === "textbox";
}

export interface Sequencer {
  /** Feed a keydown; returns the path to jump to when a `g <key>` sequence completes. */
  feed(e: KeyLike): string | null;
  reset(): void;
}

/** Two-key `g n` style jumps. The prefix expires after `timeoutMs`. */
export function createSequencer(targets: Record<string, string> = GO_TARGETS, timeoutMs = 1200, now: () => number = Date.now): Sequencer {
  let armedAt: number | null = null;
  return {
    feed(e) {
      if (e.metaKey || e.ctrlKey || e.altKey) { armedAt = null; return null; }
      const k = e.key.toLowerCase();
      if (armedAt !== null && now() - armedAt <= timeoutMs) {
        armedAt = null;
        return targets[k] ?? null;
      }
      armedAt = k === "g" && !e.shiftKey ? now() : null;
      return null;
    },
    reset() { armedAt = null; },
  };
}

export const SHORTCUTS: { keys: string[]; label: string }[] = [
  { keys: ["⌘K", "Ctrl K"], label: "Open the command palette" },
  { keys: ["g n"], label: "Go to Now" },
  { keys: ["g w"], label: "Go to Work" },
  { keys: ["g p"], label: "Go to Plan" },
  { keys: ["g b"], label: "Go to Budget" },
  { keys: ["/"], label: "Search inside a focused terminal" },
  { keys: ["Esc"], label: "Close dialog, palette or full-screen terminal" },
  { keys: ["?"], label: "Show this help" },
];
