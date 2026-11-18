import { useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { flushSync } from "react-dom";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useWork } from "../lib/data";
import { ago } from "../lib/format";
import { PageHead, StatusPill } from "../components/bits";
import { Card, Empty, ErrorState, Input, Loading, Select, Table, Truncate, type Column } from "../components/ui";
import type { WorkItem } from "../lib/lineage";
import { Arrive, useArrivals } from "../components/motion/Arrive";
import { Sparkline } from "../components/motion/Sparkline";
import { useVisible } from "../components/motion/useVisible";
import { withViewTransition } from "../components/motion/viewTransition";
import { useNow, useReducedMotion } from "../components/lineage/hooks";
import { expectedDuration, progressFraction, pushSample, tokenRateSeries, type TokenSample } from "../lib/workMotion";
import "../components/motion/work-motion.css";
import { VIEWS, filterOptions, filterWork, findWorkRoot, groupOf, GROUP_LABEL, latestAttempt, miniChain, modelsUsed, tokenTotal, workSummary, type Tokens, type WorkFilters } from "../lib/work";

/** The list endpoint may carry token totals; the sparkline only shows when it does. */
type WorkRow = WorkItem & { tokens?: Tokens | null };

const isRunning = (i: WorkItem) => latestAttempt(i)?.display.tone === "running" && latestAttempt(i)?.started_at != null;

/** Chain marks. Flow runs only on the connector into the in-progress stage (data: miniChain of live attempts). */
function Mini({ item, animate }: { item: WorkItem; animate: boolean }) {
  const marks = miniChain(item);
  return (
    <span className="mono" aria-label={`${marks.length} attempts`}>
      {marks.map((m, i) => (
        <span key={i}>
          {i > 0 && <span className={`wm-conn${m === "n" && animate ? " flow" : ""}`} aria-hidden="true"><i /></span>}
          {m === "d" ? "●" : m === "b" ? "✕" : "◐"}
        </span>
      ))}
    </span>
  );
}

/** Progress sheen for a running row: elapsed (now - started_at) against the expected duration. */
function Sheen({ item, now, expected, paused }: { item: WorkItem; now: number; expected: number; paused: boolean }) {
  const start = latestAttempt(item)?.started_at;
  if (start == null) return null;
  const p = progressFraction(now - start, expected);
  return <span className={`wm-sheen${paused ? " paused" : ""}`} aria-hidden="true"><i style={{ ["--p" as string]: p }} /><b /></span>;
}

/** Keeps token samples per running item across refetches (real data only); returns the tokens/sec series. */
function useTokenSeries(items: WorkRow[]): (id: string) => number[] {
  const store = useRef(new Map<string, TokenSample[]>());
  const [, bump] = useState(0);
  useEffect(() => {
    let changed = false;
    const t = Date.now() / 1000;
    for (const i of items) {
      if (!i.tokens || !isRunning(i)) continue;
      const prev = store.current.get(i.root_id) ?? [];
      const next = pushSample(prev, { t, total: tokenTotal(i.tokens) });
      if (next.length !== prev.length) { store.current.set(i.root_id, next); changed = true; }
    }
    if (changed) bump((n) => n + 1);
  }, [items]);
  return (id) => tokenRateSeries(store.current.get(id) ?? []);
}

/** Focus: the clicked row's title gets the shared view-transition-name, then navigation runs inside a view transition. */
function useFocusNav() {
  const navigate = useNavigate();
  return (root: string, e?: MouseEvent) => {
    e?.preventDefault();
    const el = document.querySelector<HTMLElement>(`[data-wt-root="${CSS.escape(root)}"]`);
    if (el) el.style.viewTransitionName = "work-title";
    withViewTransition(() => flushSync(() => navigate(`/work/${root}`)));
  };
}

