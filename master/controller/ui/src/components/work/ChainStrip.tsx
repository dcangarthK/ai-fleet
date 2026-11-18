import type { ChainStage } from "../../lib/work";
import { useReducedMotion } from "../lineage/hooks";
import { useVisible } from "../motion/useVisible";
import "../motion/work-motion.css";

const MARK: Record<string, string> = { done: "●", now: "◐", bad: "✕", plan: "○" };

/** The chain strip: one chip per stage, status in text as well as colour. Click selects the stage's latest attempt.
 *  Flow runs only on the connector into the in-progress stage (data: the derived stage status). */
export function ChainStrip({ chain, selected, onSelect }: { chain: ChainStage[]; selected: string | null; onSelect: (taskId: string) => void }) {
  const visible = useVisible();
  const reduced = useReducedMotion();
  return (
    <ol className="row list-reset wm-chain" aria-label="Chain">
      {chain.map((s, i) => (
        <li key={s.name}>
          {i > 0 && <span className={`wm-conn${s.status === "now" && !reduced ? " flow" : ""}${visible ? "" : " paused"}`} aria-hidden="true"><i /></span>}
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
