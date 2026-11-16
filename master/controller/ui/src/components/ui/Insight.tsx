import { useState, type ReactNode } from "react";
import { Button } from "./Button";
import { useConfirm } from "./Dialog";
import { Confidence, EvidenceLinks, type Evidence } from "./Evidence";
import { cx, type Tone } from "./util";

export interface InsightAction {
  label: string;
  /** Runs only after the owner confirms. Nothing in an insight executes on its own. */
  run: () => void | Promise<unknown>;
  /** Shown in the confirmation: the exact request that will be made. */
  confirmMessage?: ReactNode;
  danger?: boolean;
  disabled?: boolean;
}

/** One proposal from the intelligence layer: what, why, how sure, on what evidence, and one confirmed action. */
export function InsightCard({ kind, title, why, confidence, evidence = [], action, tone = "accent" }: {
  kind: string;
  title: ReactNode;
  why?: ReactNode;
  confidence?: number;
  evidence?: Evidence[];
  action?: InsightAction;
  tone?: Tone;
}) {
  const confirm = useConfirm();
  const [busy, setBusy] = useState(false);

  async function go() {
    if (!action) return;
    const ok = await confirm({
      title: action.label,
      message: action.confirmMessage ?? <>Run “{action.label}”? Nothing happens until you confirm.</>,
      confirmLabel: action.label,
      danger: action.danger,
    });
    if (!ok) return;
    setBusy(true);
    try { await action.run(); } finally { setBusy(false); }
  }

  return (
    <article className={cx("insight", tone === "warn" && "warn", tone === "bad" && "bad", tone === "ok" && "ok")}>
      <span className="kind">{kind}</span>
      <h3>{title}</h3>
      {why && <p className="why">{why}</p>}
      <EvidenceLinks items={evidence} />
      <div className="foot">
        {confidence !== undefined ? <Confidence value={confidence} /> : <span />}
        {action && <Button variant={action.danger ? "danger" : "primary"} size="sm" disabled={busy || action.disabled} onClick={go}>{action.label}</Button>}
      </div>
    </article>
  );
}
