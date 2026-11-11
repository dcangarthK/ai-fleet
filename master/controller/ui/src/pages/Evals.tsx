import { useState, type FormEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { get, post, type EvalRun, type LeaderRow, type Recommendation } from "../lib/api";
import { useCan, useFleet, useModels, useRun } from "../lib/data";
import { ago, duration, isTrustedModel, pctText, TRUSTED_RULE } from "../lib/format";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";

const ROLES = ["coder", "reviewer", "fast"];
const RUN_TONE: Record<EvalRun["state"], string> = { queued: "quiet", running: "accent", done: "ok", failed: "bad" };

export function EvalsPage() {
  const fleet = useFleet();
  const evals = useQuery({
    queryKey: ["evals"],
    queryFn: () => get<{ runs: EvalRun[]; leaderboard: LeaderRow[] }>("/api/evals"),
    refetchInterval: (q) => ((q.state.data?.runs ?? []).some((r) => r.state === "running" || r.state === "queued") ? 3000 : 30_000),
  });
  const recs = useQuery({ queryKey: ["eval-recs"], queryFn: () => get<Recommendation[]>("/api/evals/recommendations") });
  const { run, busy, error, clearError } = useRun();
  const can = useCan();
  const [model, setModel] = useState("");
  const [node, setNode] = useState("");
  const [sortRole, setSortRole] = useState("reviewer");
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved" && n.models.length);
  const nodeModels = nodes.find((n) => n.id === node)?.models ?? [];
  const board = [...(evals.data?.leaderboard ?? [])].sort((a, b) => (b.roles[sortRole]?.score ?? -1) - (a.roles[sortRole]?.score ?? -1));
  const catalog = useModels().data ?? [];
  const grounded = board.filter((x) => x.roles.grounded);
  const realReview = board.filter((x) => x.roles.reviewer_real);
  const best = Object.fromEntries(ROLES.map((r) => [r, Math.max(...board.filter((x) => !x.errors?.length).map((x) => x.roles[r]?.score ?? 0), 0)]));

  async function start(e: FormEvent) {
    e.preventDefault();
    await run(() => post("/api/evals", { model, node }));
  }

  return (
    <>
      <PageHead
        title="Evals"
        intro="Scores from running the fleet's own test cases on your hardware: coder cases run hidden unit tests in a sandbox, reviewer cases check whether the model finds planted bugs without false alarms, fast cases check short, exact answers."
      />
      <LoadError error={evals.error} />
      <ErrorBanner error={error} onDismiss={clearError} />

      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              {ROLES.map((r) => (
                <th key={r} className="num">
                  <button className="ghost small" aria-pressed={sortRole === r} onClick={() => setSortRole(r)} style={{ fontWeight: sortRole === r ? 600 : 400 }}>{r}</button>
                </th>
              ))}
              <th className="num hide-sm">Size</th><th className="num hide-sm">Speed</th><th className="hide-sm">16 GB laptop</th>
            </tr>
          </thead>
          <tbody>
            {board.map((x) => (
              <tr key={x.model}>
                <td className="mono">{x.model}{x.errors?.length ? <div className="sub tone-warn">incomplete: {x.errors.length} case{x.errors.length > 1 ? "s" : ""} errored</div> : null}</td>
                {ROLES.map((r) => {
                  const s = x.roles[r]?.score;
                  return <td key={r} className="num">{s == null ? <span className="quiet">—</span> : <b className={s === best[r] && !x.errors?.length ? "tone-ok" : s < 60 ? "tone-bad" : ""}>{Math.round(s)}</b>}</td>;
                })}
                <td className="num hide-sm">{x.size_gb ? `${x.size_gb.toFixed(1)} GB` : "—"}</td>
                <td className="num hide-sm">{x.tokens_per_s ? `${Math.round(x.tokens_per_s)} tok/s` : "—"}</td>
                <td className="hide-sm">{x.fits_16gb == null ? "—" : x.fits_16gb ? <span className="tone-ok">fits</span> : <span className="quiet">too big</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!evals.isLoading && board.length === 0 && <div className="empty">No results yet. Run an eval below.</div>}
      </div>

      <Section title="Grounding" aside="Answer from the context or abstain; scored on held-out calibration">
        <div className="panel">
          <table>
            <thead>
              <tr>
                <th>Model</th><th className="num">Score</th><th className="num">Hallucination</th>
                <th className="num">Correct abstain</th><th className="num hide-sm">Quote validity</th><th className="hide-sm">Signals (held-out)</th>
              </tr>
            </thead>
            <tbody>
              {grounded.map((x) => {
                const g = x.roles.grounded;
                const signals = (
                  <>
                      {Object.entries(g.signal_decisions ?? {}).map(([name, d]) => (
                        <span key={name} className={`tag ${d.decision === "kept" ? "tone-ok" : "tone-quiet"}`}
                          title={d.decision === "kept"
                            ? `Threshold ${d.threshold} keeps ${pctText(d.coverage)} of held-out answers with ${pctText(d.error_rate)} errors (baseline ${pctText(d.baseline_error_rate)})`
                            : d.reason ?? "dropped"}>
                          {name}: {d.decision}
                        </span>
                      ))}
                      {!Object.keys(g.signal_decisions ?? {}).length && <span className="quiet">—</span>}
                  </>
                );
                return (
                  <tr key={x.model}>
                    <td>
                      <span className="mono">{x.model}</span>
                      <div className="sub show-sm">{signals}</div>
                    </td>
                    <td className="num">{Math.round(g.score)}</td>
                    <td className={`num ${(g.hallucination_rate ?? 0) > 0.2 ? "tone-bad" : ""}`}>{pctText(g.hallucination_rate)}</td>
                    <td className="num">{pctText(g.correct_abstain_rate)}</td>
                    <td className="num hide-sm">{pctText(g.quote_validity)}</td>
                    <td className="hide-sm small">
                      {signals}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {grounded.length === 0 && <div className="empty">No grounding results yet. Run <span className="mono">python3 evals/run_evals.py --models &lt;model&gt; --roles grounded</span> on the master. A signal is kept only if a threshold chosen on the training split cuts held-out errors in half at ≥ 70% coverage.</div>}
        </div>
      </Section>

      <Section title="Real-diff reviewer" aside="Held-out precision, recall and false-positive rate on this repo's own history">
        <div className="panel">
          <table>
            <thead>
              <tr><th>Model</th><th className="num">Precision</th><th className="num">Recall</th><th className="num">False positives</th></tr>
            </thead>
            <tbody>
              {realReview.map((x) => {
                const m = x.roles.reviewer_real.metrics?.holdout;
                return (
                  <tr key={x.model}>
                    <td>
                      <span className="mono">{x.model}</span>
                      <div className="sub">{isTrustedModel(catalog, x.model) ? <span className="tag tone-ok" title={TRUSTED_RULE} tabIndex={0}>Trusted reviewer</span> : <span className="quiet small" title={TRUSTED_RULE}>not trusted</span>}</div>
                    </td>
                    <td className="num">{pctText(m?.precision)}</td>
                    <td className="num">{pctText(m?.recall)}</td>
                    <td className="num">{pctText(m?.fp_rate)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {realReview.length === 0 && <div className="empty">No real-diff results yet. Run <span className="mono">python3 evals/run_evals.py --models &lt;model&gt; --roles reviewer_real</span>. {TRUSTED_RULE}</div>}
        </div>
      </Section>

      <Section title="Suggested assignments" aside="Best scoring model per role for each node">
        <div className="panel">
          <table>
            <thead><tr><th>Node</th><th>Role</th><th>Model</th><th className="num">Score</th><th /></tr></thead>
            <tbody>
              {(recs.data ?? []).map((r) => (
                <tr key={r.node + r.role}>
                  <td>{fleet.data?.nodes.find((n) => n.id === r.node)?.name ?? r.node}</td><td>{r.role}</td>
                  <td className="mono">{r.model}{!r.installed && <div className="sub">not installed yet</div>}</td>
                  <td className="num">{Math.round(r.score)}</td>
                  <td>{can.admin && <button disabled={busy} onClick={() => run(() => post("/api/roles", { role: r.role, node: r.node, model: r.model, ensure_model: !r.installed }))}>
                    {r.installed ? "Assign" : "Install and assign"}</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!recs.isLoading && !(recs.data ?? []).length && <div className="empty">No suggestions yet: evaluate a few models first.</div>}
        </div>
      </Section>

      {can.operate && <Section title="Run an eval">
        <form className="panel" style={{ padding: 14 }} onSubmit={start}>
          <div className="grid-form">
            <label className="field">Node
              <select value={node} onChange={(e) => { setNode(e.target.value); setModel(""); }} required>
                <option value="" disabled>Choose a node</option>
                {nodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
              </select>
            </label>
            <label className="field">Model
              <select value={model} onChange={(e) => setModel(e.target.value)} required disabled={!node}>
                <option value="" disabled>{node ? "Choose a model" : "Choose a node first"}</option>
                {nodeModels.map((m) => <option key={m}>{m}</option>)}
              </select>
            </label>
          </div>
          <div className="row" style={{ marginTop: 12 }}>
            <button className="primary" disabled={busy || !model || !node}>Run eval</button>
            <span className="small quiet">Takes a few minutes per model. One runs at a time; others wait their turn.</span>
          </div>
        </form>
        <div className="panel" style={{ marginTop: 12 }}>
          <table>
            <thead><tr><th>Model</th><th>Node</th><th>State</th><th className="hide-sm">Scores</th><th>When</th></tr></thead>
            <tbody>
              {(evals.data?.runs ?? []).map((r) => (
                <tr key={r.id}>
                  <td className="mono">{r.model}</td>
                  <td>{fleet.data?.nodes.find((n) => n.id === r.node)?.name ?? r.node}</td>
                  <td><span className={`state tone-${RUN_TONE[r.state]}`}>{r.state}</span>{r.errors?.length ? <div className="sub tone-bad">{r.errors[0]}</div> : null}</td>
                  <td className="hide-sm small">{r.scores ? Object.entries(r.scores).map(([k, v]) => `${k} ${Math.round(v)}`).join(" · ") : "—"}</td>
                  <td className="small quiet">{r.started_at ? ago(r.started_at) : "waiting"}{r.started_at && r.ended_at ? ` · took ${duration(r.ended_at - r.started_at)}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!(evals.data?.runs ?? []).length && <div className="empty">No eval runs from the console yet.</div>}
        </div>
      </Section>}
    </>
  );
}
