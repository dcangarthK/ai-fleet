import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { post, type Enrollment, type FleetNode, type Mode } from "../lib/api";
import { useCan, useFleet, useRun, useTasks, useUsage } from "../lib/data";
import { ago, duration, nodeSignal, SIGNAL_LABEL } from "../lib/format";
import { ErrorBanner, LoadError, NodeLight, PageHead, Section, StateLabel, Tags } from "../components/bits";
import { Button, Card, Empty, Loading, Panel, Select, Table, useConfirm, type Column } from "../components/ui";
import { describeRequest } from "../lib/request";
import { Terminal } from "../components/Terminal";

function useNodeActions() {
  const { run, busy, error, clearError } = useRun();
  const confirm = useConfirm();
  return {
    busy, error, clearError,
    approve: async (n: FleetNode) => {
      const path = `/api/nodes/${encodeURIComponent(n.id)}/approve`;
      const ok = await confirm({ title: `Approve ${n.name}?`, message: describeRequest("POST", path), confirmLabel: "Approve node" });
      if (ok) await run(() => post(path));
    },
    drain: async (n: FleetNode) => {
      const ok = await confirm({ title: `Drain ${n.name}?`, message: "Its roles stop routing and no new tasks go to it.", confirmLabel: "Drain node", danger: true });
      if (ok) await run(() => post(`/api/nodes/${encodeURIComponent(n.id)}/drain`));
    },
    ceiling: async (n: FleetNode, mode: Mode) => {
      const path = `/api/nodes/${encodeURIComponent(n.id)}/ceiling`;
      const ok = await confirm({ title: `Set highest mode on ${n.name} to ${mode}?`, message: describeRequest("POST", path, { mode }), confirmLabel: "Set mode" });
      if (ok) await run(() => post(path, { mode }));
    },
    refresh: async () => {
      const ok = await confirm({ title: "Rescan tailnet?", message: describeRequest("POST", "/api/refresh"), confirmLabel: "Rescan" });
      if (ok) await run(() => post("/api/refresh"));
    },
    enroll: async () => {
      const ok = await confirm({ title: "Create an enrollment code?", message: describeRequest("POST", "/api/enroll", {}), confirmLabel: "Create" });
      return ok ? ((await run(() => post<Enrollment>("/api/enroll", {}))) ?? null) : null;
    },
  };
}

