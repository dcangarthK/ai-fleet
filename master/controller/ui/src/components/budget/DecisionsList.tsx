import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get } from "../../lib/api";
import { clockTime, type Decision } from "../../lib/budget";
import { Empty, ErrorState, Field, Loading, Select, StatusPill, type Tone } from "../ui";

const TONE: Record<Decision["decision"], Tone> = { refused: "bad", queued: "warn", rerouted: "info" };

/** Recent admission decisions with the controller's reason spelled out; filter by decision. Nothing here is inferred. */
export function DecisionsList() {
  const q = useQuery({ queryKey: ["budget", "decisions"], queryFn: () => get<Decision[]>("/api/budget/decisions?limit=50") });
  const [filter, setFilter] = useState<"all" | Decision["decision"]>("all");
  const all = q.data ?? [];
  const rows = filter === "all" ? all : all.filter((d) => d.decision === filter);

  return (
    <div className="stack">
      <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
        <span className="small quiet">Every refusal, queue wait and reroute the budget rules made (last 50), with the controller's reason.</span>
        <Field label="Show" className="inline-field">
          {(p) => (
            <Select {...p} value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
              <option value="all">All ({all.length})</option>
              <option value="refused">Refused</option>
              <option value="queued">Queued</option>
              <option value="rerouted">Rerouted</option>
            </Select>
          )}
        </Field>
      </div>
      {q.isLoading ? <Loading rows={4} label="Loading decisions" /> :
        q.error ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> :
        rows.length === 0 ? (
          <Empty title={all.length ? "No decisions match" : "No decisions yet"}>
            {all.length ? "Nothing of that kind in the last 50." : "Decisions show here when the budget rules refuse, queue or reroute a new task."}
          </Empty>
        ) : (
          <ul className="stack" style={{ listStyle: "none", margin: 0, padding: 0 }} aria-label="Budget decisions">
            {rows.map((d) => (
              <li key={d.id} style={{ border: "1px solid var(--line)", borderRadius: "var(--r-lg)", padding: "10px 12px", background: "var(--surface)" }}>
                <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
                  <StatusPill tone={TONE[d.decision] ?? "quiet"}>{d.decision}</StatusPill>
                  <span className="mono small" title={new Date(d.ts * 1000).toLocaleString()}>{clockTime(d.ts)}</span>
                  {d.kind && <span className="small quiet">{d.kind.replace(/_/g, " ")}</span>}
                  <span style={{ marginLeft: "auto" }} className="small wrap-any">
                    {d.task_id
                      ? <Link to={`/work/${encodeURIComponent(d.task_id)}`}>{d.title ?? d.task_id}</Link>
                      : <span className="quiet">{d.title ?? "task not created"}</span>}
                  </span>
                </div>
                <details style={{ marginTop: 6 }}>
                  <summary className="small">Why?</summary>
                  <p className="wrap-any" style={{ marginTop: 4 }}>{d.reason}</p>
                </details>
              </li>
            ))}
          </ul>
        )}
    </div>
  );
}
