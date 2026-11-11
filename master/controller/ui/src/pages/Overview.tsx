import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get, type CallOutcome, type CallStat } from "../lib/api";
import { ErrorBanner, LoadError, NodeLight, PageHead, Section, StateLabel } from "../components/bits";
import { taskAction, useCan, useFleet, useRun, useTasks, useUsage } from "../lib/data";
import { ago, denialTools, duration, needsYou, nodeName, nodeSignal, pctText } from "../lib/format";

// Outcome counts only exist for grounded calls (G4); older controllers omit them.
const cnt = (c: CallStat, k: CallOutcome) => (c.grounded ? (c.outcomes?.[k] ?? 0) : "—");

export function OverviewPage() {
  const fleet = useFleet();
  const tasks = useTasks();
  const usage = useUsage(1);
  const nav = useNavigate();
  const { run, busy, error, clearError } = useRun();
  const can = useCan();

  const nodes = fleet.data?.nodes ?? [];
  const all = tasks.data ?? [];
  const waiting = all.filter(needsYou);
  const working = all.filter((t) => t.state === "working" || t.state === "starting");
  const review = all.filter((t) => t.state === "review");
  const problems = all.filter((t) => t.state === "failed" || t.state === "interrupted");
  const up = nodes.filter((n) => nodeSignal(n) === "up").length;
  const routed = (fleet.data?.assignments ?? []).filter((a) => {
    if (a.routed != null) return Boolean(a.routed);
    const n = nodes.find((x) => x.id === a.node_id);
    return n?.healthy && n.models.includes(a.model);
  }).length;
  const calls = useQuery({ queryKey: ["calls"], queryFn: () => get<CallStat[]>("/api/usage/calls?days=7"), retry: 0 });
  const agentTime = (usage.data ?? []).reduce((s, r) => s + (r.seconds || 0), 0);
  const open = (id: string) => nav(`/tasks?tab=all&task=${encodeURIComponent(id)}`);

  return (
    <>
      <PageHead title="Overview" intro="What the fleet is doing and what's waiting on you." />
      <LoadError error={fleet.error ?? tasks.error} />
      <ErrorBanner error={error} onDismiss={clearError} />

      <div className="stats" aria-label="Fleet summary">
        <div className="stat"><b>{up}/{nodes.length}</b><span>nodes up</span></div>
        <div className="stat"><b>{working.length}</b><span>tasks working</span></div>
        <div className="stat"><b className={waiting.length ? "tone-warn" : ""}>{waiting.length}</b><span>need you</span></div>
        <div className="stat"><b>{review.length}</b><span>ready for review</span></div>
        <div className="stat"><b>{routed}/{fleet.data?.assignments.length ?? 0}</b><span>role routes live</span></div>
        <div className="stat"><b>{duration(agentTime)}</b><span>agent time today</span></div>
      </div>

      <Section title="Needs you" aside={waiting.length ? `${waiting.length} waiting` : undefined}>
        {waiting.length === 0 ? (
          <p className="quiet">Nothing is waiting on you.</p>
        ) : (
          <div className="asks">
            {waiting.map((t) => (
              <article className="ask" key={t.id}>
                <h3>{t.title}</h3>
                <p>
                  {t.state === "awaiting_approval"
                    ? <>Wants to start in <b>{t.mode}</b> mode on {nodeName(nodes, t.node_id)}.</>
                    : <>Stopped after being denied {denialTools(t).map((x) => <span className="tag" key={x}>{x}</span>)}</>}
                </p>
                <div className="row">
                  {can.admin && <button className="primary" disabled={busy} onClick={() => run(() => taskAction(t.id, "approve"))}>Approve</button>}
                  {can.admin && <button className="danger" disabled={busy} onClick={() => run(() => taskAction(t.id, "reject"))}>Reject</button>}
                  <button className="ghost" onClick={() => open(t.id)}>Details</button>
                </div>
              </article>
            ))}
          </div>
        )}
      </Section>

      <Section title="Working now">
        <div className="panel">
          <table>
            <thead><tr><th>Task</th><th>State</th><th className="hide-sm">Node</th><th>Started</th></tr></thead>
            <tbody>
              {[...working, ...problems.slice(0, 5)].map((t) => (
                <tr key={t.id} className="click" onClick={() => open(t.id)}>
                  <td><Link to={`/tasks?tab=all&task=${encodeURIComponent(t.id)}`} onClick={(e) => e.stopPropagation()}><b>{t.title}</b></Link><div className="sub">{t.agent}{t.model && ` (${t.model})`}</div></td>
                  <td><StateLabel state={t.state} /></td>
                  <td className="hide-sm">{nodeName(nodes, t.node_id)}</td>
                  <td className="quiet small">{ago(t.started_at ?? t.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {working.length + problems.length === 0 && <div className="empty">No tasks running and no failures. <Link to="/tasks">Start a task</Link>.</div>}
        </div>
      </Section>

      {(calls.data ?? []).length > 0 && (
        <Section title="Model calls" aside="last 7 days">
          <div className="panel">
            <table>
              <thead><tr><th>Role</th><th>Model</th><th className="num">Calls</th><th className="num">Errors</th><th className="num hide-sm">Typical</th><th className="num hide-sm">Slowest 5%</th><th className="num hide-sm">Fallbacks</th><th className="num hide-sm">Answered</th><th className="num hide-sm">Abstained</th><th className="num hide-sm">Escalated</th><th className="num hide-sm">Failed check</th></tr></thead>
              <tbody>
                {(calls.data ?? []).map((c) => (
                  <tr key={c.role + c.model}>
                    <td>{c.role}</td><td className="mono">{c.model}</td><td className="num">{c.count}</td>
                    <td className={`num ${c.error_rate > 0.1 ? "tone-bad" : ""}`}>{Math.round(c.error_rate * 100)}%</td>
                    <td className="num hide-sm">{(c.p50_ms / 1000).toFixed(1)}s</td><td className="num hide-sm">{(c.p95_ms / 1000).toFixed(1)}s</td>
                    <td className="num hide-sm">{c.fallbacks}</td>
                    <td className="num hide-sm">{cnt(c, "answered")}</td>
                    <td className="num hide-sm">{cnt(c, "abstained")}{c.abstain_rate != null && <div className="sub">{pctText(c.abstain_rate)}</div>}</td>
                    <td className="num hide-sm">{cnt(c, "escalated")}{c.escalation_rate != null && <div className="sub">{pctText(c.escalation_rate)}</div>}</td>
                    <td className={`num hide-sm ${(c.check_failed_rate ?? 0) > 0.1 ? "tone-bad" : ""}`}>{cnt(c, "check_failed")}{c.check_failed_rate != null && <div className="sub">{pctText(c.check_failed_rate)}</div>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      <Section title="Nodes">
        <div className="panel">
          <table>
            <thead><tr><th>Node</th><th className="hide-sm">Hardware</th><th>Agents</th><th className="num">Models</th></tr></thead>
            <tbody>
              {nodes.map((n) => (
                <tr key={n.id} className="click" onClick={() => nav(`/nodes/${encodeURIComponent(n.id)}`)}>
                  <td><Link to={`/nodes/${encodeURIComponent(n.id)}`} className="row" style={{ gap: 8 }} onClick={(e) => e.stopPropagation()}><NodeLight node={n} /><b>{n.name}</b></Link><div className="sub">seen {ago(n.last_seen)}</div></td>
                  <td className="hide-sm small">{n.inventory?.hardware?.ram_gb ? `${n.inventory.hardware.ram_gb} GB RAM` : "—"}<div className="sub">{n.inventory?.hardware?.cpu || n.os}</div></td>
                  <td className="small">{Object.entries(n.runner?.agents ?? {}).filter(([, v]) => v).map(([k]) => k).join(", ") || <span className="quiet">no runner</span>}</td>
                  <td className="num">{n.models.length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </>
  );
}
