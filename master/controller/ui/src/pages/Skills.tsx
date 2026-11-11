import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { del, get, post, put, type Scope, type Skill } from "../lib/api";
import { useCan, useFleet, useRun } from "../lib/data";
import { ago } from "../lib/format";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";

const AGENTS = ["claude", "codex", "ollama"] as const;
const BLANK = { id: "", description: "", agents: [...AGENTS] as Skill["agents"], body: "" };

export function SkillsPage() {
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => get<Skill[]>("/api/skills") });
  const [editing, setEditing] = useState<Skill | typeof BLANK | null>(null);
  const can = useCan();
  const list = skills.data ?? [];

  return (
    <>
      <PageHead
        title="Skills"
        intro="Reusable, versioned instructions. Bind a skill to the fleet, a node, a role or an agent, and every task there receives it with its instructions. Skills can't grant permissions; policy still decides."
        actions={can.admin && <button className="primary" onClick={() => setEditing({ ...BLANK })}>New skill</button>}
      />
      <LoadError error={skills.error} />
      <div className="panel">
        <table>
          <thead><tr><th>Skill</th><th className="hide-sm">For</th><th>Bound to</th><th className="hide-sm">Version</th><th /></tr></thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id}>
                <td><b className="mono">{s.id}</b><div className="sub">{s.description}</div></td>
                <td className="hide-sm small">{s.agents.join(", ")}</td>
                <td>{s.bindings.length ? s.bindings.map((b) => <span className="tag" key={b.scope + b.scope_id}>{b.scope === "fleet" ? "whole fleet" : `${b.scope}: ${b.scope_id}`}</span>) : <span className="quiet small">not bound</span>}</td>
                <td className="hide-sm small">v{s.version} · {ago(s.created_at)}<div className="sub">by {s.author}</div></td>
                <td><button onClick={() => setEditing(s)}>Open</button></td>
              </tr>
            ))}
          </tbody>
        </table>
        {!skills.isLoading && list.length === 0 && (
          <div className="empty">No skills yet. Create one, or add <span className="mono">skills/&lt;id&gt;/SKILL.md</span> to the repo and it's imported on start.</div>
        )}
      </div>
      {editing && <SkillEditor key={"id" in editing ? editing.id : "new"} skill={editing} onClose={() => setEditing(null)} />}
    </>
  );
}

