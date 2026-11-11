import { useState, type ChangeEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, get, post, put, type NotifyRule } from "../lib/api";
import { Link } from "react-router-dom";
import { useCan, useEvents, useFleet, useRun } from "../lib/data";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";

function when(ts: number) {
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Plain-language summary of an event; falls back to the raw type. */
function describe(type: string, data: Record<string, unknown> | null): string {
  const d = data ?? {};
  const title = typeof d.title === "string" ? `“${d.title}”` : "a task";
  const node = typeof d.node === "string" ? ` on ${d.node}` : "";
  const map: Record<string, string> = {
    "task.queued": `Queued ${title}`,
    "task.awaiting_approval": `${title} is waiting for approval`,
    "task.starting": `Starting ${title}${node}`,
    "task.working": `${title} is working${node}`,
    "task.blocked": `${title} was blocked by a denied permission`,
    "task.review": `${title} is ready for review`,
    "task.failed": `${title} failed`,
    "task.interrupted": `${title} was interrupted`,
    "task.done": `${title} is done`,
    "task.cancelled": `${title} was cancelled`,
    "task.approved": `Approved ${title}`,
    "task.unreachable": `Couldn't reach the runner for ${title}`,
    "task.cleaned_up": `Removed the worktree of ${title}`,
    "node.ceiling": `Set ${String(d.node ?? "a node")}'s highest mode to ${String(d.mode ?? "")}`,
    "model.state": `${String(d.model ?? "A model")} on ${String(d.node ?? "a node")}: ${String(d.state ?? "")}`,
    "model.install": `Install of ${String(d.model ?? "a model")} on ${String(d.node ?? "a node")} requested`,
    "model.remove": `Removal of ${String(d.model ?? "a model")} from ${String(d.node ?? "a node")} requested`,
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
  const can = useCan();
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem("fleet-theme") ?? "system"; } catch { return "system"; }
  });
  function apply(t: string) {
    setTheme(t);
    try { localStorage.setItem("fleet-theme", t); } catch { /* private mode */ }
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
  }
  const versions = (fleet.data?.nodes ?? []).filter((n) => n.runner).map((n) => `${n.name}: runner ${n.runner?.version}${n.update_available ? " (update available)" : ""}`);
  return (
    <>
      <PageHead title="Settings" />
      <dl className="kv">
        <dt>Signed in as</dt><dd>{can.me ? `${can.me.label} (${can.me.scope} access)` : "checking…"}</dd>
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
      </dl>
      <Notifications admin={can.admin} />
      {can.admin && <Backup />}
    </>
  );
}

function Notifications({ admin }: { admin: boolean }) {
  const rules = useQuery({ queryKey: ["notify-rules"], queryFn: () => get<NotifyRule[]>("/api/notify-rules"), retry: 0 });
  const { run, busy, error, clearError } = useRun();
  const [sent, setSent] = useState<string | null>(null);
  return (
    <Section title="Notifications" aside="Which events reach your notification plugins">
      <ErrorBanner error={error} onDismiss={clearError} />
      {sent && <div className="banner note" role="status">{sent}</div>}
      <div className="panel">
        <table>
          <thead><tr><th>When</th><th>Send through</th><th>On</th><th /></tr></thead>
          <tbody>
            {(rules.data ?? []).map((r) => (
              <tr key={r.id}>
                <td className="mono small">{r.event_glob}</td><td>{r.plugin}</td>
                <td><input type="checkbox" aria-label={`Notify on ${r.event_glob}`} checked={r.enabled} disabled={!admin || busy}
                           onChange={(e) => run(() => put(`/api/notify-rules/${encodeURIComponent(r.id)}`, { enabled: e.target.checked }))} /></td>
                <td>{admin && <button type="button" className="ghost small" disabled={busy}
                                      onClick={async () => { const ok = await run(() => post(`/api/notify-rules/${encodeURIComponent(r.id)}/test`)); if (ok) setSent(`Sent a test through ${r.plugin}.`); }}>Send a test</button>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rules.isError && <div className="empty">This controller doesn't report notification rules yet.</div>}
        {rules.isSuccess && !rules.data.length && <div className="empty">No rules. Add a plugin in master/plugins to get notifications.</div>}
      </div>
    </Section>
  );
}

function Backup() {
  const { run, busy, error, clearError } = useRun();
  const [plan, setPlan] = useState<{ doc: unknown; diff: unknown } | null>(null);
  const [done, setDone] = useState<string | null>(null);

  async function download() {
    const doc = await run(() => get<unknown>("/api/export"));
    if (!doc) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `ai-fleet-config-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function choose(e: ChangeEvent<HTMLInputElement>) {
    setDone(null);
    const file = e.target.files?.[0];
    if (!file) return;
    let doc: unknown;
    try { doc = JSON.parse(await file.text()); } catch { return run(async () => { throw new Error("That file isn't JSON exported from AI Fleet."); }); }
    const diff = await run(() => api<unknown>("/api/import?dry_run=1", "POST", doc));
    if (diff) setPlan({ doc, diff });
  }

  return (
    <Section title="Backup and restore" aside="Catalog, roles, policies, skills and rules; never tokens">
      <ErrorBanner error={error} onDismiss={clearError} />
      {done && <div className="banner note" role="status">{done}</div>}
      <div className="row">
        <button type="button" disabled={busy} onClick={download}>Download configuration</button>
        <label className="row" style={{ gap: 8 }}><span className="small">Restore from a file:</span><input type="file" accept="application/json,.json" onChange={choose} /></label>
      </div>
      {plan && (
        <div className="panel" style={{ padding: 14, marginTop: 12 }}>
          <b>This is what restoring would change</b>
          <pre className="out">{JSON.stringify(plan.diff, null, 2)}</pre>
          <div className="row">
            <button type="button" className="primary" disabled={busy}
                    onClick={async () => { const ok = await run(() => api("/api/import", "POST", plan.doc)); if (ok) { setPlan(null); setDone("Restored. Changes were saved as new versions; nothing was deleted."); } }}>Apply these changes</button>
            <button type="button" className="ghost" onClick={() => setPlan(null)}>Cancel</button>
          </div>
        </div>
      )}
    </Section>
  );
}
