import { Link } from "react-router-dom";
import { useFleet } from "../lib/data";
import { LoadError, NodeLight, PageHead } from "../components/bits";

/** Which node serves which model, and which roles use it. Install/remove arrives with the catalog. */
export function ModelsPage() {
  const fleet = useFleet();
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.models.length);
  const assignments = fleet.data?.assignments ?? [];
  const names = Array.from(new Set(nodes.flatMap((n) => n.models))).sort();
  const size = (node: string, model: string) => {
    const n = fleet.data?.nodes.find((x) => x.id === node);
    for (const s of n?.inventory?.servers ?? []) {
      const m = s.models.find((x) => x.name === model);
      if (m?.size_gb) return `${m.size_gb} GB`;
    }
    return "";
  };

  return (
    <>
      <PageHead
        title="Models"
        intro="Every model the fleet can serve right now, by node. Adding models from a catalog and installing them on a node is the next step for this page."
      />
      <LoadError error={fleet.error} />
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              {nodes.map((n) => <th key={n.id}><span className="row" style={{ gap: 6 }}><NodeLight node={n} />{n.name}</span></th>)}
              <th>Roles</th>
            </tr>
          </thead>
          <tbody>
            {names.map((m) => {
              const roles = Array.from(new Set(assignments.filter((a) => a.model === m).map((a) => a.role)));
              return (
                <tr key={m}>
                  <td className="mono">{m}</td>
                  {nodes.map((n) => (
                    <td key={n.id} className="small">{n.models.includes(m) ? <span className="tone-ok">Installed</span> : <span className="quiet">—</span>}
                      {n.models.includes(m) && size(n.id, m) && <div className="sub">{size(n.id, m)}</div>}
                    </td>
                  ))}
                  <td>{roles.length ? roles.map((r) => <span className="tag" key={r}>{r}</span>) : <Link to="/roles" className="small">Assign</Link>}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!fleet.isLoading && names.length === 0 && (
          <div className="empty">No node serves a model yet. Install Ollama on a node (onboarding does this) and pull a model.</div>
        )}
      </div>
    </>
  );
}
