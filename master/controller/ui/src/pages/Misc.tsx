import { useState } from "react";
import { Link } from "react-router-dom";
import { useEvents, useFleet } from "../lib/data";
import { LoadError, PageHead } from "../components/bits";

function when(ts: number) {
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Plain-language summary of an event; falls back to the raw type. */
function describe(type: string, data: Record<string, unknown> | null): string {
  const d = data ?? {};
  const title = typeof d.title === "string" ? `“${d.title}”` : "a task";
  const map: Record<string, string> = {
    "task.created": `Created ${title}`,
    "task.state": `${title} is now ${String(d.state ?? "").replace("_", " ")}`,
    "task.approved": `Approved ${title}`,
    "task.rejected": `Rejected ${title}`,
    "task.message": `Sent a follow-up to ${title}`,
    "task.cleaned_up": `Removed the worktree of ${title}`,
    "node.ceiling": `Set ${String(d.node ?? "a node")}'s highest mode to ${String(d.mode ?? "")}`,
  };
  return map[type] ?? type;
}

export function ActivityPage() {
  const events = useEvents();
  const [filter, setFilter] = useState("");
  const rows = [...(events.data ?? [])].reverse().filter((e) => !filter || e.type.includes(filter) || (e.task_id ?? "").includes(filter));
  return (
    <>
      <PageHead title="Activity" intro="Everything the controller recorded, newest first." />
      <LoadError error={events.error} />
      <div className="row" style={{ marginBottom: 10 }}>
        <label className="field" style={{ minWidth: 240 }}>Filter by event or task
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="task.state, a task id…" />
        </label>
      </div>
      <div className="panel">
        <table>
          <thead><tr><th>When</th><th>What happened</th><th className="hide-sm">Task</th></tr></thead>
          <tbody>
            {rows.slice(0, 500).map((e) => (
              <tr key={e.id}>
                <td className="small quiet" style={{ whiteSpace: "nowrap" }}>{when(e.ts)}</td>
                <td>{describe(e.type, e.data)}<div className="sub mono">{e.type}</div></td>
                <td className="hide-sm">{e.task_id && <Link className="mono small" to={`/tasks?tab=all&task=${e.task_id}`}>{e.task_id}</Link>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!events.isLoading && rows.length === 0 && <div className="empty">No activity recorded yet.</div>}
      </div>
    </>
  );
}

export function LaterPage({ title, what }: { title: string; what: string }) {
  return (
    <>
      <PageHead title={title} intro={what} />
      <div className="banner note">
        This part of the control plane is being built. Until then, the spec in <span className="mono">docs/SPEC-control-plane.md</span> describes how it will work.
      </div>
    </>
  );
}

export function SettingsPage() {
  const fleet = useFleet();
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("fleet-theme") ?? "system"; } catch { return "system"; }
  });
  function apply(t: string) {
    setTheme(t);
    try { localStorage.setItem("fleet-theme", t); } catch { /* private mode */ }
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
  }
  const versions = (fleet.data?.nodes ?? []).filter((n) => n.runner).map((n) => `${n.name}: runner ${n.runner?.version}`);
  return (
    <>
      <PageHead title="Settings" />
      <dl className="kv">
        <dt>Appearance</dt>
        <dd>
          <select value={theme} onChange={(e) => apply(e.target.value)} aria-label="Appearance">
            <option value="system">Match the system</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </select>
        </dd>
        <dt>Access</dt>
        <dd>Tokens are managed on the master with <span className="mono">controller.py token create|list|revoke</span>. Open <span className="mono">/login?token=…</span> once on a new device.</dd>
        <dt>Versions</dt>
        <dd>{versions.length ? versions.join(" · ") : "No runners reachable."}</dd>
        <dt>Classic view</dt>
        <dd><a href="/">The previous single-page dashboard</a> stays available until this console covers everything.</dd>
      </dl>
    </>
  );
}
