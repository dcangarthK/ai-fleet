import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { get, post, put, type Effective, type InstructionSet, type Mode, type PolicyBody, type PolicyDoc, type Scope } from "../lib/api";
import { useCan, useFleet, useRun } from "../lib/data";
import { ago } from "../lib/format";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";

const AGENTS = ["claude", "codex", "ollama"];
const ROLES = ["coder", "reviewer", "fast"];

interface Layer { scope: Scope; id: string; label: string; group: string }

const lines = (v: string) => v.split("\n").map((x) => x.trim()).filter(Boolean);
const joinLines = (v: string[] | undefined) => (v ?? []).join("\n");
const numOrUndef = (v: string) => (v.trim() === "" ? undefined : Number(v));

/** Drop empty fields so a blank form saves as "no opinion" rather than "allow nothing". */
export function compact(body: PolicyBody): PolicyBody {
  const out: PolicyBody = {};
  const fs = { allowed_roots: body.filesystem?.allowed_roots, denied: body.filesystem?.denied };
  if (fs.allowed_roots?.length || fs.denied?.length) {
    out.filesystem = {};
    if (fs.allowed_roots?.length) out.filesystem.allowed_roots = fs.allowed_roots;
    if (fs.denied?.length) out.filesystem.denied = fs.denied;
  }
  if (body.tools?.claude_allow?.length || body.tools?.claude_deny?.length) {
    out.tools = {};
    if (body.tools.claude_allow?.length) out.tools.claude_allow = body.tools.claude_allow;
    if (body.tools.claude_deny?.length) out.tools.claude_deny = body.tools.claude_deny;
  }
  if (body.network?.agents) out.network = { agents: body.network.agents };
  if (body.mode_ceiling) out.mode_ceiling = body.mode_ceiling;
  const l = body.limits ?? {};
  const limits = Object.fromEntries(Object.entries(l).filter(([, v]) => v !== undefined && !(Array.isArray(v) && !v.length) && !Number.isNaN(v)));
  if (Object.keys(limits).length) out.limits = limits;
  const a = body.auto_approve;
  if (a && (a.agents?.length || a.full_mode !== undefined || a.denied_tools !== undefined)) out.auto_approve = a;
  return out;
}

export function PoliciesPage() {
  const fleet = useFleet();
  const instructions = useQuery({ queryKey: ["instructions"], queryFn: () => get<InstructionSet[]>("/api/instructions") });
  const policies = useQuery({ queryKey: ["policies"], queryFn: () => get<PolicyDoc[]>("/api/policies") });

  const roles = Array.from(new Set([...ROLES, ...(fleet.data?.assignments ?? []).map((a) => a.role)]));
  const layers: Layer[] = [
    { scope: "fleet", id: "*", label: "Whole fleet", group: "Fleet" },
    ...(fleet.data?.nodes ?? []).filter((n) => n.status !== "pending").map((n) => ({ scope: "node" as Scope, id: n.id, label: n.name, group: "Nodes" })),
    ...roles.map((r) => ({ scope: "role" as Scope, id: r, label: r, group: "Roles" })),
    ...AGENTS.map((a) => ({ scope: "agent" as Scope, id: a, label: a, group: "Agents" })),
  ];
  const [sel, setSel] = useState<Layer>(layers[0]);
  const has = (l: Layer) =>
    (instructions.data ?? []).some((x) => x.scope === l.scope && x.scope_id === l.id && x.text.trim()) ||
    (policies.data ?? []).some((x) => x.scope === l.scope && x.scope_id === l.id && Object.keys(x.body).length);

  return (
    <>
      <PageHead
        title="Policies"
        intro="Instructions tell agents how to work; policy decides what they can do. Layers apply in order (fleet, node, role, agent, task): instructions add up, limits only get stricter, and each node's own settings always cap what the master asks for."
      />
      <LoadError error={instructions.error ?? policies.error ?? fleet.error} />
      <div className="split">
        <nav className="panel layers" aria-label="Policy layers">
          {["Fleet", "Nodes", "Roles", "Agents"].map((g) => (
            <div key={g}>
              <div className="nav-group">{g}</div>
              {layers.filter((l) => l.group === g).map((l) => (
                <button key={l.scope + l.id} className={`layer ${sel.scope === l.scope && sel.id === l.id ? "on" : ""}`}
                        aria-current={sel.scope === l.scope && sel.id === l.id ? "true" : undefined} onClick={() => setSel(l)}>
                  <span>{l.label}</span>{has(l) && <span className="dot-set" aria-label="has settings" />}
                </button>
              ))}
            </div>
          ))}
        </nav>
        <div style={{ minWidth: 0 }}>
          {instructions.isSuccess && policies.isSuccess ? (() => {
            const current = instructions.data.find((x) => x.scope === sel.scope && x.scope_id === sel.id);
            const policy = policies.data.find((x) => x.scope === sel.scope && x.scope_id === sel.id);
            return <LayerEditor key={`${sel.scope}/${sel.id}/${current?.version ?? 0}/${policy?.version ?? 0}`} layer={sel} current={current} policy={policy} />;
          })() : !(instructions.error || policies.error) && <p className="quiet">Loading this layer…</p>}
          <Simulator roles={roles} />
        </div>
      </div>
    </>
  );
}

