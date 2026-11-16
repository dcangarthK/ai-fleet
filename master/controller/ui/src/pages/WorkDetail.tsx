import { useEffect, useMemo, useState } from "react";
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
import { chainSummary, deriveChain, gateRounds, parseFindings, sameModelWarning, tokenTotal, VERDICT_LABEL, parseVerdict, type DetailAttempt, type WorkDetail } from "../lib/work";

const ACTIVE = new Set(["queued", "starting", "working"]);
const n = (x: number) => x.toLocaleString();

export function WorkDetailPage() {
  const { root = "" } = useParams();
  const q = useQuery({ queryKey: ["work-detail", root], queryFn: () => get<WorkDetail>(`/api/work/${encodeURIComponent(root)}`), enabled: !!root });
  const d = q.data;
  const [sel, setSel] = useState<string | null>(null);
  const attempts = d?.attempts ?? [];
  useEffect(() => { setSel(null); }, [root]);
  const chain = useMemo(() => (d ? deriveChain(d) : []), [d]);
  const current: DetailAttempt | undefined = attempts.find((a) => a.id === sel) ?? attempts[attempts.length - 1];
  const log = useTaskLog(current?.id, !!current && ACTIVE.has(current.state));

  if (q.isLoading) return <Loading />;
  if (q.error || !d) return <ErrorState error={q.error} title="Couldn't load this work item" />;

  const findings = parseFindings(current?.result_excerpt);
  const verdict = parseVerdict(current?.result_excerpt);
  const warn = sameModelWarning(chain);
  const gate = gateRounds(attempts);

  const cols: Column<DetailAttempt>[] = [
    { key: "id", header: "Attempt", cell: (a) => <button type="button" className="linklike mono" onClick={() => setSel(a.id)}>{a.id}</button> },
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
        actions={<Link to={`/lineage/${d.root_id}`}>Trace</Link>}
      />
      <div className="stack" style={{ gap: 16 }}>
        {warn && <div role="alert"><Badge tone="warn">Same model</Badge> {warn}</div>}
        <Card><ChainStrip chain={chain} selected={current?.id ?? null} onSelect={setSel} /></Card>
        <Card>
          <h2>Attempts</h2>
          {attempts.length === 0 ? <Empty title="No attempts yet" /> : <Table columns={cols} rows={attempts} rowKey={(a) => a.id} selectedKey={current?.id} caption="Attempts" />}
        </Card>
        {current && (
          <>
            <Card>
              <h2>Log <span className="muted mono">{current.id}</span></h2>
              <Terminal title={`${current.node ?? "node"}:~/worktree${current.branch ? ` (${current.branch})` : ""}`} text={log} live={ACTIVE.has(current.state)} label={`Log of ${current.id}`} />
            </Card>
            <Card>
              <h2>Gate</h2>
              {gate.rounds === 0 ? <p className="muted">No gate has run.</p> : (
                <>
                  <p>{gate.rounds} round{gate.rounds > 1 ? "s" : ""}, {gate.failed} failed (max {gate.max}). Latest: {gate.last?.gate?.passed ? "passed" : gate.last?.gate?.passed === false ? "failed" : "running"}.</p>
                  {gate.last?.gate?.tail && <pre className="mono" style={{ overflow: "auto", maxHeight: 220 }}>{gate.last.gate.tail}</pre>}
                </>
              )}
            </Card>
            <Card>
              <h2>Findings {verdict && <Badge>{VERDICT_LABEL[verdict]}</Badge>}</h2>
              {findings.length === 0 ? <p className="muted">No HIGH, MED or LOW findings in this attempt's result.</p> : (
                <ul>{findings.map((f, i) => <li key={i}><Badge tone={f.severity === "HIGH" ? "bad" : f.severity === "MED" ? "warn" : "quiet"}>{f.severity}</Badge> {f.title}{f.detail && <span className="muted"> {f.detail}</span>}</li>)}</ul>
              )}
            </Card>
            <Card>
              <h2>Tokens</h2>
              {tokenTotal(d.tokens) === 0 ? <p className="muted">No token data.</p>
                : <p>Whole item: {n(tokenTotal(d.tokens))} total ({n(d.tokens.input)} in, {n(d.tokens.output)} out, {n(d.tokens.cache_read)} cache read, {n(d.tokens.cache_write)} cache write, {n(d.tokens.turns)} turns).{current.tokens && <> This attempt: {n(tokenTotal(current.tokens))}.</>}</p>}
            </Card>
            <Card><h2>Actions</h2><Actions root={d.root_id} attempt={current} repo={d.repo} /></Card>
          </>
        )}
        <Card><h2>Ask</h2><Ask root={d.root_id} /></Card>
      </div>
    </>
  );
}
