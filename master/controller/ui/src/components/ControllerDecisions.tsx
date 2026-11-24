import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get } from "../lib/api";
import { ago } from "../lib/format";
import { shortValue, decisionKinds, decisionRules, filterDecisions, isShadowMismatch, shadowMismatchCount, type Decision } from "../lib/decisions";

function Rows({ rows, showTask }: { rows: Decision[]; showTask: boolean }) {
  return (
    <div className="table-wrap">
      <table>
        <thead><tr><th>When</th><th>Kind</th>{showTask && <th>Task</th>}<th>Rule</th><th>Chosen</th><th>Shadow choice</th></tr></thead>
        <tbody>
          {rows.map((d) => {
            const mismatch = isShadowMismatch(d);
            return (
              <tr key={d.id} className={mismatch ? "shadow-mismatch" : undefined}>
                <td className="small quiet">{ago(d.ts)}</td>
                <td className="mono">{d.kind}</td>
                {showTask && <td className="mono small">{d.task_id ? <Link to={`/work?task=${encodeURIComponent(d.task_id)}`}>{d.task_id}</Link> : "—"}</td>}
                <td className="mono small">{d.rule ?? "—"}</td>
                <td className="mono small" title={JSON.stringify(d.chosen)}>{shortValue(d.chosen)}</td>
                <td className="mono small" title={JSON.stringify(d.shadow_choice)}>
                  {shortValue(d.shadow_choice)}{mismatch && <> <span className="tag tone-warn">shadow mismatch</span></>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Policies-page panel: the controller's recorded decisions (read-only), filterable by kind and rule. */
export function ControllerDecisions() {
  const q = useQuery({ queryKey: ["controller-decisions"], queryFn: () => get<Decision[]>("/api/decisions?limit=200") });
  const [kind, setKind] = useState("");
  const [rule, setRule] = useState("");
  const [mismatchOnly, setMismatchOnly] = useState(false);
  const all = q.data ?? [];
  const kinds = decisionKinds(all);
  const rules = decisionRules(all);
  // A refetch can drop the chosen option; fall back to "All" rather than filter on a value nobody can see.
  const kindOk = !kind || kinds.includes(kind);
  const ruleOk = !rule || rules.includes(rule);
  useEffect(() => {
    if (!q.data) return;
    if (!kindOk) setKind("");
    if (!ruleOk) setRule("");
  }, [q.data, kindOk, ruleOk]);
  const rows = filterDecisions(all, { kind: kindOk ? kind : "", rule: ruleOk ? rule : "", mismatchOnly });
  const mismatches = shadowMismatchCount(all);
  return (
    <section className="panel mt-3" aria-labelledby="ctl-decisions">
      <h3 id="ctl-decisions">Controller decisions</h3>
      <p className="small quiet">
        What the controller chose (placement, limit park and handoff), newest first, last 200. Read-only.
        {" "}Shadow data caveats: the shadow plan is computed just before the action runs, so a mismatch can be a timing artefact
        (snapshot race) rather than a policy difference. One limit can write two <span className="mono">limit.park</span> rows
        (rules <span className="mono">requeue_on_limit</span> and <span className="mono">handle_limit</span>); filter by rule when counting.
      </p>
      {q.error ? <p className="tone-bad small">{q.error instanceof Error ? q.error.message : String(q.error)}</p> : (
        <>
          <div className="row small" style={{ flexWrap: "wrap" }}>
            <label>Kind{" "}
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="">All</option>
                {kinds.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
            <label>Rule{" "}
              <select value={rule} onChange={(e) => setRule(e.target.value)}>
                <option value="">All</option>
                {rules.map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label><input type="checkbox" checked={mismatchOnly} onChange={(e) => setMismatchOnly(e.target.checked)} /> Shadow mismatches only</label>
            <span className={mismatches ? "tone-warn" : "quiet"}>{mismatches} shadow mismatch{mismatches === 1 ? "" : "es"} of {all.length}</span>
          </div>
          {q.isLoading ? <p className="quiet">Loading decisions…</p> : rows.length === 0 ? <p className="quiet">{all.length ? "No decisions match." : "No decisions recorded yet."}</p> : <Rows rows={rows} showTask />}
        </>
      )}
    </section>
  );
}

/** TaskSheet section: decisions recorded for one task. */
export function TaskDecisions({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["controller-decisions", id], queryFn: () => get<Decision[]>(`/api/decisions?task_id=${encodeURIComponent(id)}&limit=50`) });
  if (q.error || !q.data || q.data.length === 0) return (
    <><h3>Decisions</h3><p className="small quiet">{q.error ? "Could not load decisions." : q.isLoading ? "Loading…" : "None recorded for this task."}</p></>
  );
  return (<><h3>Decisions</h3><Rows rows={q.data} showTask={false} /></>);
}
