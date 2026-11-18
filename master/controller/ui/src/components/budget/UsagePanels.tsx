import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get } from "../../lib/api";
import {
  OTHER, formatBytes, formatTokens, kindRows, stackDays,
  type BudgetUsage, type BudgetUsageRow, type LocalReviewer, type Savings,
} from "../../lib/budget";
import { pctText } from "../../lib/format";
import { Card, Empty, ErrorState, Loading, StatusPill, Table, Tabs, type Column } from "../ui";

// Categorical slots 1-3 of the validated reference palette; everything else folds into Other (styles.css .seg.*).
const SERIES = ["series-1", "series-2", "series-3"];

function StackChart({ stack }: { stack: ReturnType<typeof stackDays> }) {
  const max = Math.max(...stack.days.map((d) => d.total), 1);
  const colour = (model: string) => (model === OTHER ? "series-other" : SERIES[stack.series.indexOf(model)] ?? "series-other");
  return (
    <div className="stack-chart viz-root">
      <ul className="legend" aria-label="Legend">
        {stack.series.map((m) => <li key={m}><span className={`swatch ${colour(m)}`} />{m}</li>)}
      </ul>
      <div className="stack-bars" role="region" tabIndex={0} aria-label="Tokens per day by model, as stacked bars; the table above has the numbers">
        {stack.days.map((d) => (
          <div key={d.day} className="stack-col" title={`${d.day}: ${formatTokens(d.total)} tokens`}>
            <span className="stack-total small">{formatTokens(d.total)}</span>
            <div className="stack-area">
              <div className="stack-bar" style={{ height: `${Math.max(2, (d.total / max) * 100)}%` }}>
                {[...d.parts].reverse().map((p) => (
                  <span key={p.model} className={`seg ${colour(p.model)}`} style={{ flexGrow: p.tokens }} title={`${d.day} · ${p.model}: ${formatTokens(p.tokens)} tokens`} />
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

export function UsagePanel() {
  const [days, setDays] = useState(7);
  const byModel = useQuery({ queryKey: ["budget", "usage", days, "model"], queryFn: () => get<BudgetUsage>(`/api/budget/usage?days=${days}&group=model`) });
  const byKind = useQuery({ queryKey: ["budget", "usage", days, "kind"], queryFn: () => get<BudgetUsage>(`/api/budget/usage?days=${days}&group=kind`) });
  const byDay = useQuery({ queryKey: ["budget", "usage", days, "day"], queryFn: () => get<BudgetUsage>(`/api/budget/usage?days=${days}&group=day`) });
  const models = byModel.data?.rows ?? [];
  const paidKinds = (byKind.data?.rows ?? []).filter((r) => r.kind !== "local");
  const stack = useMemo(() => stackDays(byDay.data?.rows ?? []), [byDay.data]);

  const modelCols: Column<BudgetUsageRow>[] = [
    { key: "model", header: "Model", truncate: true, cell: (r) => <><span className="mono">{r.model}</span>{r.local && <div className="small quiet">local model, free</div>}</> },
    { key: "tasks", header: "Tasks", numeric: true, cell: (r) => r.tasks },
    { key: "in", header: "Input", numeric: true, cell: (r) => formatTokens(r.input) },
    { key: "out", header: "Output", numeric: true, cell: (r) => formatTokens(r.output) },
    { key: "cached", header: "Cached", numeric: true, cell: (r) => formatTokens(r.cached) },
    { key: "cost", header: "Cost", numeric: true, cell: (r) => (r.cost_usd == null ? "not reported" : `$${r.cost_usd.toFixed(2)}`) },
  ];
  const kinds = kindRows(paidKinds);
  const kindCols: Column<(typeof kinds)[number]>[] = [
    { key: "kind", header: "Kind", cell: (r) => r.kind },
    { key: "tasks", header: "Tasks", numeric: true, cell: (r) => r.tasks },
    { key: "tokens", header: "Tokens", numeric: true, cell: (r) => r.tokens },
    { key: "avg", header: "Avg prompt", numeric: true, cell: (r) => r.avgPrompt },
    { key: "hits", header: "Limit hits", numeric: true, cell: (r) => r.limitHits },
  ];

  return (
    <div className="stack">
      <Tabs
        label="Period" value={String(days)} onChange={(v) => setDays(Number(v))}
        tabs={[{ id: "1", label: "24 hours" }, { id: "7", label: "7 days" }, { id: "30", label: "30 days" }]}
      />
      {(byModel.error ?? byKind.error ?? byDay.error) && <ErrorState error={byModel.error ?? byKind.error ?? byDay.error} onRetry={() => { byModel.refetch(); byKind.refetch(); byDay.refetch(); }} />}
      {byModel.isLoading ? <Loading rows={3} label="Loading usage" /> : models.length === 0 ? (
        <Empty title="No usage in this period">Token counts arrive when a Claude or Codex task's run ends and when a local role answers through the MCP bridge. Fleet tasks only.</Empty>
      ) : <Table columns={modelCols} rows={models} rowKey={(r) => r.model ?? "?"} caption="Usage by model" />}

      <h3 className="small quiet">Tokens per day by model (input + output)</h3>
      {stack.days.length === 0
        ? <Empty title="No daily usage yet">The chart fills in as tasks finish.</Empty>
        : <Card><StackChart stack={stack} /></Card>}

      <h3 className="small quiet">By task kind (paid)</h3>
      {kinds.length === 0
        ? <Empty title="No paid tasks in this period">Every task records its kind (build, review, merge…); name it on spawn_task.</Empty>
        : <Table columns={kindCols} rows={kinds} rowKey={(r) => r.kind} caption="Usage by task kind" />}
    </div>
  );
}

export function SavingsPanel() {
  const q = useQuery({ queryKey: ["budget", "savings"], queryFn: () => get<Savings>("/api/budget/savings?days=30") });
  const s = q.data;
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
    <Card title="Savings from local routing" actions={<StatusPill tone="quiet" dot={false}>last 30 days</StatusPill>}>
      {q.isLoading ? <Loading rows={2} /> : q.error ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : s && (
        <div className="stack">
          <div className="stats">{items.map(([label, value]) => <div key={label} className="stat"><b>{value}</b><span>{label}</span></div>)}</div>
          {s.local_tokens + s.paid_tokens + s.local_reviews + s.prompt_bytes_stripped === 0 && (
            <p className="quiet small">Nothing measured yet. Savings come from pipelines (local first-pass reviews, deltas, the skip rule) and from generated files stripped out of prompts. Counts only; no dollar figure is invented.</p>
          )}
        </div>
      )}
    </Card>
  );
}

export function ReviewersPanel() {
  const q = useQuery({ queryKey: ["budget", "local-reviewers"], queryFn: () => get<LocalReviewer[]>("/api/budget/local-reviewers") });
  const rows = q.data ?? [];
  const cols: Column<LocalReviewer>[] = [
    { key: "model", header: "Model", truncate: true, cell: (r) => <span className="mono">{r.model}</span> },
    { key: "roles", header: "Roles", cell: (r) => r.roles.join(", ") || <span className="quiet">none</span> },
    { key: "gp", header: "G2 precision", numeric: true, cell: (r) => pctText(r.g2_precision) },
    { key: "gr", header: "G2 recall", numeric: true, cell: (r) => pctText(r.g2_recall) },
    { key: "lp", header: "Live precision", numeric: true, cell: (r) => <span title={`${r.live.confirmed} of ${r.live.findings} findings confirmed`}>{pctText(r.live.precision)}</span> },
    { key: "lr", header: "Live recall", numeric: true, cell: (r) => <span title={`${r.live.misses} paid findings missed`}>{pctText(r.live.recall)}</span> },
    { key: "trust", header: "Trust", cell: (r) => <StatusPill tone={r.trusted ? "ok" : "quiet"}>{r.trusted ? "Trusted" : "Hints recorded only"}</StatusPill> },
  ];
  return (
    <div className="stack">
      <span className="small quiet">Live agreement informs you; it never changes trust.</span>
      {q.isLoading ? <Loading rows={2} /> : q.error ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : rows.length === 0 ? (
        <Empty title="No local reviewers yet">Benchmark a model on the <Link to="/evals">Evals</Link> page and give it the reviewer role.</Empty>
      ) : <Table columns={cols} rows={rows} rowKey={(r) => r.model} caption="Local reviewers" />}
    </div>
  );
}
