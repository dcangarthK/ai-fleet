import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get, post, put } from "../lib/api";
import { ErrorBanner, LoadError, PageHead, Section } from "../components/bits";
import { useCan, useRun } from "../lib/data";
import { ago, pctText } from "../lib/format";
import {
  EFFORTS, KINDS, OTHER, TIERS, clockTime, concurrencyText, formToRules, formatBytes, formatTokens, kindRows, meter,
  modelLabel, ruleSummary, rulesDiff, rulesFormClass, rulesToForm, stackDays, statusCard,
  type BudgetStatus, type BudgetUsage, type Decision, type LocalReviewer, type RulesForm, type RulesVersion,
  type Savings,
} from "../lib/budget";
import { canMarkSignedIn, slotName, slotPill, type ResourceSlot } from "../lib/handoff";

const FLEET_ONLY = "Fleet tasks only: your own interactive use draws on the same limits, so real headroom is lower.";
// Categorical slots 1-3 of the validated reference palette (dataviz skill); everything else folds into Other.
const SERIES = ["series-1", "series-2", "series-3"];

function usePhone(): boolean {
  const query = "(max-width: 860px)";
  const [phone, setPhone] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setPhone(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return phone;
}

export function BudgetPage() {
  const status = useQuery({ queryKey: ["budget", "status"], queryFn: () => get<BudgetStatus>("/api/budget/status") });
  return (
    <>
      <PageHead title="Budget" intro={<>Paid-model spend, the rules the controller enforces, and what local models saved. {FLEET_ONLY}</>} />
      <LoadError error={status.error} />
      <StatusStrip status={status.data} loading={status.isLoading} />
      <ResourcesSection />
      <UsageSection />
      <SavingsSection />
      <RulesSection />
      <LocalReviewersSection />
      <DecisionsSection />
    </>
  );
}

function StatusStrip({ status, loading }: { status?: BudgetStatus; loading: boolean }) {
  const shared = (status?.profiles ?? []).filter((p) => p.codex_profile === "shared");
  return (
    <Section title="Status" aside="fleet tasks only">
      {status?.rules_invalid && (
        <div className="banner error" role="alert">
          Stored rules invalid; defaults in force. {status.rules_error}. Fix them under Rules or restore an earlier version.
        </div>
      )}
      {shared.length > 0 && (
        <div className="banner note" role="status">
          Codex fleet tasks use your own Codex profile on {shared.map((p) => p.name).join(", ")} (plugins, memories and its
          default effort included). Sign the fleet profile in once: <span className="mono">CODEX_HOME=~/.codex-fleet codex login</span>
        </div>
      )}
      {loading && <p className="quiet">Loading…</p>}
      {!loading && !(status?.models.length) && (
        <p className="empty panel">No paid models in the budget rules. Add Codex or a Claude model under Rules to see its status here.</p>
      )}
      <div className="budget-cards">
        {(status?.models ?? []).map((m) => {
          const card = statusCard(m);
          const gauge = meter(m.today_tokens, m.soft, m.hard);
          return (
            <div key={m.key} className="budget-card">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <b>{modelLabel(m.key)}</b>
                <span className={`state tone-${card.tone}`}>{card.label}</span>
              </div>
              <div className="small quiet mono">{m.key}</div>
              <div className="small">Running {concurrencyText(m)}</div>
              <div className="meter" role="meter" aria-label={`${modelLabel(m.key)} tokens today`}
                   aria-valuemin={0} aria-valuemax={100} aria-valuenow={gauge.pct == null ? undefined : Math.round(gauge.pct * 100)}>
                <span className={`fill tone-bg-${gauge.tone}`} style={{ width: gauge.pct == null ? 0 : `${Math.round(gauge.pct * 100)}%` }} />
              </div>
              <div className="small quiet">{gauge.label}</div>
            </div>
          );
        })}
      </div>
    </Section>
  );
}

function UsageSection() {
  const [days, setDays] = useState(7);
  const byModel = useQuery({ queryKey: ["budget", "usage", days, "model"], queryFn: () => get<BudgetUsage>(`/api/budget/usage?days=${days}&group=model`) });
  const byKind = useQuery({ queryKey: ["budget", "usage", days, "kind"], queryFn: () => get<BudgetUsage>(`/api/budget/usage?days=${days}&group=kind`) });
  const byDay = useQuery({ queryKey: ["budget", "usage", days, "day"], queryFn: () => get<BudgetUsage>(`/api/budget/usage?days=${days}&group=day`) });
  const models = byModel.data?.rows ?? [];
  const paidKinds = (byKind.data?.rows ?? []).filter((r) => r.kind !== "local");
  const stack = useMemo(() => stackDays(byDay.data?.rows ?? []), [byDay.data]);
  return (
    <Section title="Usage" aside="fleet tasks only">
      <div className="tabs" role="tablist" aria-label="Period">
        {[[1, "24 hours"], [7, "7 days"], [30, "30 days"]].map(([n, label]) => (
          <button key={n} role="tab" aria-selected={days === n} onClick={() => setDays(n as number)}>{label}</button>
        ))}
      </div>
      <LoadError error={byModel.error ?? byKind.error ?? byDay.error} />
      <div className="panel">
        <table>
          <thead><tr><th>Model</th><th className="num">Tasks</th><th className="num">Input</th><th className="num">Output</th><th className="num">Cached</th><th className="num">Cost</th></tr></thead>
          <tbody>
            {models.map((r) => (
              <tr key={r.model}>
                <td><span className="mono">{r.model}</span>{r.local && <div className="sub">local model, free</div>}</td>
                <td className="num">{r.tasks}</td>
                <td className="num">{formatTokens(r.input)}</td>
                <td className="num">{formatTokens(r.output)}</td>
                <td className="num">{formatTokens(r.cached)}</td>
                <td className="num">{r.cost_usd == null ? "not reported" : `$${r.cost_usd.toFixed(2)}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!byModel.isLoading && models.length === 0 && (
          <p className="empty">No usage in this period. Token counts arrive when a Claude or Codex task's run ends (runner 2.1 reports what the CLI says) and when a local role answers through the MCP bridge.</p>
        )}
      </div>

      <h3 className="small quiet" style={{ margin: "16px 0 6px" }}>Tokens per day by model (input + output)</h3>
      {stack.days.length === 0 ? (
        <p className="empty panel">No daily usage yet; the chart fills in as tasks finish.</p>
      ) : (
        <StackChart stack={stack} />
      )}

      <h3 className="small quiet" style={{ margin: "16px 0 6px" }}>By task kind (paid)</h3>
      <div className="panel">
        <table>
          <thead><tr><th>Kind</th><th className="num">Tasks</th><th className="num">Tokens</th><th className="num">Avg prompt</th><th className="num">Limit hits</th></tr></thead>
          <tbody>
            {kindRows(paidKinds).map((r) => (
              <tr key={r.kind}><td>{r.kind}</td><td className="num">{r.tasks}</td><td className="num">{r.tokens}</td>
                <td className="num">{r.avgPrompt}</td><td className="num">{r.limitHits}</td></tr>
            ))}
          </tbody>
        </table>
        {!byKind.isLoading && paidKinds.length === 0 && (
          <p className="empty">No paid tasks in this period. Every task records its kind (build, review, merge…); name it on spawn_task.</p>
        )}
      </div>
    </Section>
  );
}

function StackChart({ stack }: { stack: ReturnType<typeof stackDays> }) {
  const max = Math.max(...stack.days.map((d) => d.total), 1);
  const colour = (model: string) => (model === OTHER ? "series-other" : SERIES[stack.series.indexOf(model)] ?? "series-other");
  return (
    <div className="panel stack-chart viz-root">
      <ul className="legend" aria-label="Legend">
        {stack.series.map((m) => (
          <li key={m}><span className={`swatch ${colour(m)}`} />{m}</li>
        ))}
      </ul>
      <div className="stack-bars" role="img" aria-label="Stacked bars of tokens per day by model; the table above has the numbers">
        {stack.days.map((d) => (
          <div key={d.day} className="stack-col" title={`${d.day}: ${formatTokens(d.total)} tokens`}>
            <span className="stack-total small">{formatTokens(d.total)}</span>
            <div className="stack-area">
              <div className="stack-bar" style={{ height: `${Math.max(2, (d.total / max) * 100)}%` }}>
                {[...d.parts].reverse().map((p) => (
                  <span key={p.model} className={`seg ${colour(p.model)}`} style={{ flexGrow: p.tokens }}
                        title={`${d.day} · ${p.model}: ${formatTokens(p.tokens)} tokens`} />
                ))}
              </div>
            </div>
            <span className="stack-day small quiet">{d.day.slice(5)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function SavingsSection() {
  const savings = useQuery({ queryKey: ["budget", "savings"], queryFn: () => get<Savings>("/api/budget/savings?days=30") });
  const s = savings.data;
  const items: [string, string][] = s ? [
    ["Local tokens", formatTokens(s.local_tokens)],
    ["Paid tokens", formatTokens(s.paid_tokens)],
    ["Local first-pass reviews", String(s.local_reviews)],
    ["Paid rounds avoided", String(s.paid_rounds_avoided)],
    ["Paid reviews skipped by rule", String(s.paid_reviews_skipped)],
    ["Prompt bytes stripped", formatBytes(s.prompt_bytes_stripped)],
    ["Re-reviews sent as delta", String(s.delta_reviews)],
    ["Bytes saved by deltas", formatBytes(s.delta_bytes_saved)],
  ] : [];
  return (
    <Section title="Savings" aside="last 30 days, fleet tasks only">
      <LoadError error={savings.error} />
      {s && (
        <div className="stats">
          {items.map(([label, value]) => <div key={label} className="stat"><b>{value}</b><span>{label}</span></div>)}
        </div>
      )}
      {s && s.local_tokens + s.paid_tokens + s.local_reviews + s.prompt_bytes_stripped === 0 && (
        <p className="quiet small">Nothing measured yet. Savings come from pipelines (local first-pass reviews, deltas, the skip rule) and from generated files stripped out of prompts.</p>
      )}
    </Section>
  );
}

function RulesSection() {
  const can = useCan();
  const phone = usePhone();
  const { run, busy, error, clearError } = useRun();
  const current = useQuery({ queryKey: ["budget", "rules"], queryFn: () => get<RulesVersion>("/api/budget/rules") });
  const history = useQuery({ queryKey: ["budget", "rules", "history"], queryFn: () => get<RulesVersion[]>("/api/budget/rules/history") });
  const [form, setForm] = useState<RulesForm | null>(null);
  useEffect(() => {
    if (current.data) setForm(rulesToForm(current.data.body));
  }, [current.data]);
  const editable = can.admin;
  const previous = (history.data ?? [])[1];
  const changes = current.data && previous ? rulesDiff(previous.body, current.data.body) : [];
  const pending = form && current.data ? rulesDiff(current.data.body, formToRules(form)) : [];
  const set = (patch: Partial<RulesForm>) => form && setForm({ ...form, ...patch });
  const setModel = (i: number, patch: Partial<RulesForm["models"][number]>) =>
    form && setForm({ ...form, models: form.models.map((m, j) => (j === i ? { ...m, ...patch } : m)) });

  return (
    <Section title="Rules" aside={current.data ? `version ${current.data.version}${editable ? "" : " · read only (admin edits)"}` : undefined}>
      <LoadError error={current.error} />
      <ErrorBanner error={error} onDismiss={clearError} />
      {current.data && (
        <ul className="summary">
          {Object.entries(current.data.body.models).map(([key, rule]) => <li key={key}>{ruleSummary(key, rule)}</li>)}
        </ul>
      )}
      {form && (
        <form className="stack" onSubmit={(e) => {
          e.preventDefault();
          run(() => put("/api/budget/rules", { body: formToRules(form) }));
        }}>
          <fieldset disabled={!editable || busy} className="plain">
            {form.models.map((m, i) => (
              <div key={m.key} className="panel rules-model">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <b>{modelLabel(m.key)}</b><span className="mono small quiet">{m.key}</span>
                </div>
                <div className={rulesFormClass(phone)}>
                  <label className="field">Tier
                    <select value={m.tier} onChange={(e) => setModel(i, { tier: e.target.value as typeof m.tier })}>
                      {TIERS.map((t) => <option key={t}>{t}</option>)}
                    </select>
                  </label>
                  <label className="field">Max at a time
                    <input inputMode="numeric" value={m.max_concurrent} onChange={(e) => setModel(i, { max_concurrent: e.target.value })} />
                  </label>
                  <label className="field">Daily soft tokens
                    <input inputMode="numeric" value={m.soft} placeholder="none" onChange={(e) => setModel(i, { soft: e.target.value })} />
                  </label>
                  <label className="field">Daily hard tokens
                    <input inputMode="numeric" value={m.hard} placeholder="none" onChange={(e) => setModel(i, { hard: e.target.value })} />
                  </label>
                </div>
                <div className="chips" role="group" aria-label={`Allowed kinds for ${m.key}`}>
                  {["*", ...KINDS].map((k) => {
                    const on = m.kinds.includes(k);
                    return (
                      <button type="button" key={k} className={`chip ${on ? "on" : ""}`} aria-pressed={on}
                              onClick={() => setModel(i, { kinds: on ? m.kinds.filter((x) => x !== k) : [...m.kinds, k] })}>
                        {k === "*" ? "any" : k.replace(/_/g, " ")}
                      </button>
                    );
                  })}
                </div>
                <details>
                  <summary className="small">Default effort per kind</summary>
                  <div className={rulesFormClass(phone)}>
                    {["*", ...KINDS].map((k) => (
                      <label className="field" key={k}>{k === "*" ? "any other" : k.replace(/_/g, " ")}
                        <select value={m.effort[k] ?? ""} onChange={(e) => setModel(i, { effort: { ...m.effort, [k]: e.target.value } })}>
                          <option value="">CLI default</option>
                          {EFFORTS.map((x) => <option key={x}>{x}</option>)}
                        </select>
                      </label>
                    ))}
                  </div>
                </details>
              </div>
            ))}

            <div className="panel rules-model">
              <b>Prompts</b>
              <div className={rulesFormClass(phone)}>
                <label className="field">Split above (KB)<input inputMode="numeric" value={form.max_kb} onChange={(e) => set({ max_kb: e.target.value })} /></label>
                <label className="field">Refuse above (KB)<input inputMode="numeric" value={form.hard_max_kb} onChange={(e) => set({ hard_max_kb: e.target.value })} /></label>
                <label className="field">Longest added line<input inputMode="numeric" value={form.max_line_chars} onChange={(e) => set({ max_line_chars: e.target.value })} /></label>
              </div>
              <label className="field">Generated files left out of prompts (one glob per line)
                <textarea rows={5} value={form.exclude_globs} onChange={(e) => set({ exclude_globs: e.target.value })} />
              </label>
            </div>

            <div className="panel rules-model">
              <b>Limits</b>
              <div className={rulesFormClass(phone)}>
                <label className="field">When a model is limited
                  <select value={form.on_limit} onChange={(e) => set({ on_limit: e.target.value as RulesForm["on_limit"] })}>
                    <option value="queue">queue until it resets</option>
                    <option value="fallback">send to a fallback</option>
                    <option value="refuse">refuse</option>
                  </select>
                </label>
                <label className="field">Limit scope
                  <select value={form.limit_scope} onChange={(e) => set({ limit_scope: e.target.value as RulesForm["limit_scope"] })}>
                    <option value="model">per model (two limited models limit the account)</option>
                    <option value="account">whole account</option>
                  </select>
                </label>
                <label className="field">Models not in the rules
                  <select value={form.unlisted} onChange={(e) => set({ unlisted: e.target.value as RulesForm["unlisted"] })}>
                    <option value="allow">allow</option><option value="refuse">refuse</option>
                  </select>
                </label>
              </div>
              <label className="field">Fallbacks (one per line: <span className="mono">model: fallback, fallback</span>)
                <textarea rows={3} value={form.fallbacks} onChange={(e) => set({ fallbacks: e.target.value })} />
              </label>
            </div>

            <div className="panel rules-model">
              <b>Reviews</b>
              <div className="row">
                {["local", "paid"].map((st) => (
                  <label key={st} className="check row small">
                    <input type="checkbox" checked={form.stages.includes(st)}
                           onChange={(e) => set({ stages: e.target.checked ? [...form.stages, st] : form.stages.filter((x) => x !== st) })} />
                    {st === "local" ? "local first pass" : "paid review"}
                  </label>
                ))}
              </div>
              <div className={rulesFormClass(phone)}>
                <label className="field">Paid rounds per pipeline<input inputMode="numeric" value={form.max_paid_rounds} onChange={(e) => set({ max_paid_rounds: e.target.value })} /></label>
                <label className="field">Codex rounds per pipeline<input inputMode="numeric" value={form.max_codex_rounds} onChange={(e) => set({ max_codex_rounds: e.target.value })} /></label>
              </div>
              <label className="field">Security paths (always get a paid review; Codex reviews them)
                <textarea rows={4} value={form.security_paths} onChange={(e) => set({ security_paths: e.target.value })} />
              </label>
              <label className="check row small">
                <input type="checkbox" checked={form.skip_on} onChange={(e) => set({ skip_on: e.target.checked })} />
                Skip the paid review for small changes a trusted local reviewer approved (off by default)
              </label>
              {form.skip_on && (
                <div className={rulesFormClass(phone)}>
                  <label className="field">At most changed lines<input inputMode="numeric" value={form.skip_max_lines} onChange={(e) => set({ skip_max_lines: e.target.value })} /></label>
                  <label className="field">Only these paths<textarea rows={3} value={form.skip_paths} onChange={(e) => set({ skip_paths: e.target.value })} /></label>
                </div>
              )}
            </div>
          </fieldset>
          {editable && (
            <div className="row">
              <button className="primary" type="submit" disabled={busy || pending.length === 0}>Save as new version</button>
              <button type="button" disabled={busy || pending.length === 0} onClick={() => current.data && setForm(rulesToForm(current.data.body))}>Discard changes</button>
              <span className="quiet small">{pending.length ? `${pending.length} change${pending.length > 1 ? "s" : ""} not saved` : "No unsaved changes"}</span>
            </div>
          )}
        </form>
      )}

      <h3 className="small quiet" style={{ margin: "16px 0 6px" }}>
        {previous ? `Changes from version ${previous.version} to ${current.data?.version}` : "History"}
      </h3>
      {!previous && <p className="quiet small">Only the seeded rules so far. Each save adds a version, and you can restore any earlier one.</p>}
      {previous && (
        <div className="panel">
          <table>
            <thead><tr><th>Setting</th><th>Before</th><th>After</th></tr></thead>
            <tbody>
              {changes.map((c) => (
                <tr key={c.path}><td className="mono small">{c.path}</td><td className="mono small wrap">{c.before}</td><td className="mono small wrap">{c.after}</td></tr>
              ))}
            </tbody>
          </table>
          {changes.length === 0 && <p className="empty">No differences (a restore of the same body).</p>}
          {editable && (
            <div className="row" style={{ padding: "8px 12px" }}>
              <button type="button" disabled={busy}
                      onClick={() => run(() => post("/api/budget/rules/restore", { version: previous.version }))}>
                Restore version {previous.version}
              </button>
              <span className="quiet small">by {previous.author ?? "unknown"}{previous.created_at ? `, ${ago(previous.created_at)}` : ""}</span>
            </div>
          )}
        </div>
      )}
    </Section>
  );
}

/** Resources board (docs/SPEC-limit-handoff.md 5): one row per slot. Labels only; directories are never sent. */
function ResourcesSection() {
  const slots = useQuery({ queryKey: ["budget", "resources"], queryFn: () => get<ResourceSlot[]>("/api/resources"),
    refetchInterval: 30_000 });
  const rows = slots.data ?? [];
  const admin = useCan().admin;
  const { run, busy, error, clearError } = useRun();
  return (
    <Section title="Resources" aside="paid accounts per node: limits and concurrency are per account label">
      <LoadError error={slots.error} />
      <ErrorBanner error={error} onDismiss={clearError} />
      <div className="panel">
        <table>
          <thead><tr><th>Node</th><th>Agent</th><th>Account</th><th>Model</th><th>State</th><th>Kinds</th><th className="num">Tokens today</th></tr></thead>
          <tbody>
            {rows.map((s) => {
              const pill = slotPill(s);
              return (
                <tr key={`${s.node}/${s.agent}/${s.profile}/${s.model}`}>
                  <td>{s.node_name ?? s.node}</td>
                  <td>{s.agent}</td>
                  <td>{s.label ?? <span className="quiet">{s.profile}</span>}</td>
                  <td className="mono">{slotName(s)}</td>
                  <td>
                    <span className={`state tone-${pill.tone}`}>{pill.label}</span>
                    {admin && canMarkSignedIn(s) && (
                      <button className="small" disabled={busy}
                              onClick={() => confirm(`Has ${s.label ?? s.profile} on ${s.node_name ?? s.node} been signed in again?`)
                                && run(() => post("/api/resources/signed-in", { node: s.node, agent: s.agent, profile: s.profile }))}>
                        Signed in again
                      </button>
                    )}
                  </td>
                  <td className="small">{s.kinds.join(", ")}</td>
                  <td className="num">{formatTokens(s.today_tokens)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!slots.isLoading && rows.length === 0 && (
          <p className="empty">No paid agents on any node yet. Each runner lists its signed-in profiles in runner.json.</p>
        )}
      </div>
    </Section>
  );
}

function LocalReviewersSection() {
  const reviewers = useQuery({ queryKey: ["budget", "local-reviewers"], queryFn: () => get<LocalReviewer[]>("/api/budget/local-reviewers") });
  const rows = reviewers.data ?? [];
  return (
    <Section title="Local reviewers" aside="live agreement informs you; it never changes trust">
      <LoadError error={reviewers.error} />
      <div className="panel">
        <table>
          <thead><tr><th>Model</th><th>Roles</th><th className="num">G2 precision</th><th className="num">G2 recall</th><th className="num">Live precision</th><th className="num">Live recall</th><th>Trust</th></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.model}>
                <td className="mono">{r.model}</td>
                <td>{r.roles.join(", ") || <span className="quiet">none</span>}</td>
                <td className="num">{pctText(r.g2_precision)}</td>
                <td className="num">{pctText(r.g2_recall)}</td>
                <td className="num" title={`${r.live.confirmed} of ${r.live.findings} findings confirmed`}>{pctText(r.live.precision)}</td>
                <td className="num" title={`${r.live.misses} paid findings missed`}>{pctText(r.live.recall)}</td>
                <td>{r.trusted ? <span className="state tone-ok">Trusted</span> : <span className="state tone-quiet">Hints recorded only</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!reviewers.isLoading && rows.length === 0 && (
          <p className="empty">No local reviewers yet. Benchmark a model on the <Link to="/evals">Evals</Link> page and give it the reviewer role; live agreement appears once paid reviewers confirm or reject its findings in pipelines.</p>
        )}
      </div>
    </Section>
  );
}

function DecisionsSection() {
  const decisions = useQuery({ queryKey: ["budget", "decisions"], queryFn: () => get<Decision[]>("/api/budget/decisions?limit=50") });
  const rows = decisions.data ?? [];
  const tone = { refused: "bad", queued: "warn", rerouted: "info" } as const;
  return (
    <Section title="Recent refusals and reroutes" aside="last 50 admission decisions">
      <LoadError error={decisions.error} />
      <div className="panel">
        <table>
          <thead><tr><th>When</th><th>Decision</th><th>Reason</th><th>Task</th></tr></thead>
          <tbody>
            {rows.map((d) => (
              <tr key={d.id}>
                <td className="small" title={new Date(d.ts * 1000).toLocaleString()}>{clockTime(d.ts)}</td>
                <td><span className={`state tone-${tone[d.decision] ?? "quiet"}`}>{d.decision}</span></td>
                <td className="wrap">{d.reason}</td>
                <td>{d.task_id
                  ? <Link to={`/tasks?view=all&tab=all&task=${encodeURIComponent(d.task_id)}`}>{d.title ?? d.task_id}</Link>
                  : <span className="quiet small">{d.title ?? "not created"}</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!decisions.isLoading && rows.length === 0 && (
          <p className="empty">No task has been refused, queued for a limit or slot, or rerouted. Decisions show here when the budget rules act on a new task.</p>
        )}
      </div>
    </Section>
  );
}
