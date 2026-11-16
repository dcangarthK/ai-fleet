import { useQuery } from "@tanstack/react-query";
import { get } from "../../lib/api";
import { previewSpawn, type BudgetStatus, type BudgetUsage } from "../../lib/budget";
import { Badge } from "../ui";

/** Estimated tokens and cost, and the target model's budget state, shown before a spawn is confirmed. */
export function SpawnPreview({ kind, agent }: { kind: string; agent: string }) {
  const status = useQuery({ queryKey: ["budget", "status"], queryFn: () => get<BudgetStatus>("/api/budget/status"), retry: 0 });
  const usage = useQuery({ queryKey: ["budget", "usage", 30, "kind"], queryFn: () => get<BudgetUsage>("/api/budget/usage?days=30&group=kind"), retry: 0 });
  if (status.isLoading || usage.isLoading) return <p className="muted" aria-live="polite">Loading budget preview…</p>;
  if (status.error && usage.error) return <p className="muted">Budget preview is not available.</p>;
  const p = previewSpawn(usage.data?.rows ?? [], status.data?.models ?? [], kind, agent);
  return (
    <div role="group" aria-label="Budget preview" style={{ overflowWrap: "anywhere" }}>
      <p>
        Estimate: {p.tokens == null ? "unknown tokens" : `${p.tokens.toLocaleString()} tokens`}
        {p.cost != null && `, about USD ${p.cost.toFixed(2)}`} <span className="muted">({p.basis})</span>
      </p>
      <p>{agent} budget: <Badge tone={p.tone}>{p.state}</Badge>{p.blocked && " The controller may queue or refuse this."}</p>
    </div>
  );
}
