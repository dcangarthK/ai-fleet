import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get, post, type Pipeline, type PipelineState } from "../lib/api";
import { useCan, useFleet, useRun } from "../lib/data";
import { ago } from "../lib/format";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";

const STATE: Record<PipelineState, { tone: string; label: string }> = {
  building: { tone: "accent", label: "Building" },
  reviewing: { tone: "accent", label: "In review" },
  resolving: { tone: "accent", label: "Fixing review issues" },
  ready: { tone: "ok", label: "Ready to merge" },
  failed: { tone: "bad", label: "Failed" },
  escalated: { tone: "warn", label: "Needs you" },
  cancelled: { tone: "quiet", label: "Cancelled" },
};

const reviewerLabel = (r: Pipeline["reviewers"][number]) => (r.kind === "role" ? `${r.role} model` : r.agent);

export function PipelinesPage() {
  const [params, setParams] = useSearchParams();
  const open = params.get("pipeline");
  const [creating, setCreating] = useState(false);
  const can = useCan();
  const list = useQuery({
    queryKey: ["pipelines"],
    queryFn: () => get<Pipeline[]>("/api/pipelines"),
    refetchInterval: (q) => ((q.state.data ?? []).some((p) => ["building", "reviewing", "resolving"].includes(p.state)) ? 4000 : 30_000),
  });
  const items = list.data ?? [];
  const current = items.find((p) => p.id === open);

  return (
    <>
      <PageHead
        title="Pipelines"
        intro="One feature through the team's process: a builder writes it in its own branch, the gate must pass, different models review the diff, review issues go back to the builder, and you merge when it's ready."
        actions={can.operate && <button className="primary" onClick={() => setCreating(true)}>New pipeline</button>}
      />
      <LoadError error={list.error} />
      <div className="panel">
        <table>
          <thead><tr><th>Pipeline</th><th>State</th><th className="hide-sm">Builder → reviewers</th><th>Round</th><th>Updated</th></tr></thead>
          <tbody>
            {items.map((p) => (
              <tr key={p.id} className={`click ${open === p.id ? "selected" : ""}`} onClick={() => setParams({ pipeline: p.id })}>
                <td><Link to={`?pipeline=${p.id}`} onClick={(e) => e.stopPropagation()}><b>{p.title}</b></Link><div className="sub mono">{p.id}</div></td>
                <td><span className={`state tone-${STATE[p.state].tone}`}>{STATE[p.state].label}</span></td>
                <td className="hide-sm small">{p.builder.agent} → {p.reviewers.map(reviewerLabel).join(", ")}</td>
                <td>{p.round} of {p.max_rounds}</td>
                <td className="small quiet">{ago(p.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!list.isLoading && items.length === 0 && <div className="empty">No pipelines yet. Start one to have the fleet build and review a change together.</div>}
      </div>
      {current && <PipelineDetail p={current} />}
      <NewPipeline open={creating} onClose={() => setCreating(false)} onCreated={(p) => { setCreating(false); setParams({ pipeline: p.id }); }} />
    </>
  );
}

function PipelineDetail({ p }: { p: Pipeline }) {
  const { run, busy, error, clearError } = useRun();
  const can = useCan();
  const active = can.operate && ["building", "reviewing", "resolving"].includes(p.state);
  return (
    <Section title={p.title} aside={`${STATE[p.state].label} · round ${p.round} of ${p.max_rounds}`}>
      <div className="panel" style={{ padding: 16 }}>
        <ErrorBanner error={error} onDismiss={clearError} />
        {p.state === "escalated" && <div className="banner note">The fleet couldn't settle this on its own: {p.history.at(-1)?.note ?? "review rounds ran out"}. Read the reviews below and decide.</div>}
        <dl className="kv">
          <dt>Builder</dt><dd>{p.builder.agent}{p.build_task && <> · <Link to={`/tasks?tab=all&task=${p.build_task}`}>open its task</Link></>}</dd>
          <dt>Repository</dt><dd className="mono">{p.repo ?? "—"}</dd>
          <dt>Gate</dt><dd className="mono">{p.gate ?? "none"}</dd>
        </dl>
        <h3 className="small" style={{ margin: "0 0 6px" }}>Reviews this round</h3>
        <table>
          <tbody>
            {p.reviewers.map((r, i) => {
              const rv = p.reviews[String(i)];
              return (
                <tr key={i}>
                  <td style={{ width: 160 }}>{reviewerLabel(r)}{rv?.task_id && <div className="sub"><Link to={`/tasks?tab=all&task=${rv.task_id}`}>task</Link></div>}</td>
                  <td>
                    {!rv || rv.status === "pending" ? <span className="quiet">waiting</span>
                      : rv.status === "abstained" ? <span className="tone-warn">No usable review (the reply wasn't valid review JSON)</span>
                      : <>
                          <span className={rv.verdict === "approve" ? "tone-ok" : "tone-warn"}>{rv.verdict === "approve" ? "Approved" : "Requested changes"}</span>
                          {!!rv.unsupported && <span className="small quiet"> · {rv.unsupported} claim{rv.unsupported > 1 ? "s" : ""} dropped: the quoted evidence isn't in the diff</span>}
                          {(rv.issues ?? []).map((x, j) => (
                            <div key={j} className="small" style={{ marginTop: 6 }}>
                              <span className={`tone-${x.severity === "high" ? "bad" : x.severity === "medium" ? "warn" : "quiet"}`}>{x.severity}</span>{" "}
                              {x.file && <span className="mono">{x.file}: </span>}{x.problem}
                              {x.evidence && <pre className="out" style={{ margin: "4px 0" }}>{x.evidence}</pre>}
                              {x.fix && <div className="quiet">Fix: {x.fix}</div>}
                            </div>
                          ))}
                        </>}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <details style={{ marginTop: 12 }}>
          <summary className="small">History</summary>
          <ul className="small">
            {p.history.map((h, i) => <li key={i}>Round {h.round}: {STATE[h.state]?.label ?? h.state} · {ago(h.at)}{h.issues != null && ` · ${h.issues} issues`}{h.unsupported ? ` · ${h.unsupported} unsupported` : ""}{h.note ? ` · ${h.note}` : ""}</li>)}
          </ul>
        </details>
        <details>
          <summary className="small">What the builder was asked</summary>
          <pre className="out">{p.spec}</pre>
        </details>
        {active && (
          <div className="row" style={{ marginTop: 10 }}>
            <button className="danger" disabled={busy} onClick={() => confirm("Cancel this pipeline? Its tasks are cancelled; branches are kept.") && run(() => post(`/api/pipelines/${p.id}/cancel`))}>Cancel pipeline</button>
          </div>
        )}
      </div>
    </Section>
  );
}

function NewPipeline({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (p: Pipeline) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const fleet = useFleet();
  const { run, busy, error, clearError } = useRun();
  const [title, setTitle] = useState("");
  const [spec, setSpec] = useState("");
  const [repo, setRepo] = useState("");
  const [gate, setGate] = useState("");
  const [builder, setBuilder] = useState("codex");
  const [reviewers, setReviewers] = useState<string[]>(["role:reviewer", "agent:claude"]);
  const [rounds, setRounds] = useState(3);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const options = [
    { v: "role:reviewer", label: "Local reviewer model (reviewer role)" },
    { v: "role:coder", label: "Local coder model (coder role)" },
    { v: "agent:claude", label: "Claude" },
    { v: "agent:codex", label: "Codex" },
  ].filter((o) => o.v !== `agent:${builder}`);
  const profiles = Array.from(new Set((fleet.data?.nodes ?? []).flatMap((n) => n.runner?.gate_profiles ?? [])));

  async function submit(e: FormEvent) {
    e.preventDefault();
    const body = {
      title, spec, repo: repo || null, gate: gate || null, mode: "auto-edit", max_rounds: rounds,
      builder: { agent: builder },
      reviewers: reviewers.filter((r) => r !== `agent:${builder}`).map((r) => {
        const [kind, name] = r.split(":");
        return kind === "role" ? { kind, role: name } : { kind, agent: name };
      }),
    };
    const p = await run(() => post<Pipeline>("/api/pipelines", body));
    if (p) onCreated(p);
  }

  return (
    <dialog ref={ref} className="modal" onClose={onClose} aria-labelledby="np-title">
      <form onSubmit={submit}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 id="np-title">New pipeline</h2>
          <button type="button" className="ghost" onClick={onClose}>Close</button>
        </div>
        <ErrorBanner error={error} onDismiss={clearError} />
        <label className="field">Title<input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={80} /></label>
        <label className="field">What to build, with acceptance criteria
          <textarea value={spec} onChange={(e) => setSpec(e.target.value)} required placeholder={"Add X so that Y.\nDone when: tests A and B pass, …"} />
        </label>
        <div className="grid-form">
          <label className="field">Repository on the node<input className="mono" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="~/code/project" required /></label>
          <label className="field">Gate
            <input list="np-gates" value={gate} onChange={(e) => setGate(e.target.value)} placeholder={profiles[0] ?? "pytest"} required />
            <datalist id="np-gates">{profiles.map((p) => <option key={p}>{p}</option>)}</datalist>
          </label>
          <label className="field">Builder
            <select value={builder} onChange={(e) => setBuilder(e.target.value)}><option value="codex">Codex</option><option value="claude">Claude</option></select>
          </label>
          <label className="field">Review rounds before asking you
            <input type="number" min={1} max={5} value={rounds} onChange={(e) => setRounds(Number(e.target.value))} />
          </label>
        </div>
        <fieldset style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="small" style={{ color: "var(--ink-2)", marginBottom: 6 }}>Reviewers (never the builder)</legend>
          {options.map((o) => (
            <label key={o.v} className="row small" style={{ gap: 6 }}>
              <input type="checkbox" checked={reviewers.includes(o.v)}
                     onChange={() => setReviewers((xs) => (xs.includes(o.v) ? xs.filter((x) => x !== o.v) : [...xs, o.v]))} />{o.label}
            </label>
          ))}
        </fieldset>
        <div className="row">
          <button className="primary" disabled={busy || !reviewers.filter((r) => r !== `agent:${builder}`).length}>Start pipeline</button>
          <span className="small quiet">Review issues must quote the diff; claims that don't are dropped.</span>
        </div>
      </form>
    </dialog>
  );
}