function SkillEditor({ skill, onClose }: { skill: Skill | typeof BLANK; onClose: () => void }) {
  const isNew = !("version" in skill);
  const can = useCan();
  const fleet = useFleet();
  const { run, busy, error, clearError } = useRun();
  const [id, setId] = useState(skill.id);
  const [description, setDescription] = useState(skill.description);
  const [agents, setAgents] = useState<Skill["agents"]>(skill.agents);
  const [body, setBody] = useState(skill.body);
  const [saved, setSaved] = useState<string | null>(null);
  const history = useQuery({
    queryKey: ["skill-history", skill.id],
    queryFn: () => get<Skill[]>(`/api/skills/${encodeURIComponent(skill.id)}/history`),
    enabled: !isNew,
  });
  const [bindScope, setBindScope] = useState<Scope>("role");
  const [bindId, setBindId] = useState("");

  const targets: Record<Scope, { id: string; label: string }[]> = {
    fleet: [{ id: "*", label: "Whole fleet" }],
    node: (fleet.data?.nodes ?? []).filter((n) => n.status === "approved").map((n) => ({ id: n.id, label: n.name })),
    role: Array.from(new Set(["coder", "reviewer", "fast", ...(fleet.data?.assignments ?? []).map((a) => a.role)])).map((r) => ({ id: r, label: r })),
    agent: AGENTS.map((a) => ({ id: a, label: a })),
  };

  async function save(e: FormEvent) {
    e.preventDefault();
    const ok = await run(() => put(`/api/skills/${encodeURIComponent(id)}`, { description, agents, risk: "prompt-only", body }));
    if (ok) setSaved(isNew ? "Created version 1." : "Saved as a new version. Tasks already running keep the version they started with.");
  }
  const toggle = (a: (typeof AGENTS)[number]) => setAgents((xs) => (xs.includes(a) ? xs.filter((x) => x !== a) : [...xs, a]));
  const bindings = "bindings" in skill ? skill.bindings : [];

  return (
    <Section title={isNew ? "New skill" : `Skill: ${skill.id}`} aside={!isNew && "version" in skill ? `version ${skill.version}` : undefined}>
      <form className="panel" style={{ padding: 16 }} onSubmit={save}>
        <ErrorBanner error={error} onDismiss={clearError} />
        {saved && <div className="banner note" role="status">{saved}</div>}
        <div className="grid-form">
          <label className="field">Id
            <input className="mono" value={id} onChange={(e) => setId(e.target.value)} disabled={!isNew} required pattern="[a-z][a-z0-9-]{1,40}" title="Lowercase letters, digits and dashes" />
          </label>
          <label className="field" style={{ gridColumn: "span 2" }}>What it's for
            <input value={description} onChange={(e) => setDescription(e.target.value)} required maxLength={200} />
          </label>
        </div>
        <fieldset className="row" style={{ border: 0, padding: 0, margin: "12px 0" }}>
          <legend className="small" style={{ color: "var(--ink-2)", marginBottom: 4 }}>Agents that receive it</legend>
          {AGENTS.map((a) => (
            <label key={a} className="row small" style={{ gap: 6, marginRight: 14 }}>
              <input type="checkbox" checked={agents.includes(a)} onChange={() => toggle(a)} />{a}
            </label>
          ))}
        </fieldset>
        <label className="field">Instructions (Markdown)
          <textarea className="mono" style={{ minHeight: 240 }} value={body} onChange={(e) => setBody(e.target.value)} required maxLength={24576} />
        </label>
        <div className="row" style={{ marginTop: 12 }}>
          {can.admin && <button className="primary" disabled={busy || !agents.length}>{isNew ? "Create skill" : "Save new version"}</button>}
          <button type="button" className="ghost" onClick={onClose}>Close</button>
        </div>

        {!isNew && (
          <>
            <h3 style={{ fontSize: 14, margin: "20px 0 6px" }}>Where it applies</h3>
            <div style={{ marginBottom: 8 }}>
              {bindings.length === 0 && <span className="quiet small">Not bound anywhere yet, so no task receives it.</span>}
              {bindings.map((b) => (
                <span key={b.scope + b.scope_id} className="tag">
                  {b.scope === "fleet" ? "whole fleet" : `${b.scope}: ${b.scope_id}`}{" "}
                  {can.admin && <button type="button" className="ghost small" aria-label={`Unbind from ${b.scope} ${b.scope_id}`} disabled={busy}
                          onClick={() => run(() => del(`/api/skills/${encodeURIComponent(skill.id)}/bindings`, b))}>remove</button>}
                </span>
              ))}
            </div>
            {can.admin && <div className="row">
              <select aria-label="Bind to" value={bindScope} onChange={(e) => { setBindScope(e.target.value as Scope); setBindId(""); }}>
                <option value="fleet">Whole fleet</option><option value="node">Node</option><option value="role">Role</option><option value="agent">Agent</option>
              </select>
              {bindScope !== "fleet" && (
                <select aria-label="Which one" value={bindId} onChange={(e) => setBindId(e.target.value)}>
                  <option value="" disabled>Choose…</option>
                  {targets[bindScope].map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
                </select>
              )}
              <button type="button" disabled={busy || (bindScope !== "fleet" && !bindId)}
                      onClick={() => run(() => post(`/api/skills/${encodeURIComponent(skill.id)}/bindings`, { scope: bindScope, scope_id: bindScope === "fleet" ? "*" : bindId }))}>
                Bind
              </button>
            </div>}
            {(history.data ?? []).length > 1 && (
              <details style={{ marginTop: 16 }}>
                <summary className="small">Version history ({history.data?.length})</summary>
                <table><tbody>
                  {(history.data ?? []).map((h) => (
                    <tr key={h.version}><td>v{h.version}</td><td className="small quiet">{ago(h.created_at)} by {h.author}</td>
                      <td className="mono small">{h.digest.slice(0, 12)}</td>
                      <td><button type="button" className="ghost small" onClick={() => { setBody(h.body); setDescription(h.description); setAgents(h.agents); setSaved(`Loaded v${h.version} into the editor. Save to make it current.`); }}>Load into editor</button></td></tr>
                  ))}
                </tbody></table>
              </details>
            )}
          </>
        )}
      </form>
    </Section>
  );
}
