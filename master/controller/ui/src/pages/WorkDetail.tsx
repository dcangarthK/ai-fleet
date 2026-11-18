import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { get } from "../lib/api";
import { useTaskLog } from "../lib/data";
import { ago, duration } from "../lib/format";
import { PageHead } from "../components/bits";
import { Badge, Card, Empty, ErrorState, Loading, Table, Truncate, type Column } from "../components/ui";
import { Terminal } from "../components/Terminal";
import { ChainStrip } from "../components/work/ChainStrip";
import { Actions } from "../components/work/Actions";
import { Ask } from "../components/work/Ask";
import { DiffTree } from "../components/work/DiffTree";
import { WhyPanel } from "../components/work/WhyPanel";
import { useArrivals } from "../components/motion/Arrive";
import { Sparkline } from "../components/motion/Sparkline";
import { parseGateResults, pushSample, settleOrder, severityOrder, staggerDelay, tokenRateSeries, type TokenSample } from "../lib/workMotion";
import "../components/motion/work-motion.css";
import { chainSummary, deriveChain, gateRounds, parseFindings, sameModelWarning, tokenTotal, VERDICT_LABEL, parseVerdict, type DetailAttempt, type Finding, type WorkDetail } from "../lib/work";

const ACTIVE = new Set(["queued", "starting", "working"]);
const n = (x: number) => x.toLocaleString();

/** Settle: gate results turn green or red one by one. Remount per attempt (key) so only results that appear later animate. */
function GateResults({ attemptId, tail }: { attemptId: string; tail: string }) {
  const results = useMemo(() => settleOrder(parseGateResults(tail)), [tail]);
  const isNew = useArrivals(results.map((r) => `${attemptId}:${r.name}`));
  if (results.length === 0) return null;
  let k = 0;
  return (
    <ul className="wm-gate" aria-label="Gate results">
      {results.map((r) => {
        const fresh = isNew(`${attemptId}:${r.name}`);
        return (
          <li key={r.name} className={`${r.ok ? "ok" : "bad"}${fresh ? " settle" : ""}`} style={fresh ? { animationDelay: `${staggerDelay(k++, 70, 700)}ms` } : undefined}>
            <svg className="mark" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              {r.ok ? <path d="M3 8.5l3.2 3.2L13 5" /> : <path d="M4 4l8 8M12 4l-8 8" />}
            </svg>
            <span>{r.ok ? "Passed" : "Failed"}</span> <span className="muted">{r.name}</span>
          </li>
        );
      })}
    </ul>
  );
}

/** Findings in severity order; ones that appear after first render Arrive one by one. Remount per attempt (key). */
function FindingList({ attemptId, findings }: { attemptId: string; findings: Finding[] }) {
  const ordered = useMemo(() => severityOrder(findings), [findings]);
  const idOf = (f: Finding) => `${attemptId}:${f.severity}:${f.title}`;
  const isNew = useArrivals(ordered.map(idOf));
  let k = 0;
  return (
    <ul className="wm-findings">
      {ordered.map((f) => {
        const fresh = isNew(idOf(f));
        return (
          <li key={idOf(f)} className={fresh ? "arrive" : undefined} style={fresh ? { animationDelay: `${staggerDelay(k++)}ms` } : undefined}>
            <Badge tone={f.severity === "HIGH" ? "bad" : f.severity === "MED" ? "warn" : "quiet"}>{f.severity}</Badge> {f.title}{f.detail && <span className="muted"> {f.detail}</span>}
          </li>
        );
      })}
    </ul>
  );
}

/** Token samples from successive refetches of the detail (real data), only while an attempt is active. */
function useTokenSamples(root: string, total: number, active: boolean): number[] {
  const samples = useRef<TokenSample[]>([]);
  const [series, setSeries] = useState<number[]>([]);
  useEffect(() => { samples.current = []; setSeries([]); }, [root]);
  useEffect(() => {
    if (!active) return;
    samples.current = pushSample(samples.current, { t: Date.now() / 1000, total });
    setSeries(tokenRateSeries(samples.current));
  }, [total, active]);
  return series;
}

