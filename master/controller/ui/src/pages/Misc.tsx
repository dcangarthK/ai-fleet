import { useState, type ChangeEvent, type ReactElement } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, get, post, put, type NotifyRule } from "../lib/api";
import { Link } from "react-router-dom";
import { newestFirst, useCan, useEvents, useFleet, useRun } from "../lib/data";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";
import { Button, Card, Empty, Panel, Select, Table, useConfirm } from "../components/ui";
import { describeRequest } from "../lib/request";
import { Terminal } from "../components/Terminal";
import { SettleCheck } from "../components/motion/MiniGraph";
import { useArrivals } from "../components/motion/Arrive";
import { timeGroup } from "../lib/pageMotion";
import type { FleetEvent } from "../lib/api";
import { extraEventText } from "../lib/decisions";

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
  return map[type] ?? extraEventText(type, data) ?? type;
}

/** Feed rows. Mounted only once events are loaded, so "arrived later" means a real new event (source: the events feed).
 *  Time group headers are sticky. */
function ActivityRows({ all, rows }: { all: FleetEvent[]; rows: FleetEvent[] }) {
  const isNew = useArrivals(all.map((e) => String(e.id)));
  const now = Math.floor(Date.now() / 1000);
  let group = "";
  const out: ReactElement[] = [];
  for (const e of rows.slice(0, 500)) {
    const g = timeGroup(e.ts, now);
    if (g !== group) {
      group = g;
      out.push(<tr key={`g-${g}`}><th colSpan={3} className="small quiet sticky-group" scope="colgroup" style={{ textAlign: "left" }}>{g}</th></tr>);
    }
    out.push(
      <tr key={e.id} className={isNew(String(e.id)) ? "arrive" : undefined}>
        <td className="small quiet" style={{ whiteSpace: "nowrap" }}>{when(e.ts)}</td>
        <td>{describe(e.type, e.data)}<div className="sub mono">{e.type}</div></td>
        <td className="hide-sm">{e.task_id && <Link className="mono small" to={`/tasks?tab=all&task=${e.task_id}`}>{e.task_id}</Link>}</td>
      </tr>,
    );
  }
  return <tbody>{out}</tbody>;
}

export function ActivityPage() {
  const events = useEvents();
  const [filter, setFilter] = useState("");
  const rows = newestFirst(events.data).filter((e) => !filter || e.type.includes(filter) || (e.task_id ?? "").includes(filter));
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
          {events.data ? <ActivityRows all={events.data} rows={rows} /> : <tbody />}
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
      <Card title="Account and appearance"><dl className="kv">
        <dt>Signed in as</dt><dd>{can.me ? `${can.me.label} (${can.me.scope} access)` : "checking…"}</dd>
        <dt>Appearance</dt>
        <dd>
          <Select value={theme} onChange={(e) => apply(e.target.value)} aria-label="Appearance">
            <option value="system">Match the system</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </Select>
        </dd>
        <dt>Access</dt>
        <dd>Tokens are managed on the master with <span className="mono">controller.py token create|list|revoke</span>. Open <span className="mono">/login?token=…</span> once on a new device.</dd>
        <dt>Versions</dt>
        <dd>{versions.length ? versions.join(" · ") : "No runners reachable."}</dd>
      </dl></Card>
      <Notifications admin={can.admin} />
      {can.admin && <Backup />}
    </>
  );
}

function Notifications({ admin }: { admin: boolean }) {
  const rules = useQuery({ queryKey: ["notify-rules"], queryFn: () => get<NotifyRule[]>("/api/notify-rules"), retry: 0 });
  const { run, busy, error, clearError } = useRun();
  const confirm = useConfirm();
  const [sent, setSent] = useState<string | null>(null);
  return (
    <Section title="Notifications" aside="Which events reach your notification plugins">
      <ErrorBanner error={error} onDismiss={clearError} />
      {sent && <div className="banner note settle-pop" role="status"><SettleCheck label="Sent" />{sent}</div>}
      <Panel>
        <Table
          caption="Notification rules" rows={rules.data ?? []} rowKey={(r) => r.id}
          columns={[
            { key: "when", header: "When", cell: (r) => <span className="mono small">{r.event_glob}</span> },
            { key: "plugin", header: "Send through", cell: (r) => r.plugin },
            { key: "on", header: "On", cell: (r) => (
              <input type="checkbox" aria-label={`Notify on ${r.event_glob}`} checked={r.enabled} disabled={!admin || busy}
                     onChange={async (e) => {
                       const path = `/api/notify-rules/${encodeURIComponent(r.id)}`;
                       const body = { enabled: e.target.checked };
                       const ok = await confirm({ title: `${body.enabled ? "Enable" : "Disable"} notifications for ${r.event_glob}?`, message: describeRequest("PUT", path, body), confirmLabel: "Save" });
                       if (ok) await run(() => put(path, body));
                     }} />
            ) },
            { key: "act", header: "Actions", label: "", cell: (r) => admin ? (
              <Button size="sm" variant="ghost" disabled={busy}
                      onClick={async () => { const path = `/api/notify-rules/${encodeURIComponent(r.id)}/test`; if (!(await confirm({ title: `Send a test through ${r.plugin}?`, message: describeRequest("POST", path), confirmLabel: "Send" }))) return; const ok = await run(() => post(path)); if (ok) setSent(`Sent a test through ${r.plugin}.`); }}>Send a test</Button>
            ) : null },
          ]}
        />
        {rules.isError && <Empty title="This controller doesn't report notification rules yet" />}
        {rules.isSuccess && !rules.data.length && <Empty title="No rules">Add a plugin in master/plugins to get notifications.</Empty>}
      </Panel>
    </Section>
  );
}

function Backup() {
  const { run, busy, error, clearError } = useRun();
  const confirm = useConfirm();
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
      {done && <div className="banner note settle-pop" role="status"><SettleCheck label="Restored" />{done}</div>}
      <div className="row">
        <Button disabled={busy} onClick={download}>Download configuration</Button>
        <label className="row" style={{ gap: 8 }}><span className="small">Restore from a file:</span><input type="file" accept="application/json,.json" onChange={choose} /></label>
      </div>
      {plan && (
        <Card title="This is what restoring would change" className="mt">
          <Terminal title="restore preview (dry run)" text={JSON.stringify(plan.diff, null, 2)} height={280} label="Restore preview" />
          <div className="row" style={{ marginTop: "var(--s3)" }}>
            <Button variant="primary" disabled={busy}
                    onClick={async () => { if (!(await confirm({ title: "Restore this configuration?", message: describeRequest("POST", "/api/import", { document: "uploaded file (see preview above)" }), confirmLabel: "Apply" }))) return; const ok = await run(() => api("/api/import", "POST", plan.doc)); if (ok) { setPlan(null); setDone("Restored. Changes were saved as new versions; nothing was deleted."); } }}>Apply these changes</Button>
            <Button variant="ghost" onClick={() => setPlan(null)}>Cancel</Button>
          </div>
        </Card>
      )}
    </Section>
  );
}
