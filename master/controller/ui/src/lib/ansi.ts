/** ANSI SGR parsing for the Terminal. Colours map to theme status tokens, never to raw palette values. */

export type AnsiTone = "bad" | "ok" | "warn" | "accent" | "info" | "ink" | "ink-3";

export interface AnsiStyle {
  fg?: AnsiTone;
  bg?: AnsiTone;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
}

export interface AnsiSpan extends AnsiStyle {
  text: string;
}

/** Standard 8 colours (30-37 / 40-47) and bright (90-97 / 100-107), by index 0-7. */
const BASE: AnsiTone[] = ["ink-3", "bad", "ok", "warn", "accent", "info", "accent", "ink"];
const BRIGHT: AnsiTone[] = ["ink-3", "bad", "ok", "warn", "accent", "info", "accent", "ink"];

function tone256(n: number): AnsiTone | undefined {
  if (n < 8) return BASE[n];
  if (n < 16) return BRIGHT[n - 8];
  if (n >= 232) return n >= 244 ? "ink" : "ink-3";
  const c = n - 16;
  return toneRgb(Math.floor(c / 36) * 51, Math.floor((c % 36) / 6) * 51, (c % 6) * 51);
}

/** Nearest status tone by hue; greys fall back to the ink tones. */
export function toneRgb(r: number, g: number, b: number): AnsiTone {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max - min < 40) return max > 128 ? "ink" : "ink-3";
  let h: number;
  if (max === r) h = ((g - b) / (max - min)) * 60;
  else if (max === g) h = (2 + (b - r) / (max - min)) * 60;
  else h = (4 + (r - g) / (max - min)) * 60;
  if (h < 0) h += 360;
  if (h < 20 || h >= 330) return "bad";
  if (h < 70) return "warn";
  if (h < 165) return "ok";
  if (h < 255) return "accent";
  return "info";
}

// CSI sequences (any final byte), OSC sequences, and lone two-byte escapes.
const ESC = String.fromCharCode(27);
const BEL = String.fromCharCode(7);
const TOKEN = new RegExp(`${ESC}\\[([0-9;:]*)([\\x20-\\x2f]*[\\x40-\\x7e])|${ESC}\\][^${BEL}${ESC}]*(?:${BEL}|${ESC}\\\\)|${ESC}[@-Z\\\\-_]|${ESC}(?:\\[[0-9;:]*)?$`, "g");

export function stripAnsi(text: string): string {
  return text.replace(TOKEN, "");
}

/** Apply one SGR parameter list to a style; returns the new style. */
export function applySgr(style: AnsiStyle, params: string): AnsiStyle {
  const codes = params === "" ? [0] : params.split(/[;:]/).map((p) => (p === "" ? 0 : Number(p)));
  let s: AnsiStyle = { ...style };
  for (let i = 0; i < codes.length; i++) {
    const c = codes[i];
    if (Number.isNaN(c)) continue;
    if (c === 0) s = {};
    else if (c === 1) s.bold = true;
    else if (c === 2) s.dim = true;
    else if (c === 3) s.italic = true;
    else if (c === 4) s.underline = true;
    else if (c === 22) { s.bold = false; s.dim = false; }
    else if (c === 23) s.italic = false;
    else if (c === 24) s.underline = false;
    else if (c >= 30 && c <= 37) s.fg = BASE[c - 30];
    else if (c >= 90 && c <= 97) s.fg = BRIGHT[c - 90];
    else if (c >= 40 && c <= 47) s.bg = BASE[c - 40];
    else if (c >= 100 && c <= 107) s.bg = BRIGHT[c - 100];
    else if (c === 39) s.fg = undefined;
    else if (c === 49) s.bg = undefined;
    else if (c === 38 || c === 48) {
      const key = c === 38 ? "fg" : "bg";
      if (codes[i + 1] === 5) {
        s[key] = tone256(codes[i + 2] ?? -1);
        i += 2;
      } else if (codes[i + 1] === 2) {
        s[key] = toneRgb(codes[i + 2] ?? 0, codes[i + 3] ?? 0, codes[i + 4] ?? 0);
        i += 4;
      }
    }
  }
  return s;
}

/**
 * Parse one line (no newline) into styled spans. `start` carries style across lines, as terminals do.
 * A carriage return discards what came before it on the line (progress bars overwrite themselves).
 */
export function parseAnsiLine(line: string, start: AnsiStyle = {}): { spans: AnsiSpan[]; end: AnsiStyle } {
  const cr = line.lastIndexOf("\r");
  const src = cr >= 0 && cr < line.length - 1 ? line.slice(cr + 1) : line.replace(/\r/g, "");
  const spans: AnsiSpan[] = [];
  let style = start;
  let last = 0;
  const push = (text: string) => {
    if (text) spans.push({ text, ...style });
  };
  TOKEN.lastIndex = 0;
  for (let m = TOKEN.exec(src); m; m = TOKEN.exec(src)) {
    push(src.slice(last, m.index));
    last = m.index + m[0].length;
    if (m[2] === "m") style = applySgr(style, m[1] ?? "");
  }
  push(src.slice(last));
  return { spans, end: style };
}

export interface ParsedLine {
  spans: AnsiSpan[];
  plain: string;
}

/** Parse a whole log. A trailing newline does not produce an empty last line. */
export function parseAnsi(text: string): ParsedLine[] {
  const raw = text.split("\n");
  if (raw.length > 1 && raw[raw.length - 1] === "") raw.pop();
  let style: AnsiStyle = {};
  return raw.map((l) => {
    const { spans, end } = parseAnsiLine(l, style);
    style = end;
    return { spans, plain: spans.map((s) => s.text).join("") };
  });
}