function LayerEditor({ layer, current, policy }: { layer: Layer; current?: InstructionSet; policy?: PolicyDoc }) {
  const { run, busy, error, clearError } = useRun();
  const can = useCan();
  const fleet = useFleet();
  const [text, setText] = useState(current?.text ?? "");
  const [body, setBody] = useState<PolicyBody>(policy?.body ?? {});
  const [json, setJson] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [saved, setSaved] = useState<string | null>(null);
  const path = `${layer.scope}/${encodeURIComponent(layer.id)}`;
  const history = useQuery({
    queryKey: ["policy-history", path],
    queryFn: async () => ({
      instructions: await get<InstructionSet[]>(`/api/instructions/${path}/history`),
      policies: await get<PolicyDoc[]>(`/api/policies/${path}/history`),
    }),
  });

  useEffect(() => setJsonText(JSON.stringify(compact(body), null, 2)), [json]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (f: (b: PolicyBody) => PolicyBody) => setBody((b) => f(structuredClone(b)));

  async function save(e: FormEvent) {
    e.preventDefault();
    setSaved(null);
    let next = body;
    if (json) {
      try { next = JSON.parse(jsonText); } catch { return run(async () => { throw new Error("The JSON doesn't parse. Fix it or switch back to the form."); }); }
    }
    const outgoing = json ? next : compact(next);
    const changedText = text !== (current?.text ?? "");
    const changedPolicy = JSON.stringify(outgoing) !== JSON.stringify(policy?.body ?? {});
    const ok = await run(async () => {
      if (changedText) await put(`/api/instructions/${path}`, { text });
      if (changedPolicy) await put(`/api/policies/${path}`, { body: outgoing });
      return true;
    });
    if (ok) { setBody(outgoing); setSaved(changedText || changedPolicy ? "Saved as a new version." : "Nothing changed."); history.refetch(); }
  }

  const modes = fleet.data?.modes ?? ["read-only", "supervised", "auto-edit", "full"];
  const L = body.limits ?? {};

  return (
    <form className="panel" style={{ padding: 16 }} onSubmit={save}>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <h2 style={{ margin: 0, fontSize: 17 }}>{layer.group === "Fleet" ? "Whole fleet" : `${layer.group.slice(0, -1)}: ${layer.label}`}</h2>
        <span className="small quiet">
          {current || policy ? `Version ${Math.max(current?.version ?? 0, policy?.version ?? 0)} · ${ago(Math.max(current?.created_at ?? 0, policy?.created_at ?? 0))} by ${(policy ?? current)?.author}` : "Nothing set at this layer yet"}
        </span>
      </div>
      <ErrorBanner error={error} onDismiss={clearError} />
      {saved && <div className="banner note" role="status">{saved}</div>}

      <label className="field">Instructions
        <textarea value={text} onChange={(e) => setText(e.target.value)} maxLength={16384}
                  placeholder={"For example:\nFollow the repo's CLAUDE.md. Say \"I don't know\" instead of guessing. Never touch production config."} />
      </label>

      <div className="row" style={{ justifyContent: "space-between", margin: "18px 0 8px" }}>
        <h3 style={{ margin: 0, fontSize: 14 }}>Limits and permissions</h3>
        <button type="button" className="ghost small" onClick={() => setJson((j) => !j)}>{json ? "Edit as form" : "Edit as JSON"}</button>
      </div>

      {json ? (
        <>
        <p className="small quiet" style={{ margin: "0 0 6px" }}>Saved exactly as written. An empty list here means "allow nothing", unlike an empty field in the form.</p>
        <textarea className="mono" style={{ minHeight: 260 }} value={jsonText} onChange={(e) => setJsonText(e.target.value)} aria-label="Policy as JSON" />
        </>
      ) : (
        <div className="stack">
          <div className="grid-form">
            <label className="field">Folders agents may work in (one per line; empty means no limit at this layer)
              <textarea className="mono" style={{ minHeight: 70 }} value={joinLines(body.filesystem?.allowed_roots)} placeholder="~/code"
                        onChange={(e) => set((b) => ({ ...b, filesystem: { ...b.filesystem, allowed_roots: lines(e.target.value) } }))} />
            </label>
            <label className="field">Folders that are always off limits
              <textarea className="mono" style={{ minHeight: 70 }} value={joinLines(body.filesystem?.denied)} placeholder="~/code/client-secrets"
                        onChange={(e) => set((b) => ({ ...b, filesystem: { ...b.filesystem, denied: lines(e.target.value) } }))} />
            </label>
          </div>
          <div className="grid-form">
            <label className="field">Claude tools pre-approved (empty means no list at this layer; use "denied" to block)
              <textarea className="mono" style={{ minHeight: 60 }} value={joinLines(body.tools?.claude_allow)} placeholder={"Read\nEdit\nBash(npm test:*)"}
                        onChange={(e) => set((b) => ({ ...b, tools: { ...b.tools, claude_allow: lines(e.target.value) } }))} />
            </label>
            <label className="field">Claude tools blocked
              <textarea className="mono" style={{ minHeight: 60 }} value={joinLines(body.tools?.claude_deny)} placeholder="WebFetch"
                        onChange={(e) => set((b) => ({ ...b, tools: { ...b.tools, claude_deny: lines(e.target.value) } }))} />
            </label>
          </div>
          <div className="grid-form">
            <label className="field">Agents' internet access
              <select value={body.network?.agents ?? ""} onChange={(e) => set((b) => ({ ...b, network: e.target.value ? { agents: e.target.value as "allow" | "deny" } : undefined }))}>
                <option value="">No opinion at this layer</option>
                <option value="deny">Blocked</option>
                <option value="allow">Allowed (if the node also allows it)</option>
              </select>
            </label>
            <label className="field">Highest permission mode
              <select value={body.mode_ceiling ?? ""} onChange={(e) => set((b) => ({ ...b, mode_ceiling: (e.target.value || undefined) as Mode | undefined }))}>
                <option value="">No opinion at this layer</option>
                {modes.map((m) => <option key={m}>{m}</option>)}
              </select>
            </label>
            <label className="field">Max minutes per task run
              <input type="number" min={1} value={L.task_minutes ?? ""} onChange={(e) => set((b) => ({ ...b, limits: { ...b.limits, task_minutes: numOrUndef(e.target.value) } }))} />
            </label>
            <label className="field">Tasks at once per node
              <input type="number" min={1} value={L.max_concurrent ?? ""} onChange={(e) => set((b) => ({ ...b, limits: { ...b.limits, max_concurrent: numOrUndef(e.target.value) } }))} />
            </label>
            <label className="field">Agent minutes per node per day
              <input type="number" min={1} value={L.agent_minutes_per_day ?? ""} onChange={(e) => set((b) => ({ ...b, limits: { ...b.limits, agent_minutes_per_day: numOrUndef(e.target.value) } }))} />
            </label>
            <label className="field">Quiet hours (no new tasks start)
              <input className="mono" value={(L.quiet_hours ?? []).join(", ")} placeholder="23:00-07:00"
                     onChange={(e) => set((b) => ({ ...b, limits: { ...b.limits, quiet_hours: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) } }))} />
            </label>
          </div>
          <fieldset className="panel" style={{ padding: 12, margin: 0 }}>
            <legend className="small" style={{ padding: "0 6px" }}>Approvals for Claude and Codex</legend>
            <div className="row" style={{ gap: 16 }}>
              {(["claude", "codex"] as const).map((agent) => (
                <label key={agent} className="row small" style={{ gap: 6 }}>
                  <input type="checkbox" checked={(body.auto_approve?.agents ?? []).includes(agent)}
                         onChange={(e) => set((b) => {
                           const agents = new Set(b.auto_approve?.agents ?? []);
                           if (e.target.checked) agents.add(agent); else agents.delete(agent);
                           return { ...b, auto_approve: { ...b.auto_approve, agents: [...agents] } };
                         })} />
                  Trust {agent === "claude" ? "Claude" : "Codex"}
                </label>
              ))}
            </div>
            <label className="row small" style={{ gap: 6, marginTop: 8 }}>
              <input type="checkbox" checked={!!body.auto_approve?.denied_tools}
                     onChange={(e) => set((b) => ({ ...b, auto_approve: { ...b.auto_approve, denied_tools: e.target.checked } }))} />
              When a trusted agent is denied a tool (like Bash), approve it automatically (up to 3 times per task)
            </label>
            <label className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={!!body.auto_approve?.full_mode}
                     onChange={(e) => set((b) => ({ ...b, auto_approve: { ...b.auto_approve, full_mode: e.target.checked } }))} />
              Let trusted agents start in full mode (no permission prompts) without asking you. The node must also allow full mode.
            </label>
          </fieldset>
        </div>
      )}

      <div className="row" style={{ marginTop: 16 }}>
        {can.admin ? <button className="primary" disabled={busy}>Save new version</button> : <span className="small">Read only: changing policy needs an admin token.</span>}
        <span className="small quiet">Running tasks keep the version they started with.</span>
      </div>

      <History data={history.data} current={{ text, body }} onRestore={(t, b) => {
        setText(t ?? text);
        if (b) { setBody(b); setJsonText(JSON.stringify(b, null, 2)); }
        setSaved("Restored into the editor. Save to make it the current version.");
      }} />
    </form>
  );
}

