import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get, post, put } from "../../lib/api";
import { useCan, useRun } from "../../lib/data";
import { ago } from "../../lib/format";
import {
  EFFORTS, KINDS, TIERS, formToRules, modelLabel, ruleSummary, rulesDiff, rulesFormClass, rulesToForm,
  type RulesForm, type RulesVersion,
} from "../../lib/budget";
import { ErrorBanner } from "../bits";
import { Button, Card, Empty, ErrorState, Field, Loading, Select, StatusPill, useConfirm } from "../ui";
import { RuleDiff } from "./RuleDiff";

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

/** The rules form, version history and compare. Saving or restoring first shows the exact diff in a confirmation. */
export function RulesEditor() {
  const can = useCan();
  const phone = usePhone();
  const confirm = useConfirm();
  const { run, busy, error, clearError } = useRun();
  const current = useQuery({ queryKey: ["budget", "rules"], queryFn: () => get<RulesVersion>("/api/budget/rules") });
  const history = useQuery({ queryKey: ["budget", "rules", "history"], queryFn: () => get<RulesVersion[]>("/api/budget/rules/history") });
  const [form, setForm] = useState<RulesForm | null>(null);
  const [compare, setCompare] = useState<string>("");
  useEffect(() => {
    if (current.data) setForm(rulesToForm(current.data.body));
  }, [current.data]);

  const editable = can.admin;
  const versions = history.data ?? [];
  const older = versions.filter((v) => v.version !== current.data?.version);
  const base = older.find((v) => String(v.version) === compare) ?? older[0];
  const changes = current.data && base ? rulesDiff(base.body, current.data.body) : [];
  const pending = form && current.data ? rulesDiff(current.data.body, formToRules(form)) : [];
  const set = (patch: Partial<RulesForm>) => form && setForm({ ...form, ...patch });
  const setModel = (i: number, patch: Partial<RulesForm["models"][number]>) =>
    form && setForm({ ...form, models: form.models.map((m, j) => (j === i ? { ...m, ...patch } : m)) });

  async function save() {
    if (!form) return;
    const body = formToRules(form);
    const ok = await confirm({
      title: `Save rules as version ${(current.data?.version ?? 0) + 1}`,
      message: (
        <div className="stack">
          <p>{pending.length} change{pending.length === 1 ? "" : "s"} to the rules the controller enforces on every new task. Nothing running is stopped.</p>
          <RuleDiff rows={pending} label="Changes to save" />
        </div>
      ),
      confirmLabel: "Save as new version",
    });
    if (ok) run(() => put("/api/budget/rules", { body }));
  }

  async function restore(v: RulesVersion) {
    const rows = current.data ? rulesDiff(current.data.body, v.body) : [];
    const ok = await confirm({
      title: `Restore version ${v.version}`,
      message: (
        <div className="stack">
          <p>This saves version {v.version}'s rules as a new version. What changes from now:</p>
          <RuleDiff rows={rows} label={`Changes from restoring version ${v.version}`} emptyText="Identical to the current rules." />
        </div>
      ),
      confirmLabel: `Restore version ${v.version}`,
    });
    if (ok) run(() => post("/api/budget/rules/restore", { version: v.version }));
  }

  if (current.isLoading) return <Loading rows={5} label="Loading rules" />;
  if (current.error) return <ErrorState error={current.error} onRetry={() => current.refetch()} />;

  return (
    <div className="stack">
      <ErrorBanner error={error} onDismiss={clearError} />
      {current.data && (
        <div className="row" style={{ justifyContent: "space-between", flexWrap: "wrap" }}>
          <span className="small quiet">
            Version {current.data.version}{current.data.author ? `, saved by ${current.data.author}` : ""}{current.data.created_at ? `, ${ago(current.data.created_at)}` : ""}
          </span>
          <StatusPill tone={editable ? "ok" : "quiet"}>{editable ? "Admin: editable" : "Read only (admin edits)"}</StatusPill>
        </div>
      )}
      {current.data && (
        <ul className="summary">
          {Object.entries(current.data.body.models).map(([key, rule]) => <li key={key}>{ruleSummary(key, rule)}</li>)}
        </ul>
      )}

      {form && (
        <form className="stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <fieldset disabled={!editable || busy} className="plain">
            {form.models.map((m, i) => (
              <Card key={m.key} title={modelLabel(m.key)} actions={<span className="mono small quiet">{m.key}</span>}>
                <div className="stack">
                  <div className={rulesFormClass(phone)}>
                    <Field label="Tier">{(p) => (
                      <Select {...p} value={m.tier} onChange={(e) => setModel(i, { tier: e.target.value as typeof m.tier })}>
                        {TIERS.map((t) => <option key={t}>{t}</option>)}
                      </Select>
                    )}</Field>
                    <Field label="Max at a time">{(p) => <input {...p} inputMode="numeric" value={m.max_concurrent} onChange={(e) => setModel(i, { max_concurrent: e.target.value })} />}</Field>
                    <Field label="Daily soft tokens">{(p) => <input {...p} inputMode="numeric" value={m.soft} placeholder="none" onChange={(e) => setModel(i, { soft: e.target.value })} />}</Field>
                    <Field label="Daily hard tokens">{(p) => <input {...p} inputMode="numeric" value={m.hard} placeholder="none" onChange={(e) => setModel(i, { hard: e.target.value })} />}</Field>
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
                        <Field key={k} label={k === "*" ? "any other" : k.replace(/_/g, " ")}>{(p) => (
                          <Select {...p} value={m.effort[k] ?? ""} onChange={(e) => setModel(i, { effort: { ...m.effort, [k]: e.target.value } })}>
                            <option value="">CLI default</option>
                            {EFFORTS.map((x) => <option key={x}>{x}</option>)}
                          </Select>
                        )}</Field>
                      ))}
                    </div>
                  </details>
                </div>
              </Card>
            ))}

            <Card title="Prompts">
              <div className="stack">
                <div className={rulesFormClass(phone)}>
                  <Field label="Split above (KB)">{(p) => <input {...p} inputMode="numeric" value={form.max_kb} onChange={(e) => set({ max_kb: e.target.value })} />}</Field>
                  <Field label="Refuse above (KB)">{(p) => <input {...p} inputMode="numeric" value={form.hard_max_kb} onChange={(e) => set({ hard_max_kb: e.target.value })} />}</Field>
                  <Field label="Longest added line">{(p) => <input {...p} inputMode="numeric" value={form.max_line_chars} onChange={(e) => set({ max_line_chars: e.target.value })} />}</Field>
                </div>
                <Field label="Generated files left out of prompts (one glob per line)">{(p) => <textarea {...p} rows={5} value={form.exclude_globs} onChange={(e) => set({ exclude_globs: e.target.value })} />}</Field>
              </div>
            </Card>

            <Card title="Limits">
              <div className="stack">
                <div className={rulesFormClass(phone)}>
                  <Field label="When a model is limited">{(p) => (
                    <Select {...p} value={form.on_limit} onChange={(e) => set({ on_limit: e.target.value as RulesForm["on_limit"] })}>
                      <option value="queue">queue until it resets</option>
                      <option value="fallback">send to a fallback</option>
                      <option value="refuse">refuse</option>
                    </Select>
                  )}</Field>
                  <Field label="Limit scope">{(p) => (
                    <Select {...p} value={form.limit_scope} onChange={(e) => set({ limit_scope: e.target.value as RulesForm["limit_scope"] })}>
                      <option value="auto">automatic (Claude: whole account, Codex: per model)</option>
                      <option value="model">per model (two limited models limit the account)</option>
                      <option value="account">whole account</option>
                    </Select>
                  )}</Field>
                  <Field label="Models not in the rules">{(p) => (
                    <Select {...p} value={form.unlisted} onChange={(e) => set({ unlisted: e.target.value as RulesForm["unlisted"] })}>
                      <option value="allow">allow</option><option value="refuse">refuse</option>
                    </Select>
                  )}</Field>
                </div>
                <Field label={<>Fallbacks (one per line: <span className="mono">model: fallback, fallback</span>)</>}>{(p) => <textarea {...p} rows={3} value={form.fallbacks} onChange={(e) => set({ fallbacks: e.target.value })} />}</Field>
              </div>
            </Card>

            <Card title="Reviews">
              <div className="stack">
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
                  <Field label="Paid rounds per pipeline">{(p) => <input {...p} inputMode="numeric" value={form.max_paid_rounds} onChange={(e) => set({ max_paid_rounds: e.target.value })} />}</Field>
                  <Field label="Codex rounds per pipeline">{(p) => <input {...p} inputMode="numeric" value={form.max_codex_rounds} onChange={(e) => set({ max_codex_rounds: e.target.value })} />}</Field>
                </div>
                <Field label="Security paths (always get a paid review; Codex reviews them)">{(p) => <textarea {...p} rows={4} value={form.security_paths} onChange={(e) => set({ security_paths: e.target.value })} />}</Field>
                <label className="check row small">
                  <input type="checkbox" checked={form.skip_on} onChange={(e) => set({ skip_on: e.target.checked })} />
                  Skip the paid review for small changes a trusted local reviewer approved (off by default)
                </label>
                {form.skip_on && (
                  <div className={rulesFormClass(phone)}>
                    <Field label="At most changed lines">{(p) => <input {...p} inputMode="numeric" value={form.skip_max_lines} onChange={(e) => set({ skip_max_lines: e.target.value })} />}</Field>
                    <Field label="Only these paths">{(p) => <textarea {...p} rows={3} value={form.skip_paths} onChange={(e) => set({ skip_paths: e.target.value })} />}</Field>
                  </div>
                )}
              </div>
            </Card>
          </fieldset>
          {editable && (
            <div className="row" style={{ flexWrap: "wrap" }}>
              <Button variant="primary" type="submit" disabled={busy || pending.length === 0}>Review changes and save</Button>
              <Button disabled={busy || pending.length === 0} onClick={() => current.data && setForm(rulesToForm(current.data.body))}>Discard changes</Button>
              <span className="quiet small" role="status">{pending.length ? `${pending.length} change${pending.length > 1 ? "s" : ""} not saved` : "No unsaved changes"}</span>
            </div>
          )}
        </form>
      )}

      <Card
        title="Version history"
        actions={older.length > 0 && (
          <Field label="Compare current with" className="row">{(p) => (
            <Select {...p} value={String(base?.version ?? "")} onChange={(e) => setCompare(e.target.value)}>
              {older.map((v) => <option key={v.version} value={v.version}>v{v.version}{v.author ? ` · ${v.author}` : ""}{v.created_at ? ` · ${ago(v.created_at)}` : ""}</option>)}
            </Select>
          )}</Field>
        )}
      >
        {history.isLoading ? <Loading rows={2} /> : history.error ? <ErrorState error={history.error} onRetry={() => history.refetch()} /> :
          !base ? <Empty title="Only the seeded rules so far">Each save adds a version, and you can restore any earlier one.</Empty> : (
            <div className="stack">
              <span className="small quiet">Version {base.version} → {current.data?.version}</span>
              <RuleDiff rows={changes} label={`Rules diff, version ${base.version} to ${current.data?.version}`} emptyText="No differences (a restore of the same body)." />
              {editable && (
                <div className="row" style={{ flexWrap: "wrap" }}>
                  <Button disabled={busy} onClick={() => restore(base)}>Restore version {base.version}</Button>
                  <span className="quiet small">by {base.author ?? "unknown"}{base.created_at ? `, ${ago(base.created_at)}` : ""}</span>
                </div>
              )}
            </div>
          )}
      </Card>
    </div>
  );
}
