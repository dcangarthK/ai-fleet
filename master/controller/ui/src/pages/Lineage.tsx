import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { get } from "../lib/api";
import {
  criticalPath, dataExtent, findAnomalies, findBottleneck, flagMap, fmtClock, rootsOf, snapshotAt,
  stepTimes, fmtDur, fmtTokens, spanOf, tokenTotal, type LineageData,
} from "../lib/lineageGraph";
import { STATE } from "../lib/format";
import { PageHead } from "../components/bits";
import { Button, Card, Empty, ErrorState, Loading, Select, TabPanel, Tabs } from "../components/ui";
import { River, RiverPanel } from "../components/lineage/River";
import { Graph } from "../components/lineage/Graph";
import { TraceGraph, TracePanel } from "../components/lineage/Trace";
import { Agents } from "../components/lineage/Agents";
import { Intel } from "../components/lineage/Intel";
import { Scrubber } from "../components/lineage/Scrubber";
import { useNow, useReducedMotion, useTimeView } from "../components/lineage/hooks";
import "../components/lineage/lineage.css";

const RANGES = [
  { id: "1h", label: "Last hour", sec: 3600 },
  { id: "6h", label: "Last 6 hours", sec: 6 * 3600 },
  { id: "24h", label: "Last 24 hours", sec: 24 * 3600 },
  { id: "7d", label: "Last 7 days", sec: 7 * 86400 },
];
const LENSES = [
  { id: "graph", label: "Graph" },
  { id: "river", label: "Fleet river" },
  { id: "trace", label: "Trace" },
  { id: "agents", label: "Agents" },
];
type Lens = "graph" | "river" | "trace" | "agents";

