import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { del, post, put, type Assignment, type FleetNode } from "../lib/api";
import { useCan, useFleet, useModels, useRun } from "../lib/data";
import { explainReason, FIT_LABEL } from "../lib/format";
import { ErrorBanner, LoadError, PageHead } from "../components/bits";
import { describeRequest } from "../lib/request";
import { Button, Card, Panel, Select, Table, useConfirm } from "../components/ui";

const SUGGESTED = ["coder", "reviewer", "fast"];

function routing(a: Assignment, n: FleetNode | undefined, reason: string | null | undefined) {
  if (a.state === "pending_install") return { tone: "accent", label: "Waiting for install", why: "Switches on when the model is installed and tested." };
  if (a.state === "blocked" || a.state === "failed") return { tone: a.state === "blocked" ? "warn" : "bad", label: a.state === "blocked" ? "Install blocked" : "Install failed", why: explainReason(reason) };
  // SQLite stores routed as 0/1; treat any non-null value as the gateway's answer.
  const routed = a.routed == null ? null : Boolean(a.routed);
  if (routed === true) return { tone: "ok", label: "Live", why: "" };
  if (routed === false) return { tone: "bad", label: "Not routed", why: a.route_reason ?? "No deployment exists in the gateway." };
  // Older controllers don't report routing; fall back to node health.
  const live = !!n?.healthy && n.models.includes(a.model);
  if (live) return { tone: "ok", label: "Probably live", why: "The gateway didn't report routing; the node and model are up." };
  return { tone: "bad", label: "Not routed", why: !n ? "The node is gone." : !n.healthy ? "The node's model server isn't answering." : "The model is no longer on the node." };
}

