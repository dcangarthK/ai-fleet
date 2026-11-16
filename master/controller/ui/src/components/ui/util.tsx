import type { CSSProperties, ReactNode } from "react";

export const cx = (...parts: (string | false | null | undefined)[]): string => parts.filter(Boolean).join(" ");

/** Single-line text that ellipsises and keeps the full value in a tooltip. */
export function Truncate({ text, max, className, style }: { text: string; max?: number | string; className?: string; style?: CSSProperties }) {
  return (
    <span className={cx("trunc", className)} title={text} style={max !== undefined ? { maxWidth: max, ...style } : style}>
      {text}
    </span>
  );
}

export type Tone = "ok" | "warn" | "bad" | "info" | "accent" | "quiet";

export type Children = { children?: ReactNode };
