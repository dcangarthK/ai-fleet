import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { post, type Enrollment, type FleetNode, type Mode } from "../lib/api";
import { useCan, useFleet, useRun, useTasks, useUsage } from "../lib/data";
import { ago, duration, nodeSignal, SIGNAL_LABEL } from "../lib/format";
import { ErrorBanner, LoadError, NodeLight, PageHead, Section, StateLabel, Tags } from "../components/bits";

function useNodeActions() {
  const { run, busy, error, clearError } = useRun();
  return {
    busy, error, clearError,
    approve: (n: FleetNode) => run(() => post(`/api/nodes/${encodeURIComponent(n.id)}/approve`)),
    drain: (n: FleetNode) => confirm(`Drain ${n.name}? Its roles stop routing and no new tasks go to it.`) &&
      run(() => post(`/api/nodes/${encodeURIComponent(n.id)}/drain`)),
    ceiling: (n: FleetNode, mode: Mode) => run(() => post(`/api/nodes/${encodeURIComponent(n.id)}/ceiling`, { mode })),
    refresh: () => run(() => post("/api/refresh")),
    enroll: async () => (await run(() => post<Enrollment>("/api/enroll", {}))) ?? null,
  };
}

function CeilingSelect({ node, modes, onChange, disabled }: { node: FleetNode; modes: Mode[]; onChange: (m: Mode) => void; disabled: boolean }) {
  const can = useCan();
  if (node.status !== "approved") return <span className="quiet">—</span>;
  const set = node.ceiling ?? "auto-edit";
  if (!can.admin) return <>{set}{node.effective_ceiling && node.effective_ceiling !== set && <div className="sub">The node's runner caps it at {node.effective_ceiling}</div>}</>;
  return (
    <>
      <select aria-label={`Highest permission mode on ${node.name}`} value={set} disabled={disabled} onChange={(e) => onChange(e.target.value as Mode)}>
        {modes.map((m) => <option key={m}>{m}</option>)}
      </select>
      {node.effective_ceiling && node.effective_ceiling !== set && (
        <div className="sub">The node's runner caps it at {node.effective_ceiling}</div>
      )}
    </>
  );
}

export function NodesPage() {
  const fleet = useFleet();
  const can = useCan();
  const [enroll, setEnroll] = useState<Enrollment | null>(null);
  const nav = useNavigate();
  const a = useNodeActions();
  const nodes = fleet.data?.nodes ?? [];
  return (
    <>
      <PageHead
        title="Nodes"
        intro="Machines in the fleet, found on your tailnet. New nodes wait for your approval before they get work."
        actions={<>
          {can.operate && <button disabled={a.busy} onClick={a.refresh}>Rescan tailnet</button>}
          {can.admin && <button className="primary" disabled={a.busy} onClick={async () => setEnroll(await a.enroll())}>Add a node</button>}
        </>}
      />
      {enroll && (
        <div className="panel" style={{ padding: 14, marginBottom: 16 }} role="status">
          <b>Run this on the new machine</b> (it must already be on your tailnet):
          <pre className="out" style={{ userSelect: "all" }}>{enroll.command}</pre>
          <p className="small" style={{ margin: "8px 0" }}>
            When it asks for the enrollment code (input hidden), type:{" "}
            <code style={{ userSelect: "all", fontSize: "1.1em", letterSpacing: "0.08em" }}>{enroll.code}</code>
            <span className="quiet"> — the code is kept out of the command so it never lands in that machine's shell history.</span>
          </p>
          <div className="row small">
            <button type="button" onClick={() => navigator.clipboard?.writeText(enroll.command)}>Copy command</button>
            <span className="quiet">Works once, until {new Date(enroll.expires_at * 1000).toLocaleTimeString()}. The node then shows up here waiting for your approval.</span>
            <button type="button" className="ghost" onClick={() => setEnroll(null)}>Done</button>
          </div>
        </div>
      )}
      <LoadError error={fleet.error} />
      <ErrorBanner error={a.error} onDismiss={a.clearError} />
      <div className="panel">
        <table>
          <thead><tr><th>Node</th><th>Status</th><th className="hide-sm">Agents and models</th><th>Highest mode</th><th /></tr></thead>
          <tbody>
            {nodes.map((n) => (
              <tr key={n.id}>
                <td>
                  <Link to={`/nodes/${encodeURIComponent(n.id)}`} className="row" style={{ gap: 8 }}><NodeLight node={n} /><b>{n.name}</b></Link>
                  <div className="sub">{n.os} · <span className="mono">{n.ip}</span> · seen {ago(n.last_seen)}</div>
                </td>
                <td>{SIGNAL_LABEL[nodeSignal(n)]}
                  {n.runner?.power?.on_battery && <div className={`sub ${n.runner.power.ok === false ? "tone-warn" : ""}`}>on battery{n.runner.power.battery_pct != null && ` (${n.runner.power.battery_pct}%)`}{n.runner.power.ok === false && ": no agent tasks"}</div>}
                  {n.update_available && <div className="sub tone-info">runner update available</div>}
                </td>
                <td className="hide-sm">
                  <Tags items={Object.entries(n.runner?.agents ?? {}).filter(([, v]) => v).map(([k]) => k)} />
                  <div><Tags items={n.models} max={5} /></div>
                </td>
                <td><CeilingSelect node={n} modes={fleet.data?.modes ?? []} disabled={a.busy} onChange={(m) => a.ceiling(n, m)} /></td>
                <td>
                  {can.admin && n.id !== "master" && n.status !== "approved" && <button className="primary" disabled={a.busy} onClick={() => a.approve(n)}>Approve</button>}
                  {can.admin && n.id !== "master" && n.status === "approved" && <button disabled={a.busy} onClick={() => a.drain(n)}>Drain</button>}
                  {" "}<button className="ghost" onClick={() => nav(`/nodes/${encodeURIComponent(n.id)}`)}>Details</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!fleet.isLoading && nodes.length === 0 && (
          <div className="empty">No nodes yet. Copy <span className="mono">nodes/onboard.sh</span> to a machine on your tailnet and run it.</div>
        )}
      </div>
    </>
  );
}

export function NodeDetailPage() {
  const { id } = useParams();
  const can = useCan();
  const fleet = useFleet();
  const tasks = useTasks();
  const usage = useUsage(7);
  const a = useNodeActions();
  const node = fleet.data?.nodes.find((n) => n.id === id);
  if (fleet.isLoading) return <p className="quiet">Loading…</p>;
  if (fleet.error) return <><PageHead title="Node" /><LoadError error={fleet.error} /></>;
  if (!node) return <><PageHead title="Node not found" /><p>This node isn't in the fleet any more. <Link to="/nodes">Back to nodes</Link></p></>;

  const hw = node.inventory?.hardware ?? {};
  const recent = (tasks.data ?? []).filter((t) => t.node_id === node.id).slice(0, 10);
  const time = (usage.data?.rows ?? []).filter((u) => u.node_id === node.id);
  const roles = (fleet.data?.assignments ?? []).filter((r) => r.node_id === node.id);
  const r = node.runner;

  return (
    <>
      <PageHead
        title={node.name}
        intro={<span className="row" style={{ gap: 8 }}><NodeLight node={node} />{SIGNAL_LABEL[nodeSignal(node)]} · seen {ago(node.last_seen)}</span>}
        actions={can.admin && node.id !== "master" && (node.status !== "approved"
          ? <button className="primary" disabled={a.busy} onClick={() => a.approve(node)}>Approve node</button>
          : <button disabled={a.busy} onClick={() => a.drain(node)}>Drain node</button>)}
      />
      <ErrorBanner error={a.error} onDismiss={a.clearError} />

      <dl className="kv">
        <dt>Address</dt><dd className="mono">{node.ip}</dd>
        <dt>System</dt><dd>{[hw.os ?? node.os, hw.arch, hw.cpu].filter(Boolean).join(", ")}</dd>
        <dt>Memory</dt><dd>{hw.ram_gb ? `${hw.ram_gb} GB` : "unknown"}{hw.gpu && ` · ${hw.gpu}`}</dd>
        <dt>Model server</dt><dd>{node.engine ? `${node.engine} on port ${node.port} · ${node.healthy ? "answering" : "not answering"}` : "none found"}</dd>
        <dt>Runner</dt>
        <dd>{r ? `version ${r.version} · ${r.running} running · capped at ${r.max_mode}` : "not reachable"}</dd>
        {r?.allowed_roots && (<><dt>Folders</dt><dd>{r.allowed_roots.length ? <Tags items={r.allowed_roots} /> : <span className="tone-warn">No folders allowed yet; tasks can only use scratch space.</span>}</dd></>)}
        {r?.gate_profiles && (<><dt>Gates</dt><dd>{r.gate_profiles.length ? <Tags items={r.gate_profiles} /> : "none defined"}</dd></>)}
        {r?.power && (<><dt>Power</dt><dd>{r.power.on_battery == null ? "unknown" : r.power.on_battery ? `on battery${r.power.battery_pct != null ? ` (${r.power.battery_pct}%)` : ""}` : "plugged in"}
          {r.power.ok === false && <span className="tone-warn"> · below {r.power.min_battery_pct ?? 30}%, so Claude and Codex tasks wait</span>}</dd></>)}
        {node.update_available && (<><dt>Update</dt><dd><span className="tone-info">This node runs an older runner.</span> Run this on it (onboarding is safe to re-run and keeps its settings):
          <pre className="out" style={{ userSelect: "all" }}>{`curl -fsS ${window.location.origin}/enroll/onboard.sh | bash`}</pre></dd></>)}
        <dt>Highest mode</dt><dd><CeilingSelect node={node} modes={fleet.data?.modes ?? []} disabled={a.busy} onChange={(m) => a.ceiling(node, m)} /></dd>
      </dl>

      <Section title="Agents">
        <Tags items={Object.entries(r?.agents ?? {}).filter(([, v]) => v).map(([k]) => k)} />
        {(node.inventory?.agents ?? []).filter((x) => !(r?.agents ?? {})[x.cli]).length > 0 && (
          <p className="small quiet">Installed but not runnable here: {(node.inventory?.agents ?? []).filter((x) => !(r?.agents ?? {})[x.cli]).map((x) => x.cli).join(", ")}</p>
        )}
      </Section>

      <Section title="Models" aside={`${node.models.length} served`}>
        {node.models.length ? <Tags items={node.models} max={50} /> : <p className="quiet">This node doesn't serve any models.</p>}
        {(node.inventory?.servers ?? []).filter((s) => !s.reachable_from_master && node.id !== "master").map((s) => (
          <div className="banner note small" key={s.port}>{s.engine} on port {s.port}: {s.hint}</div>
        ))}
      </Section>

      <Section title="Roles">
        {roles.length ? roles.map((x) => <span className="tag" key={x.role + x.model}>{x.role}: {x.model}</span>) : <p className="quiet">No roles routed to this node. <Link to="/roles">Assign one</Link>.</p>}
      </Section>

      <Section title="Recent tasks" aside={time.length ? `agent time this week: ${duration(time.reduce((s, u) => s + u.seconds, 0))}` : undefined}>
        <div className="panel">
          <table>
            <tbody>
              {recent.map((t) => (
                <tr key={t.id}><td><Link to={`/tasks?tab=all&task=${t.id}`}>{t.title}</Link></td><td><StateLabel state={t.state} /></td><td className="quiet small">{ago(t.updated_at)}</td></tr>
              ))}
            </tbody>
          </table>
          {recent.length === 0 && <div className="empty">No tasks have run here yet.</div>}
        </div>
      </Section>
    </>
  );
}