export function LineagePage() {
  const { root } = useParams();
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const reduced = useReducedMotion();
  const lens: Lens = root ? "trace" : sp.get("lens") === "river" ? "river" : sp.get("lens") === "agents" ? "agents" : sp.get("lens") === "trace" ? "trace" : "graph";
  const range = RANGES.find((r) => r.id === sp.get("range")) ?? RANGES[1];
  const sel = sp.get("sel");
  const [cursor, setCursor] = useState<number | null>(null);
  const [grouped, setGrouped] = useState(true);
  const [resetVersion, setResetVersion] = useState(0);

  const query = useQuery({
    queryKey: ["lineage", root ?? range.id],
    queryFn: () => get<LineageData>(root ? `/api/lineage?root=${encodeURIComponent(root)}` : `/api/lineage?since=${Math.floor(Date.now() / 1000 - range.sec)}`),
  });
  const data = query.data;
  const now = useNow(cursor === null, reduced ? 5000 : 1000);
  const nodes = data?.nodes ?? [];
  const events = data?.events ?? [];

  const extent = useMemo(() => dataExtent(nodes, data?.now ?? now), [data]); // eslint-disable-line react-hooks/exhaustive-deps
  const tv = useTimeView(extent, now, true);
  useEffect(() => { setCursor(null); }, [root, range.id]);
  useEffect(() => { if (cursor !== null && tv.follow) tv.setFollow(false); }, [cursor]); // eslint-disable-line react-hooks/exhaustive-deps

  const effNow = cursor ?? now;
  const snap = useMemo(() => (cursor === null ? nodes : snapshotAt(nodes, events, cursor)), [nodes, events, cursor]);
  const steps = useMemo(() => stepTimes(events), [events]);
  const anomalies = useMemo(() => findAnomalies(snap, events.filter((e) => cursor === null || e.ts <= cursor)), [snap, events, cursor]);
  const flags = useMemo(() => flagMap(anomalies), [anomalies]);
  const bottleneck = useMemo(() => findBottleneck(snap, effNow), [snap, effNow]);
  const roots = useMemo(() => rootsOf(nodes, now), [nodes, now]);
  const critical = useMemo(() => criticalPath(nodes, now), [nodes, now]);

  const setParam = (k: string, v: string | null) => setSp((p) => { const n = new URLSearchParams(p); if (v === null) n.delete(k); else n.set(k, v); return n; }, { replace: true });
  const onLens = (id: string) => {
    if (id === "trace") { if (!root) setParam("lens", "trace"); return; }
    const q = new URLSearchParams(sp);
    q.delete("sel");
    if (id === "graph") q.delete("lens"); else q.set("lens", id);
    navigate({ pathname: "/lineage", search: q.toString() ? `?${q}` : "" });
  };

  const selectedId = lens === "trace" ? sel ?? critical[critical.length - 1] ?? null : sel;
  const selected = snap.find((n) => n.id === selectedId) ?? nodes.find((n) => n.id === selectedId) ?? null;
  const t0 = Math.floor(extent.t0), t1 = Math.ceil(Math.max(extent.t1, cursor ?? 0, now));

  const live = nodes.filter((n) => n.ended_at === null).length;
  const intro = root
    ? <>Everything that happened to <span className="mono">{root}</span>, step by step.</>
    : "The flight recorder: every attempt on every node and account, how they link, and what held them up.";

  return (
    <>
      <PageHead
        title="Lineage"
        intro={intro}
        actions={root ? <Link className="lin-openlink" to={`/work/${root}`}>Open work item</Link> : undefined}
      />

      <div className="lin-controls">
        <Tabs tabs={LENSES.map((l) => ({ ...l, label: l.label, count: l.id === "agents" || !data ? undefined : l.id === "river" ? nodes.length : root ? nodes.length : roots.length }))}
          value={lens} onChange={onLens} label="Lineage lenses" idPrefix="lin" />
        {!root && (
          <label className="lin-range">
            <span className="sr-only">Time range</span>
            <Select value={range.id} onChange={(e) => setParam("range", e.target.value === "6h" ? null : e.target.value)} aria-label="Time range">
              {RANGES.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
            </Select>
          </label>
        )}
      </div>

      {query.isLoading && <Card><Loading rows={7} label="Loading lineage" /></Card>}
      {query.isError && <Card><ErrorState error={query.error} onRetry={() => query.refetch()} title="Couldn't load lineage" /></Card>}

      {data && (
        <>
          {data.truncated && <div className="banner note" role="status">Showing the newest 400 attempts in this window. Narrow the range to see the rest.</div>}

          {nodes.length === 0 ? (
            <Card>
              <Empty
                title={root ? "Nothing recorded for this work item" : "No work in this window"}
                action={!root && range.id !== "7d" ? <Button onClick={() => setParam("range", "7d")}>Widen to 7 days</Button> : undefined}
              >
                {root ? "The controller has no attempts under this id." : "Attempts show up here the moment a task is queued."}
              </Empty>
            </Card>
          ) : (
            <>
              <Scrubber t0={t0} t1={t1} steps={steps} events={events} nodes={nodes} cursor={cursor} setCursor={setCursor} />
              <Summary live={live} nodes={snap.length} lanes={data.lanes.length} roots={roots.length} cursor={cursor} root={root} />

              <TabPanel id="graph" value={lens} idPrefix="lin">
                <div className="graph-layout"><div><Graph nodes={snap} edges={data.edges} now={effNow} selected={sel} onSelect={(n) => setParam("sel", n?.id ?? null)} /></div>{selected ? <RiverPanel node={selected} now={effNow} onClose={() => setParam("sel", null)} /> : <details className="graph-insights"><summary>Insights</summary><Intel nodes={snap} bottleneck={bottleneck} anomalies={anomalies} narrative={null} /></details>}</div>
              </TabPanel>

              <TabPanel id="river" value={lens} idPrefix="lin">
                <LineageInsight nodes={snap} critical={critical} now={effNow} />
                <Toolbar tv={tv} cursor={cursor} grouped={grouped} setGrouped={setGrouped} onFit={() => setResetVersion((v) => v + 1)} />
                <div className="lin-river-layout">
                  <Card className="lin-chart-card" bodyClassName="lin-chart-body">
                    <River
                      nodes={snap} edges={data.edges} lanes={data.lanes} events={events} now={now} view={tv.view} follow={tv.follow}
                      cursor={cursor} flags={flags} selected={sel} grouped={grouped} resetVersion={resetVersion} zoom={tv.zoom} pan={tv.pan}
                      onOpen={(n) => setParam("sel", n?.id ?? null)}
                    />
                  </Card>
                  {selected && <RiverPanel node={selected} now={effNow} onClose={() => setParam("sel", null)} />}
                </div>
                <Legend />
              </TabPanel>

              <TabPanel id="agents" value={lens} idPrefix="lin">
                <Toolbar tv={tv} cursor={cursor} />
                <Card className="lin-chart-card" bodyClassName="lin-chart-body">
                  <Agents nodes={snap} now={now} view={tv.view} follow={tv.follow} cursor={cursor} zoom={tv.zoom} pan={tv.pan} />
                </Card>
                <p className="quiet lin-hint">Bars under each strip are busy time; columns are tokens per minute, spread evenly over each attempt's run; dotted lines are idle gaps of a minute or more.</p>
              </TabPanel>

              <TabPanel id="trace" value={lens} idPrefix="lin">
                {root ? (
                  <div className="lin-trace-layout">
                    <Card title="Trace" actions={<span className="quiet lin-crit-key"><i aria-hidden="true" />critical path</span>} className="lin-chart-card" bodyClassName="lin-chart-body">
                      <TraceGraph
                        all={nodes} visible={snap} edges={data.edges} now={effNow} events={events} cursor={cursor}
                        selected={selectedId} onSelect={(id) => setParam("sel", id)}
                      />
                    </Card>
                    <Card title={selected ? "Step details" : "Step details"} className="lin-side">
                      {selected ? <TracePanel key={selected.id} node={selected} now={effNow} events={events} /> : <Empty title="Pick a step">Select a step to see its log, gate and tokens.</Empty>}
                    </Card>
                  </div>
                ) : (
                  <Card title="Work items in view" bodyClassName="lin-pick-body">
                    <p className="quiet">Pick one to trace it end to end.</p>
                    <ul className="lin-pick">
                      {roots.map((r) => (
                        <li key={r.root}>
                          <Link to={`/lineage/${r.root}`}>
                            <span className="mono">{r.root}</span>
                            <span className="lin-pick-title">{r.title}</span>
                            <span className={`state tone-${r.active ? "accent" : r.failed ? "bad" : "ok"}`}>{r.active ? "Active" : r.failed ? "Problems" : "Done"}</span>
                            <span className="quiet">{r.count} attempt{r.count === 1 ? "" : "s"} · {fmtDur(r.last - r.first)} · {fmtClock(r.first)}</span>
                          </Link>
                        </li>
                      ))}
                    </ul>
                  </Card>
                )}
              </TabPanel>
            </>
          )}
        </>
      )}
    </>
  );
}

function Summary({ live, nodes, lanes, roots, cursor, root }: { live: number; nodes: number; lanes: number; roots: number; cursor: number | null; root?: string }) {
  return (
    <p className="lin-summary" role="status">
      <b>{nodes}</b> attempt{nodes === 1 ? "" : "s"}{root ? "" : <> across <b>{lanes}</b> lane{lanes === 1 ? "" : "s"} and <b>{roots}</b> work item{roots === 1 ? "" : "s"}</>}
      {cursor === null ? <> · <b>{live}</b> running or waiting now</> : <> · replay at {fmtClock(cursor, false, true)}</>}
    </p>
  );
}

function Toolbar({ tv, cursor, grouped, setGrouped, onFit }: { tv: ReturnType<typeof useTimeView>; cursor: number | null; grouped?: boolean; setGrouped?: (grouped: boolean) => void; onFit?: () => void }) {
  return (
    <div className="lin-toolbar row" role="toolbar" aria-label="Time axis">
      <Button size="sm" variant={tv.follow ? "primary" : "secondary"} aria-pressed={tv.follow} disabled={cursor !== null} onClick={() => tv.setFollow(!tv.follow)}>
        {tv.follow ? "Following now" : "Follow now"}
      </Button>
      <Button size="sm" onClick={() => tv.zoom(0.5, 0.5)} aria-label="Zoom in">Zoom +</Button>
      <Button size="sm" onClick={() => tv.zoom(2, 0.5)} aria-label="Zoom out">Zoom −</Button>
      <Button size="sm" onClick={() => { tv.fit(); onFit?.(); }}>Fit all</Button>
      <Button size="sm" variant="ghost" onClick={() => tv.pan(-(tv.view.t1 - tv.view.t0) * 0.4)} aria-label="Pan earlier">‹ Earlier</Button>
      <Button size="sm" variant="ghost" onClick={() => tv.pan((tv.view.t1 - tv.view.t0) * 0.4)} aria-label="Pan later">Later ›</Button>
      {setGrouped && <Button size="sm" variant={grouped ? "primary" : "secondary"} aria-pressed={grouped} onClick={() => setGrouped(!grouped)}>Group by work item</Button>}
      <span className="quiet lin-hint-inline">Drag to pan · Ctrl/⌘ + scroll or pinch to zoom · {fmtDur(tv.view.t1 - tv.view.t0)} in view</span>
    </div>
  );
}

function LineageInsight({ nodes, critical, now }: { nodes: import("../lib/lineageGraph").LNode[]; critical: string[]; now: number }) {
  const gateNodes = nodes.filter((n) => n.gate?.passed !== null && n.gate?.passed !== undefined);
  const passRate = gateNodes.length ? Math.round((gateNodes.filter((n) => n.gate?.passed).length / gateNodes.length) * 100) : null;
  const retries = nodes.filter((n) => ["retry", "handoff", "resume"].includes(n.link ?? "")).length;
  const slowest = [...nodes].sort((a, b) => {
    const as = spanOf(a, now), bs = spanOf(b, now);
    return (bs.start === null ? bs.wait : bs.end - bs.start) - (as.start === null ? as.wait : as.end - as.start);
  })[0];
  return <div className="lin-insight-strip" aria-label="Lineage insight">
    <span><b>{critical.length}</b> critical steps</span><span><b>{retries}</b> retries</span><span><b>{passRate === null ? "—" : `${passRate}%`}</b> gate pass</span><span><b>{fmtTokens(nodes.reduce((sum, node) => sum + tokenTotal(node.tokens), 0))}</b> tokens</span><span><b>{slowest ? slowest.stage ?? slowest.kind ?? "task" : "—"}</b> slowest stage</span>
  </div>;
}

function Legend() {
  const states = ["working", "done", "failed", "review", "awaiting_approval", "queued"] as const;
  return (
    <ul className="lin-legend" aria-label="Legend">
      {states.map((s) => <li key={s}><i className={`sw t-${STATE[s].tone}`} aria-hidden="true" />{STATE[s].label}</li>)}
      <li><i className="sw hatch" aria-hidden="true" />Waiting in queue</li>
      <li><svg width="12" height="12" viewBox="-7 -7 14 14" aria-hidden="true" className="lin-diamond d-limit"><path d="M0 -6L6 0L0 6L-6 0z" /></svg>Limit hit</li>
      <li><svg width="12" height="12" viewBox="-7 -7 14 14" aria-hidden="true" className="lin-diamond d-stall"><path d="M0 -6L6 0L0 6L-6 0z" /></svg>Stall</li>
      <li><svg width="12" height="12" viewBox="-7 -7 14 14" aria-hidden="true" className="lin-diamond d-gate"><path d="M0 -6L6 0L0 6L-6 0z" /></svg>Gate failed</li>
      <li><span className="lin-legend-flag" aria-hidden="true">!</span>Anomaly</li>
    </ul>
  );
}
