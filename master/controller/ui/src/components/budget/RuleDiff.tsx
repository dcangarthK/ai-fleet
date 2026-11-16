import { Empty } from "../ui";

export interface DiffRow { path: string; before: string; after: string }

/** Leaf-by-leaf rule differences (lib/budget.ts rulesDiff). Removed values read red, added values green; the signs say so too. */
export function RuleDiff({ rows, label, emptyText = "No differences." }: { rows: DiffRow[]; label: string; emptyText?: string }) {
  if (rows.length === 0) return <Empty title={emptyText} />;
  return (
    <div role="group" aria-label={label} className="mono small" style={{ border: "1px solid var(--line)", borderRadius: "var(--r-lg)", background: "var(--sunken)", overflow: "auto", maxHeight: 320 }}>
      {rows.map((r) => (
        <div key={r.path} style={{ padding: "6px 12px", borderBottom: "1px solid var(--line)" }}>
          <div style={{ color: "var(--ink-3)" }} className="wrap-any">{r.path}</div>
          <div style={{ color: "var(--bad)" }} className="wrap-any">- {r.before}</div>
          <div style={{ color: "var(--ok)" }} className="wrap-any">+ {r.after}</div>
        </div>
      ))}
    </div>
  );
}
