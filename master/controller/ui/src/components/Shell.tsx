import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { BUSY, useFleet, useLiveUpdates, useModels, useTasks } from "../lib/data";
import { INSTALL_LABEL, needsYou, nodeSignal, progressFraction, SIGNAL_LABEL } from "../lib/format";
import { NodeLight } from "./bits";

const NAV: { to: string; label: string; later?: string }[] = [
  { to: "/", label: "Overview" },
  { to: "/tasks", label: "Tasks" },
  { to: "/models", label: "Models" },
  { to: "/roles", label: "Roles" },
  { to: "/nodes", label: "Nodes" },
  { to: "/policies", label: "Policies", later: "soon" },
  { to: "/skills", label: "Skills", later: "soon" },
  { to: "/evals", label: "Evals", later: "soon" },
  { to: "/activity", label: "Activity" },
  { to: "/settings", label: "Settings" },
];

export function Shell() {
  const live = useLiveUpdates();
  const fleet = useFleet();
  const tasks = useTasks();
  const [menu, setMenu] = useState(false);
  const loc = useLocation();
  useEffect(() => setMenu(false), [loc.pathname]);

  const nodes = fleet.data?.nodes ?? [];
  const waiting = (tasks.data ?? []).filter(needsYou).length;
  const runningOn = (id: string) => (tasks.data ?? []).filter((t) => t.node_id === id && t.state === "working").length;

  return (
    <div className="shell">
      <div className="mobile-bar">
        <button onClick={() => setMenu(true)} aria-label="Open navigation">Menu</button>
        <div className="lights" aria-label="Fleet status">
          {nodes.map((n) => (
            <Link key={n.id} to={`/nodes/${encodeURIComponent(n.id)}`} className="row" style={{ gap: 5, color: "inherit", textDecoration: "none" }}>
              <NodeLight node={n} />
              {n.name}
            </Link>
          ))}
        </div>
        {waiting > 0 && (
          <Link to="/tasks?tab=waiting" className="tone-warn small" style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>
            {waiting} need you
          </Link>
        )}
      </div>

      <aside className={`side ${menu ? "open" : ""}`} aria-label="Navigation">
        <div className="brand">
          <b>AI Fleet</b>
          <span className="live" title={live ? "Live updates connected" : "Live updates disconnected; refreshing every 30s"}>
            <span className={`light sse ${live ? "on" : ""}`} />
            {live ? "live" : "offline"}
          </span>
          {menu && (
            <button className="ghost" style={{ marginLeft: "auto" }} onClick={() => setMenu(false)} aria-label="Close navigation">
              Close
            </button>
          )}
        </div>

        <nav className="nav">
          {NAV.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"}>
              <span>{item.label}</span>
              {item.to === "/tasks" && waiting > 0 && <span className="count" aria-label={`${waiting} need you`}>{waiting}</span>}
              {item.later && <span className="later">{item.later}</span>}
            </NavLink>
          ))}
        </nav>

        <div className="rail">
          <h2>Fleet</h2>
          {fleet.isLoading && <p className="quiet small" style={{ margin: "0 8px" }}>Loading nodes…</p>}
          <ul>
            {nodes.map((n) => {
              const running = runningOn(n.id);
              const signal = nodeSignal(n);
              return (
                <li key={n.id}>
                  <Link to={`/nodes/${encodeURIComponent(n.id)}`} title={`${n.name}: ${SIGNAL_LABEL[signal]}`}>
                    <NodeLight node={n} />
                    <span className="name">{n.name}</span>
                    <span className="meta">
                      {signal === "pending" ? "approve" : running ? `${running} running` : n.inventory?.hardware?.ram_gb ? `${n.inventory.hardware.ram_gb} GB` : ""}
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
          {!fleet.isLoading && nodes.length === 0 && (
            <p className="quiet small" style={{ margin: "0 8px" }}>
              No nodes yet. Run <span className="mono">nodes/onboard.sh</span> on a machine to add one.
            </p>
          )}
        </div>
      </aside>

      <main className="main" id="main">
        <InstallStrip />
        <Outlet />
      </main>
    </div>
  );
}

/** Model installs and removals in progress, shown above every page until they finish. */
function InstallStrip() {
  const models = useModels();
  const fleet = useFleet();
  const ops = (models.data ?? []).flatMap((m) =>
    Object.entries(m.nodes).filter(([, n]) => n.state && BUSY.has(n.state)).map(([node, n]) => ({ m, node, n })));
  if (!ops.length) return null;
  const name = (id: string) => fleet.data?.nodes.find((x) => x.id === id)?.name ?? id;
  return (
    <div className="ops" role="status" aria-live="polite">
      {ops.map(({ m, node, n }) => {
        const frac = progressFraction(n.progress);
        return (
          <Link key={m.id + node} to="/models" className="op" style={{ color: "inherit", textDecoration: "none" }}>
            <span>{INSTALL_LABEL[n.state ?? "pending"].label} <b>{m.display_name || m.reference}</b> on {name(node)}</span>
            <span className="bar"><span style={{ width: frac == null ? "30%" : `${Math.round(frac * 100)}%` }} className={frac == null ? "indeterminate" : ""} /></span>
          </Link>
        );
      })}
    </div>
  );
}