export function WorkDetailPage() {
  const { root = "" } = useParams();
  const q = useQuery({ queryKey: ["work-detail", root], queryFn: () => get<WorkDetail>(`/api/work/${encodeURIComponent(root)}`), enabled: !!root,
    // Poll faster while something is running so gates, findings and tokens show up as they happen.
    refetchInterval: (query) => ((query.state.data?.attempts ?? []).some((a) => ACTIVE.has(a.state)) ? 5000 : 30_000) });
  const d = q.data;
  const [sel, setSel] = useState<string | null>(null);
  const attempts = d?.attempts ?? [];
  useEffect(() => { setSel(null); }, [root]);
  const chain = useMemo(() => (d ? deriveChain(d) : []), [d]);
  const current: DetailAttempt | undefined = attempts.find((a) => a.id === sel) ?? attempts[attempts.length - 1];
  const log = useTaskLog(current?.id, !!current && ACTIVE.has(current.state));
  const tokenSeries = useTokenSamples(root, tokenTotal(d?.tokens), !!current && ACTIVE.has(current.state));
  // Focus: the heading takes the name the clicked Work row title carried, so the view transition morphs one into the other.
  useEffect(() => {
    const h = document.querySelector<HTMLElement>(".page-head h1");
    if (h) h.style.viewTransitionName = "work-title";
    return () => { if (h) h.style.viewTransitionName = ""; };
  }, [d?.root_id]);

  if (q.isLoading) return <Loading />;
  if (q.error || !d) return <ErrorState error={q.error} title="Couldn't load this work item" />;

  const findings = parseFindings(current?.result_excerpt);
  const verdict = parseVerdict(current?.result_excerpt);
  const warn = sameModelWarning(chain);
  const gate = gateRounds(attempts);

  const cols: Column<DetailAttempt>[] = [
    { key: "id", header: "Attempt", cell: (a) => <a href={`#${a.id}`} className="mono" onClick={(e) => { e.preventDefault(); setSel(a.id); }}>{a.id}</a> },
    { key: "stage", header: "Stage", cell: (a) => a.stage || a.kind || "build" },
    { key: "state", header: "State", cell: (a) => a.state.replace(/_/g, " ") },
    { key: "model", header: "Model", cell: (a) => a.model || a.agent },
    { key: "acct", header: "Account", cell: (a) => a.account_label || a.account || "-" },
    { key: "when", header: "When", cell: (a) => (a.created_at ? `${ago(a.created_at)}${a.ended_at ? ` · ${duration(a.ended_at - (a.created_at ?? 0))}` : ""}` : "-") },
  ];

  return (
    <>
      <PageHead
        title={d.title || d.root_id}
        intro={<><Truncate text={d.root_id} className="mono" /> · {chainSummary(chain)}</>}
        actions={<Link className="button-link" to={`/lineage/${d.root_id}`}>Open trace</Link>}
      />
      <div className="stack stack-lg">
        {warn && <div role="alert"><Badge tone="warn">Same model</Badge> {warn}</div>}
        <Card><ChainStrip chain={chain} selected={current?.id ?? null} onSelect={setSel} /></Card>
        <Card>
          <h2>Attempts</h2>
          {attempts.length === 0 ? <Empty title="No attempts yet" /> : <Table columns={cols} rows={attempts} rowKey={(a) => a.id} selectedKey={current?.id} caption="Attempts" />}
        </Card>
        {d.truncated && <div role="status"><Badge tone="warn">Truncated</Badge> Showing the first {attempts.length} attempts only.</div>}
        <Card><h2>Why?</h2><WhyPanel attempts={attempts} /></Card>
        {current && (
          <>
            <Card>
              <h2>Log <span className="muted mono">{current.id}</span></h2>
              <Terminal title={`${current.node ?? "node"}:~/worktree${current.branch ? ` (${current.branch})` : ""}`} text={log} live={ACTIVE.has(current.state)} label={`Log of ${current.id}`} />
            </Card>
            <Card><h2>Changes <span className="muted mono">{current.id}</span></h2><DiffTree taskId={current.id} /></Card>
            <Card>
              <h2>Gate</h2>
              {gate.rounds === 0 ? <p className="muted">No gate has run.</p> : (
                <>
                  <p>{gate.rounds} round{gate.rounds > 1 ? "s" : ""}, {gate.failed} failed (max {gate.max}). Latest: {gate.last?.gate?.passed ? "passed" : gate.last?.gate?.passed === false ? "failed" : "running"}.</p>
                  {gate.last?.gate?.tail && <GateResults key={gate.last.id} attemptId={gate.last.id} tail={gate.last.gate.tail} />}
                  {gate.last?.gate?.tail && <Terminal title={`gate: ${gate.last.node ?? "node"}`} text={gate.last.gate.tail} height={220} idPrefix="gate-" label="Gate output" />}
                </>
              )}
            </Card>
            <Card>
              <h2>Findings {verdict && <Badge>{VERDICT_LABEL[verdict]}</Badge>}</h2>
              {findings.length === 0 ? <p className="muted">No HIGH, MED or LOW findings in this attempt's result.</p> : (
                <FindingList key={current.id} attemptId={current.id} findings={findings} />
              )}
            </Card>
            <Card title="Tokens">
              {tokenTotal(d.tokens) === 0 ? <p className="muted">No token data.</p>
                : <div className="stats"><div className="stat"><span>Whole item</span><b>{n(tokenTotal(d.tokens))}</b><Sparkline values={tokenSeries} /></div><div className="stat"><span>This attempt</span><b>{n(tokenTotal(current.tokens))}</b></div><div className="stat"><span>Input / output</span><b>{n(d.tokens.input)} / {n(d.tokens.output)}</b></div><div className="stat"><span>Cache / turns</span><b>{n(d.tokens.cache_read + d.tokens.cache_write)} / {n(d.tokens.turns)}</b></div></div>}
            </Card>
            <Card><h2>Actions</h2><Actions root={d.root_id} attempt={current} repo={d.repo} /></Card>
          </>
        )}
        <Card><h2>Ask</h2><Ask root={d.root_id} /></Card>
      </div>
    </>
  );
}