function CeilingSelect({ node, modes, onChange, disabled }: { node: FleetNode; modes: Mode[]; onChange: (m: Mode) => void; disabled: boolean }) {
  const can = useCan();
  if (node.status !== "approved") return <span className="quiet">—</span>;
  const set = node.ceiling ?? "auto-edit";
  if (!can.admin) return <>{set}{node.effective_ceiling && node.effective_ceiling !== set && <div className="sub">The node's runner caps it at {node.effective_ceiling}</div>}</>;
  return (
    <>
      <Select aria-label={`Highest permission mode on ${node.name}`} value={set} disabled={disabled} onChange={(e) => onChange(e.target.value as Mode)}>
        {modes.map((m) => <option key={m}>{m}</option>)}
      </Select>
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
  const cols: Column<FleetNode>[] = [
    { key: "node", header: "Node", cell: (n) => (
      <>
        <Link to={`/nodes/${encodeURIComponent(n.id)}`} className="row" style={{ gap: "var(--s2)" }}><NodeLight node={n} /><b>{n.name}</b></Link>
        <div className="sub">{n.os} · <span className="mono">{n.ip}</span> · seen {ago(n.last_seen)}</div>
      </>
    ) },
    { key: "status", header: "Status", cell: (n) => (
      <>
        {SIGNAL_LABEL[nodeSignal(n)]}
        {n.runner?.power?.on_battery && <div className={`sub ${n.runner.power.ok === false ? "tone-warn" : ""}`}>on battery{n.runner.power.battery_pct != null && ` (${n.runner.power.battery_pct}%)`}{n.runner.power.ok === false && ": no agent tasks"}</div>}
        {n.update_available && <div className="sub tone-info">runner update available</div>}
      </>
    ) },
    { key: "models", header: "Agents and models", hideStacked: true, cell: (n) => (
      <>
        <Tags items={Object.entries(n.runner?.agents ?? {}).filter(([, v]) => v).map(([k]) => k)} />
        <div><Tags items={n.models} max={5} /></div>
      </>
    ) },
    { key: "mode", header: "Highest mode", cell: (n) => <CeilingSelect node={n} modes={fleet.data?.modes ?? []} disabled={a.busy} onChange={(m) => a.ceiling(n, m)} /> },
    { key: "act", header: "Actions", label: "", cell: (n) => (
      <span className="row">
        {can.admin && n.id !== "master" && n.status !== "approved" && <Button variant="primary" size="sm" disabled={a.busy} onClick={() => a.approve(n)}>Approve</Button>}
        {can.admin && n.id !== "master" && n.status === "approved" && <Button size="sm" disabled={a.busy} onClick={() => a.drain(n)}>Drain</Button>}
        <Button variant="ghost" size="sm" onClick={() => nav(`/nodes/${encodeURIComponent(n.id)}`)}>Details</Button>
      </span>
    ) },
  ];
  return (
    <>
      <PageHead
        title="Nodes"
        intro="Machines in the fleet, found on your tailnet. New nodes wait for your approval before they get work."
        actions={<>
          {can.operate && <Button disabled={a.busy} onClick={a.refresh}>Rescan tailnet</Button>}
          {can.admin && <Button variant="primary" disabled={a.busy} onClick={async () => setEnroll(await a.enroll())}>Add a node</Button>}
        </>}
      />
      {enroll && (
        <Card title="Run this on the new machine" actions={<Button variant="ghost" size="sm" onClick={() => setEnroll(null)}>Done</Button>}>
          <div role="status" className="stack">
            <p className="small quiet" style={{ margin: 0 }}>It must already be on your tailnet.</p>
            <Terminal title="new machine" text={`$ ${enroll.command}`} height={120} label="Enrollment command" />
            <p className="small" style={{ margin: 0 }}>
              When it asks for the enrollment code (input hidden), type:{" "}
              <code style={{ userSelect: "all", fontSize: "1.1em", letterSpacing: "0.08em" }}>{enroll.code}</code>
              <span className="quiet"> — the code is kept out of the command so it never lands in that machine's shell history.</span>
            </p>
            <div className="row small">
              <Button size="sm" onClick={() => navigator.clipboard?.writeText(enroll.command)}>Copy command</Button>
              <span className="quiet">Works once, until {new Date(enroll.expires_at * 1000).toLocaleTimeString()}. The node then shows up here waiting for your approval.</span>
            </div>
          </div>
        </Card>
      )}
      <LoadError error={fleet.error} />
      <ErrorBanner error={a.error} onDismiss={a.clearError} />
      <Panel>
        {fleet.isLoading ? <Loading label="Loading nodes" /> : <Table columns={cols} rows={nodes} rowKey={(n) => n.id} caption="Nodes" />}
        {!fleet.isLoading && nodes.length === 0 && (
          <Empty title="No nodes yet">Copy <span className="mono">nodes/onboard.sh</span> to a machine on your tailnet and run it.</Empty>
        )}
      </Panel>
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
  if (fleet.isLoading) return <Loading label="Loading node" />;
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
          ? <Button variant="primary" disabled={a.busy} onClick={() => a.approve(node)}>Approve node</Button>
          : <Button disabled={a.busy} onClick={() => a.drain(node)}>Drain node</Button>)}
      />
      <ErrorBanner error={a.error} onDismiss={a.clearError} />

      <Card title="Details"><dl className="kv">
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
          <Terminal title="on that node" text={`$ curl -fsS ${window.location.origin}/enroll/onboard.sh | bash`} height={90} label="Update command" /></dd></>)}
        <dt>Highest mode</dt><dd><CeilingSelect node={node} modes={fleet.data?.modes ?? []} disabled={a.busy} onChange={(m) => a.ceiling(node, m)} /></dd>
      </dl></Card>

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
        <Panel>
          <Table
            caption="Recent tasks" rows={recent} rowKey={(t) => t.id}
            columns={[
              { key: "t", header: "Task", cell: (t) => <Link to={`/tasks?tab=all&task=${t.id}`}>{t.title}</Link> },
              { key: "s", header: "State", cell: (t) => <StateLabel state={t.state} /> },
              { key: "u", header: "Updated", cell: (t) => <span className="quiet small">{ago(t.updated_at)}</span> },
            ]}
          />
          {recent.length === 0 && <Empty title="No tasks have run here yet" />}
        </Panel>
      </Section>
    </>
  );
}
