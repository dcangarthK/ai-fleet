/** Controller decisions (master/controller/decisions.py: GET /api/decisions) and related read-only helpers. */

export interface Decision {
  id: number;
  task_id: string | null;
  kind: string;
  inputs: unknown;
  candidates: unknown;
  rule: string | null;
  chosen: unknown;
  shadow_choice: unknown;
  ts: number;
}

export interface ReviewerProposal {
  agent: string;
  model: string | null;
  profile?: string | null;
  reason: string;
}

/** A typed brief (master/controller/briefs.py); every field optional because older tasks carry none. */
export interface Brief {
  goal?: string;
  files_allowed?: string[];
  acceptance?: string[];
  context_refs?: string[];
  done_when?: string[];
  gate?: string;
}

const isSet = (v: unknown) => v !== null && v !== undefined;

/** True when a shadow policy chose something different from what the live policy chose. */
export function isShadowMismatch(d: Pick<Decision, "chosen" | "shadow_choice">): boolean {
  return isSet(d.shadow_choice) && JSON.stringify(d.shadow_choice) !== JSON.stringify(d.chosen);
}

export function shadowMismatchCount(rows: Decision[]): number {
  return rows.filter(isShadowMismatch).length;
}

/** Distinct kinds, sorted, for the filter control. */
export function decisionKinds(rows: Decision[]): string[] {
  return Array.from(new Set(rows.map((r) => r.kind))).sort();
}

/** Filter by kind ("" = all), by rule ("" = all) and optionally to shadow mismatches only. */
export function filterDecisions(rows: Decision[], opts: { kind?: string; rule?: string; mismatchOnly?: boolean }): Decision[] {
  return rows.filter((r) => (!opts.kind || r.kind === opts.kind) && (!opts.rule || r.rule === opts.rule) && (!opts.mismatchOnly || isShadowMismatch(r)));
}

export function decisionRules(rows: Decision[]): string[] {
  return Array.from(new Set(rows.map((r) => r.rule).filter((x): x is string => !!x))).sort();
}

/** Short one-line text for a JSON value in a table cell. */
export function shortValue(v: unknown, max = 80): string {
  if (!isSet(v)) return "—";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}

/** Reader labels for events added by the limit, merge-close and placement work. */
export const EXTRA_EVENT_LABEL: Record<string, string> = {
  "task.auto_closed": "Closed automatically after merge",
  "task.auto_close_failed": "Automatic close after merge failed",
  "task.limit_wait": "Waiting for a usage limit to reset",
};

export function extraEventText(type: string, data: Record<string, unknown> | null): string | null {
  const d = data ?? {};
  const title = typeof d.title === "string" ? `“${d.title}”` : "a task";
  if (type === "task.auto_closed") return `${title} was closed automatically after its branch merged${d.cleaned ? " (worktree removed)" : ""}`;
  if (type === "task.auto_close_failed") return `Automatic close of ${title} failed${typeof d.stage === "string" ? ` at ${d.stage}` : ""}`;
  if (type === "task.limit_wait") {
    const nb = typeof d.not_before === "number" ? `, until ${new Date(d.not_before * 1000).toLocaleString()}` : "";
    return `${title} is waiting for a usage limit to reset${nb}`;
  }
  return null;
}