/** Mounted once the list has loaded, so rows present then are the baseline and only later ones (or updates) Arrive. */
function WorkTable({ rows, all }: { rows: WorkRow[]; all: WorkRow[] }) {
  const goto = useFocusNav();
  const visible = useVisible();
  const reduced = useReducedMotion();
  const live = visible && !reduced;
  const isNew = useArrivals(all.map((i) => `${i.root_id}@${i.updated_at}`));
  const now = useNow(live && all.some(isRunning), 5000);
  const series = useTokenSeries(all);
  const expected = useMemo(() => {
    const d: number[] = [];
    for (const i of all) for (const a of i.attempts) if (a.started_at != null && a.ended_at != null) d.push(a.ended_at - a.started_at);
    return expectedDuration(d);
  }, [all]);

  const cols: Column<WorkRow>[] = [
    {
      key: "title", header: "Work", label: "Work",
      cell: (i) => (
        <Arrive id={`${i.root_id}@${i.updated_at}`} isNew={isNew}>
          <Link to={`/work/${i.root_id}`} data-wt-root={i.root_id} onClick={(e) => { if (!e.metaKey && !e.ctrlKey && !e.shiftKey && e.button === 0) { e.stopPropagation(); goto(i.root_id, e); } }}>
            <Truncate text={i.title || i.root_id} max={380} />
          </Link>
        </Arrive>
      ),
    },
    {
      key: "status", header: "Status",
      cell: (i) => (
        <>
          <StatusPill display={latestAttempt(i)?.display ?? i.display} /> <span className="muted">{GROUP_LABEL[groupOf(i)]}</span>
          {isRunning(i) && <Sparkline values={series(i.root_id)} />}
          {isRunning(i) && <Sheen item={i} now={now} expected={expected} paused={!live} />}
        </>
      ),
    },
    { key: "chain", header: "Chain", cell: (i) => <Mini item={i} animate={live && isRunning(i)} /> },
    { key: "models", header: "Models", cell: (i) => modelsUsed(i).join(", ") || "-" },
    { key: "upd", header: "Updated", cell: (i) => ago(i.updated_at) },
  ];
  return <Table columns={cols} rows={rows} rowKey={(i) => i.root_id} onRowClick={(i) => goto(i.root_id)} caption="Work items" />;
}

export function WorkPage() {
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const q = useWork(true);
  const items: WorkRow[] = q.data ?? [];
  const f: WorkFilters = { q: sp.get("q") ?? "", model: sp.get("model") ?? "", account: sp.get("account") ?? "", kind: sp.get("kind") ?? "", view: sp.get("view") ?? "all" };
  const set = (k: string, v: string) => setSp((p) => {
    const n = new URLSearchParams(p);
    if (v && !(k === "view" && v === "all")) n.set(k, v); else n.delete(k);
    return n;
  }, { replace: true });

  const task = sp.get("task");
  const target = useMemo(() => (task ? findWorkRoot(items, task) : null), [items, task]);
  useEffect(() => { if (target) navigate(`/work/${target}`, { replace: true }); }, [target, navigate]);

  const opts = useMemo(() => filterOptions(items), [items]);
  const rows = useMemo(() => [...filterWork(items, f)].sort((a, b) => b.updated_at - a.updated_at), [items, sp]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <PageHead title="Work" intro={workSummary(items)} />
      <div className="row" style={{ flexWrap: "wrap", gap: 8, marginBottom: 12 }}>
        <Input type="search" aria-label="Search work" placeholder="Search work" value={f.q} onChange={(e) => set("q", e.target.value)} />
        <Select aria-label="View" value={f.view} onChange={(e) => set("view", e.target.value)}>{VIEWS.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}</Select>
        <Select aria-label="Model" value={f.model} onChange={(e) => set("model", e.target.value)}><option value="">All models</option>{opts.models.map((m) => <option key={m}>{m}</option>)}</Select>
        <Select aria-label="Account" value={f.account} onChange={(e) => set("account", e.target.value)}><option value="">All accounts</option>{opts.accounts.map((m) => <option key={m}>{m}</option>)}</Select>
        <Select aria-label="Kind" value={f.kind} onChange={(e) => set("kind", e.target.value)}><option value="">All kinds</option>{opts.kinds.map((m) => <option key={m}>{m}</option>)}</Select>
      </div>
      <Card>
        {q.isLoading ? <Loading /> : q.error ? <ErrorState error={q.error} /> : rows.length === 0
          ? <Empty title="No work matches">Clear a filter or start a task.</Empty>
          : <WorkTable rows={rows} all={items} />}
      </Card>
    </>
  );
}
