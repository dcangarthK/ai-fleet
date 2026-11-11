import { useState, type FormEvent } from "react";
import { del, post } from "../lib/api";
import { useFleet, useRun } from "../lib/data";
import { ErrorBanner, LoadError, PageHead } from "../components/bits";

const SUGGESTED = ["coder", "reviewer", "fast"];

export function RolesPage() {
  const fleet = useFleet();
  const { run, busy, error, clearError } = useRun();
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved");
  const [role, setRole] = useState("coder");
  const [node, setNode] = useState("");
  const [model, setModel] = useState("");
  const models = nodes.find((n) => n.id === node)?.models ?? [];
  const assignments = fleet.data?.assignments ?? [];
  const roles = Array.from(new Set([...SUGGESTED, ...assignments.map((a) => a.role)]));

  async function assign(e: FormEvent) {
    e.preventDefault();
    await run(() => post("/api/roles", { role, node, model }));
  }

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
                const n = nodes.find((x) => x.id === a.node_id) ?? fleet.data?.nodes.find((x) => x.id === a.node_id);
                const live = !!n?.healthy && n.models.includes(a.model);
                return (
                  <tr key={r + a.node_id + a.model}>
                    <td>{i === 0 && <b>{r}</b>}</td>
                    <td className="mono">{a.model}</td>
                    <td>{a.node ?? a.node_id}</td>
                    <td>{live ? <span className="state tone-ok">Live</span> : <span className="state tone-bad">Not routed</span>}
                      {!live && <div className="sub">{!n ? "node is gone" : !n.healthy ? "model server not answering" : "model no longer on the node"}</div>}
                    </td>
                    <td>
                      <button disabled={busy} onClick={() => confirm(`Stop routing ${r} to ${a.model} on ${a.node}?`) &&
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
                <option value="" disabled>{node ? (models.length ? "Choose a model" : "This node has no models") : "Choose a node first"}</option>
                {models.map((m) => <option key={m}>{m}</option>)}
              </select>
            </label>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="primary" disabled={busy || !node || !model}>Assign</button>
            <span className="quiet small">Only models a node already has are listed. Installing new ones from here comes with the model catalog.</span>
          </div>
        </form>
      </section>
    </>
  );
}
