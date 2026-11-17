import type { ChainStage } from "../../lib/work";

const MARK: Record<string, string> = { done: "●", now: "◐", bad: "✕", plan: "○" };

/** The chain strip: one chip per stage, status in text as well as colour. Click selects the stage's latest attempt. */
export function ChainStrip({ chain, selected, onSelect }: { chain: ChainStage[]; selected: string | null; onSelect: (taskId: string) => void }) {
  return (
    <ol className="row list-reset" aria-label="Chain">
      {chain.map((s) => (
        <li key={s.name}>
          <button
            type="button" disabled={!s.taskId} aria-current={s.taskId === selected ? "true" : undefined}
            className={s.taskId === selected ? "chain-btn primary" : "chain-btn"} onClick={() => s.taskId && onSelect(s.taskId)}
            title={[s.model, s.account, s.node].filter(Boolean).join(" · ")}
          >
            <span aria-hidden="true">{MARK[s.status]}</span> <b>{s.label}</b> <span className="muted">{s.detail}{s.model ? ` · ${s.model}` : ""}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}
