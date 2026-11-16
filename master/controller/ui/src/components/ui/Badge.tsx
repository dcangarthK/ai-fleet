import type { ReactNode } from "react";
import { cx, type Tone } from "./util";

const PILL: Record<Tone, string> = { ok: "p-ok", warn: "p-warn", bad: "p-bad", info: "p-info", accent: "p-accent", quiet: "p-quiet" };

/** Small counter or label chip. */
const BADGE_BG: Record<Tone, string> = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", info: "var(--info)", accent: "var(--accent)", quiet: "var(--ink-3)" };

export function Badge({ tone = "bad", children, label }: { tone?: Tone; children: ReactNode; label?: string }) {
  return <span className="badge" aria-label={label} style={{ background: BADGE_BG[tone] }}>{children}</span>;
}

/** Status with a dot; colour is never the only signal because the text always says what it is. */
export function StatusPill({ tone = "quiet", children, dot = true, title }: { tone?: Tone; children: ReactNode; dot?: boolean; title?: string }) {
  return <span className={cx("pill", PILL[tone], !dot && "no-dot")} title={title}>{children}</span>;
}
