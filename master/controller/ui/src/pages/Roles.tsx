import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { del, post, type Assignment, type FleetNode } from "../lib/api";
import { useFleet, useModels, useRun } from "../lib/data";
import { explainReason, FIT_LABEL } from "../lib/format";
import { ErrorBanner, LoadError, PageHead } from "../components/bits";

const SUGGESTED = ["coder", "reviewer", "fast"];

function routing(a: Assignment, n: FleetNode | undefined, reason: string | null | undefined) {
  if (a.state === "pending_install") return { tone: "accent", label: "Waiting for install", why: "Switches on when the model is installed and tested." };
  if (a.state === "blocked" || a.state === "failed") return { tone: a.state === "blocked" ? "warn" : "bad", label: a.state === "blocked" ? "Install blocked" : "Install failed", why: explainReason(reason) };
  const live = !!n?.healthy && n.models.includes(a.model);
  if (live) return { tone: "ok", label: "Live", why: "" };
  return { tone: "bad", label: "Not routed", why: !n ? "The node is gone." : !n.healthy ? "The node's model server isn't answering." : "The model is no longer on the node." };
}

export function RolesPage() {
  const fleet = useFleet();
  const models = useModels();
  const { run, busy, error, clearError } = useRun();
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved");
  const [role, setRole] = useState("coder");
  const [node, setNode] = useState("");
  const [model, setModel] = useState("");
  const assignments = fleet.data?.assignments ?? [];
  const roles = Array.from(new Set([...SUGGESTED, ...assignments.map((a) => a.role)]));
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
    await run(() => post("/api/roles", { role, node, model, ensure_model: needsInstall, catalog_id: entry?.id }));
  }

  const reasonFor = (a: Assignment) => {
    const m = catalog.find((x) => x.reference === a.model || x.id === a.catalog_id);
    return m?.nodes[a.node_id]?.reason;
  };

  return (
    <>
      <PageHead
        title="Roles"
        intro={<>A role is a name agents call (for example <span className="mono">coder</span>). Each role routes to one or more models on the fleet; with several, requests are balanced between them.</>}
      />
      <LoadError error={fleet.error} />
      <ErrorBanner error={error} onDismiss={clearError} />

      <div className="panel">
        <table>
          <thead><tr><th>Role</th><th>Model</th><th>Node</th><th>Routing</th><th /></tr></thead>
          <tbody>
            {roles.flatMap((r) => {
              const rows = assignments.filter((a) => a.role === r);
              if (!rows.length) {
                return [<tr key={r}><td><b>{r}</b></td><td colSpan={4} className="quiet">No model assigned. Calls to this role fail until you assign one.</td></tr>];
              }
              return rows.map((a, i) => {
                const n = fleet.data?.nodes.find((x) => x.id === a.node_id);
                const st = routing(a, n, reasonFor(a));
                return (
                  <tr key={r + a.node_id + a.model}>
                    <td>{i === 0 && <b>{r}</b>}</td>
                    <td className="mono">{a.model}</td>
                    <td>{a.node ?? a.node_id}</td>
                    <td><span className={`state tone-${st.tone}`}>{st.label}</span>{st.why && <div className="sub" style={{ maxWidth: 300 }}>{st.why}</div>}</td>
                    <td>
                      <button disabled={busy} onClick={() => confirm(`Stop routing ${r} to ${a.model} on ${a.node ?? a.node_id}?`) &&
                        run(() => del("/api/roles", { role: a.role, node: a.node_id, model: a.model }))}>Remove</button>
                    </td>
                  </tr>
                );
              });
            })}
          </tbody>
        </table>
      </div>

      <section className="section">
        <h2>Assign a model to a role</h2>
        <form className="panel" style={{ padding: 14 }} onSubmit={assign}>
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
            <button className="primary" disabled={busy || !node || !model}>{needsInstall ? "Install and assign" : "Assign"}</button>
            <span className="quiet small">
              {needsInstall ? "The node downloads and tests the model first; the role switches on when it's ready."
                : remoteOff ? "This node doesn't allow remote model installs, so only its installed models are offered."
                : <>Models not on the node come from the <Link to="/models">catalog</Link> and are installed for you.</>}
            </span>
          </div>
        </form>
      </section>
    </>
  );
}