function History({ data, current, onRestore }: {
  data?: { instructions: InstructionSet[]; policies: PolicyDoc[] };
  current: { text: string; body: PolicyBody };
  onRestore: (t?: string, b?: PolicyBody) => void;
}) {
  const rows = useMemo(() => [
    ...(data?.instructions ?? []).map((x) => ({ kind: "Instructions", v: x.version, at: x.created_at, by: x.author, content: x.text,
      same: x.text === current.text, restore: () => onRestore(x.text) })),
    ...(data?.policies ?? []).map((x) => ({ kind: "Policy", v: x.version, at: x.created_at, by: x.author, content: JSON.stringify(x.body, null, 2),
      same: JSON.stringify(x.body) === JSON.stringify(current.body), restore: () => onRestore(undefined, x.body) })),
  ].sort((a, b) => b.at - a.at), [data, current, onRestore]);
  if (!rows.length) return null;
  return (
    <details style={{ marginTop: 16 }}>
      <summary className="small">Version history ({rows.length})</summary>
      {rows.map((r) => (
        <details key={r.kind + r.v} style={{ margin: "6px 0 0 12px" }}>
          <summary className="small">{r.kind} v{r.v} · {ago(r.at)} by {r.by}{r.same ? " · same as the editor" : ""}</summary>
          <pre className="out">{r.content || "(empty)"}</pre>
          {!r.same && <button type="button" className="ghost small" onClick={r.restore}>Restore into editor</button>}
        </details>
      ))}
    </details>
  );
}