export function RolesPage() {
  const fleet = useFleet();
  const models = useModels();
  const { run, busy, error, clearError } = useRun();
  const can = useCan();
  const confirm = useConfirm();
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved");
  const [role, setRole] = useState("coder");
  const [node, setNode] = useState("");
  const [model, setModel] = useState("");
  const assignments = fleet.data?.assignments ?? [];
  const roles = Array.from(new Set([...SUGGESTED, ...assignments.map((a) => a.role), ...(fleet.data?.roles ?? []).map((r) => r.name)]));
  const chosen = nodes.find((n) => n.id === node);
  const catalog = models.data ?? [];

  // Models on the node, plus catalog models that would be installed on assignment.
  const onNode = new Set(chosen?.models ?? []);
  const options = [
    ...Array.from(onNode).sort().map((ref) => ({ ref, note: "installed" })),
    ...catalog.filter((m) => !onNode.has(m.reference)).map((m) => {
      const fit = m.nodes[node]?.fit ?? "unknown";
      return { ref: m.reference, note: `will install${m.download_gb ? ` (${m.download_gb.toFixed(1)} GB)` : ""} · ${FIT_LABEL[fit].label.toLowerCase()}` };
    }),
  ];
  const needsInstall = !!model && !onNode.has(model);
  const remoteOff = chosen?.runner?.models?.remote_management === false;

  async function assign(e: FormEvent) {
    e.preventDefault();
    const entry = catalog.find((m) => m.reference === model);
    const body = { role, node, model, ensure_model: needsInstall, catalog_id: entry?.id };
    const ok = await confirm({ title: `Assign ${role} to ${model}?`, message: describeRequest("POST", "/api/roles", body), confirmLabel: "Assign" });
    if (ok) await run(() => post("/api/roles", body));
  }

  async function remove(a: Assignment) {
    const ok = await confirm({ title: "Remove role assignment?", message: `Stop routing ${a.role} to ${a.model} on ${a.node ?? a.node_id}? ${describeRequest("DELETE", "/api/roles", { role: a.role, node: a.node_id, model: a.model })}`, confirmLabel: "Remove", danger: true });
    if (ok) await run(() => del("/api/roles", { role: a.role, node: a.node_id, model: a.model }));
  }

  const reasonFor = (a: Assignment) => {
    const m = catalog.find((x) => x.reference === a.model || x.id === a.catalog_id);
    return m?.nodes[a.node_id]?.reason;
  };

  const rows = roles.flatMap((r) => {
    const list = assignments.filter((a) => a.role === r);
    if (!list.length) return [{ key: r, role: r, first: true, a: null as Assignment | null, st: null as ReturnType<typeof routing> | null }];
    return list.map((a, i) => ({ key: r + a.node_id + a.model, role: r, first: i === 0, a: a as Assignment | null, st: routing(a, fleet.data?.nodes.find((x) => x.id === a.node_id), reasonFor(a)) }));
  });

  return (
    <>
      <PageHead
        title="Roles"
        intro={<>A role is a name agents call (for example <span className="mono">coder</span>). Each role routes to one or more models on the fleet; with several, requests are balanced between them.</>}
      />
      <LoadError error={fleet.error ?? models.error} />
      <ErrorBanner error={error} onDismiss={clearError} />

      <Panel>
        <Table
          caption="Role assignments"
          rows={rows}
          rowKey={(r) => r.key}
          columns={[
            { key: "role", header: "Role", cell: (r) => (r.first ? <b>{r.role}</b> : null) },
            { key: "model", header: "Model", cell: (r) => r.a ? <span className="mono">{r.a.model}</span> : <span className="quiet">No model assigned. Calls to this role fail until you assign one.</span> },
            { key: "node", header: "Node", cell: (r) => r.a ? (r.a.node ?? r.a.node_id) : null },
            { key: "routing", header: "Routing", cell: (r) => r.st ? <><span className={`state tone-${r.st.tone}`}>{r.st.label}</span>{r.st.why && <div className="sub" style={{ maxWidth: 300 }}>{r.st.why}</div>}</> : null },
            { key: "act", header: "Actions", label: "", cell: (r) => r.a && can.admin ? <Button size="sm" variant="danger" disabled={busy} onClick={() => remove(r.a!)}>Remove</Button> : null },
          ]}
        />
      </Panel>

      <section className="section">
        <h2>When a role fails</h2>
        <Panel>
          <Table
            caption="Fallback roles"
            rows={roles}
            rowKey={(r) => r}
            columns={[
              { key: "role", header: "Role", cell: (r) => <b>{r}</b> },
              { key: "next", header: "Try next", cell: (r) => {
                const info = fleet.data?.roles?.find((x) => x.name === r);
                return can.admin ? (
                  <Select aria-label={`Fallback for ${r}`} value={info?.fallback ?? ""} disabled={busy}
                          onChange={async (e) => {
                            const path = `/api/roles/${encodeURIComponent(r)}`;
                            const body = { fallback: e.target.value || null, description: info?.description ?? null };
                            const ok = await confirm({ title: `Change fallback for ${r}?`, message: describeRequest("PUT", path, body), confirmLabel: "Change fallback" });
                            if (ok) await run(() => put(path, body));
                          }}>
                    <option value="">Nothing: report the error</option>
                    {roles.filter((x) => x !== r).map((x) => <option key={x} value={x}>{x}</option>)}
                  </Select>
                ) : (info?.fallback ?? "nothing");
              } },
            ]}
          />
        </Panel>
        <p className="small quiet">If a role's models error or time out, the request is tried once on the fallback role and the answer says which role replied.</p>
      </section>

      {can.admin && <section className="section">
        <h2>Assign a model to a role</h2>
        <Card as="div"><form onSubmit={assign}>
          <div className="grid-form">
            <label className="field">Role
              <input list="role-names" value={role} onChange={(e) => setRole(e.target.value)} required pattern="[a-z][a-z0-9-]{1,31}"
                     title="Lowercase letters, digits and dashes" />
              <datalist id="role-names">{roles.map((r) => <option key={r}>{r}</option>)}</datalist>
            </label>
            <label className="field">Node
              <select value={node} onChange={(e) => { setNode(e.target.value); setModel(""); }} required>
                <option value="" disabled>Choose a node</option>
                {nodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
              </select>
            </label>
            <label className="field">Model
              <select value={model} onChange={(e) => setModel(e.target.value)} required disabled={!node}>
                <option value="" disabled>{node ? "Choose a model" : "Choose a node first"}</option>
                {options.map((o) => <option key={o.ref} value={o.ref} disabled={o.note !== "installed" && remoteOff}>{o.ref} ({o.note})</option>)}
              </select>
            </label>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <Button type="submit" variant="primary" disabled={busy || !node || !model}>{needsInstall ? "Install and assign" : "Assign"}</Button>
            <span className="quiet small">
              {needsInstall ? "The node downloads and tests the model first; the role switches on when it's ready."
                : remoteOff ? "This node doesn't allow remote model installs, so only its installed models are offered."
                : <>Models not on the node come from the <Link to="/models">catalog</Link> and are installed for you.</>}
            </span>
          </div>
        </form></Card>
      </section>}
    </>
  );
}