function Simulator({ roles }: { roles: string[] }) {
  const fleet = useFleet();
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved");
  const [node, setNode] = useState("");
  const [role, setRole] = useState("");
  const [agent, setAgent] = useState("claude");
  const { run, busy, error, clearError } = useRun();
  const [result, setResult] = useState<Effective | null>(null);

  async function simulate(e: FormEvent) {
    e.preventDefault();
    const r = await run(() => post<Effective>("/api/policies/preview", { node, role: role || undefined, agent }));
    if (r) setResult(r);
  }

  const src = (k: string) => {
    const s = result?.sources[k];
    return s ? <span className="quiet small"> · from {Array.isArray(s) ? s.join(", ") : s}</span> : null;
  };
  const p = result?.policy;

  return (
    <Section title="What would a task get?" aside="Combine the layers for a node, role and agent">
      <form className="panel" style={{ padding: 14 }} onSubmit={simulate}>
        <ErrorBanner error={error} onDismiss={clearError} />
        <div className="grid-form">
          <label className="field">Node
            <select value={node} onChange={(e) => setNode(e.target.value)} required>
              <option value="" disabled>Choose a node</option>
              {nodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
            </select>
          </label>
          <label className="field">Role
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="">No role</option>
              {roles.map((r) => <option key={r}>{r}</option>)}
            </select>
          </label>
          <label className="field">Agent
            <select value={agent} onChange={(e) => setAgent(e.target.value)}>
              {AGENTS.map((a) => <option key={a}>{a}</option>)}
            </select>
          </label>
        </div>
        <div className="row" style={{ marginTop: 12 }}><button className="primary" disabled={busy || !node}>Show the result</button></div>
        {result && (
          <div style={{ marginTop: 14 }}>
            <dl className="kv">
              <dt>Folders</dt><dd>{p?.filesystem?.allowed_roots ? (p.filesystem.allowed_roots.length ? p.filesystem.allowed_roots.join(", ") : <span className="tone-bad">none: the layers' folder lists don't overlap</span>) : "node's own list"}{src("filesystem.allowed_roots")}</dd>
              <dt>Off limits</dt><dd>{p?.filesystem?.denied?.join(", ") || "node's built-in list only"}{src("filesystem.denied")}</dd>
              <dt>Internet</dt><dd>{p?.network?.agents === "allow" ? "allowed if the node allows it" : p?.network?.agents === "deny" ? "blocked" : "no policy: Codex has no network by default; Claude's web tools aren't blocked"}{src("network.agents")}</dd>
              <dt>Highest mode</dt><dd>{p?.mode_ceiling ?? "node ceiling"}{src("mode_ceiling")}</dd>
              <dt>Claude tools</dt><dd>{p?.tools?.claude_allow ? `pre-approved: ${p.tools.claude_allow.join(", ")}` : "no pre-approved list"}{p?.tools?.claude_deny?.length ? `; blocked: ${p.tools.claude_deny.join(", ")}` : ""}{src("tools.claude_deny")}</dd>
              <dt>Task time</dt><dd>{p?.limits?.task_minutes ? `${p.limits.task_minutes} min` : "node limit"}{src("limits.task_minutes")}</dd>
              <dt>At once</dt><dd>{p?.limits?.max_concurrent ?? "node limit"}{src("limits.max_concurrent")}</dd>
              <dt>Daily budget</dt><dd>{p?.limits?.agent_minutes_per_day ? `${p.limits.agent_minutes_per_day} min` : "none"}{src("limits.agent_minutes_per_day")}</dd>
              <dt>Quiet hours</dt><dd>{p?.limits?.quiet_hours?.join(", ") || "none"}{src("limits.quiet_hours")}</dd>
              <dt>Fingerprint</dt><dd className="mono small">{result.hash.slice(0, 16)}</dd>
            </dl>
            <h3 className="small" style={{ margin: "0 0 4px" }}>Instructions the agent receives</h3>
            <pre className="out">{result.instructions || "None."}</pre>
          </div>
        )}
      </form>
    </Section>
  );
}
