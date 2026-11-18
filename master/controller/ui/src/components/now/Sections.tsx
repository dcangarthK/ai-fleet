import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Card, CardGrid, Confidence, Empty, EvidenceLinks, InsightCard, StatusPill, Table, Truncate, cx, type Column } from "../ui";
import { api } from "../../lib/api";
import { accountTone, alertText, machineWarnings, queueText, workerText, type Account, type Alert, type Live, type Machine, type QueueRow, type Worker } from "../../lib/live";
import { ago, duration } from "../../lib/format";
import { anomalies as findAnomalies, type Anomaly, type Briefing as BriefingData, type Forecast, type Proposal } from "../../lib/insights";
import { RequestPreview } from "./Request";
import { Arrive, useArrivals } from "../motion/Arrive";
import { useCountUp } from "../motion/useCountUp";
import "../motion/now-motion.css";

/** A number that counts from its previous value to `value` (the data source); snaps under reduced motion. */
export function Count({ value }: { value: number }) {
  return <span className="count-num">{Math.round(useCountUp(value))}</span>;
}

/** The briefing reveals sentence by sentence on the first load of the session only. */
let briefingRevealed = false;

export function Section({ title, children }: { title: string; children: ReactNode }) {
  return <section><h2 className="sec">{title}</h2>{children}</section>;
}

export function Briefing({ data }: { data: BriefingData }) {
  const reveal = useRef(!briefingRevealed);
  useEffect(() => { briefingRevealed = true; }, []);
  return (
    <Card title="Briefing">
      <div className="brief">
        <p>{reveal.current
          ? data.sentences.map((t, i) => <span key={i} className="reveal" style={{ "--i": i } as CSSProperties}>{t}{i < data.sentences.length - 1 ? " " : ""}</span>)
          : data.sentences.join(" ")}</p>
        <div className="brief-foot">
          <Confidence value={data.confidence} />
          <span>Derived from live state, budget status and tasks. No model was called.</span>
          <EvidenceLinks items={data.evidence} />
        </div>
      </div>
    </Card>
  );
}

/** Ranked proposals. Every button goes through InsightCard's useConfirm(); `run` is only called after Confirm.
 *  Cards that appear after first load Arrive; a confirmed action Settles (check draws in, or one 2px shake on failure). */
export function NextActions({ items, run }: { items: Proposal[]; run: (fn: () => Promise<unknown>) => Promise<unknown> }) {
  const isNew = useArrivals(items.map((p) => p.id));
  const [settled, setSettled] = useState<Record<string, "ok" | "bad">>({});
  if (!items.length) return <Card><Empty title="No suggested actions">Nothing is waiting on a decision. New proposals appear here as work changes state.</Empty></Card>;
  return (
    <CardGrid min={320}>
      {items.map((p, i) => {
        const st = settled[p.id];
        return (
          <Arrive key={p.id} id={p.id} isNew={isNew} className={st ? `settle-wrap ${st}` : "settle-wrap"}>
            <InsightCard
              kind={`${i + 1}. ${p.kind}`} title={p.title} tone={p.tone} confidence={p.confidence} evidence={p.evidence}
              why={<>{p.why}<span className="cost">Expected cost: {p.cost}</span></>}
              action={p.action && {
                label: p.action.label, danger: p.action.danger,
                confirmMessage: <RequestPreview request={p.action.request} note={<>{p.why} Nothing is sent until you confirm.</>} />,
                run: async () => {
                  let ok = false;
                  await run(async () => { const r = await api(p.action!.request.method, p.action!.request.path, p.action!.request.body); ok = true; return r; });
                  setSettled((m) => ({ ...m, [p.id]: ok ? "ok" : "bad" }));
                },
              }}
            />
            {st && (
              <svg className="settle-mark" viewBox="0 0 20 20" aria-hidden="true">
                <path d={st === "ok" ? "M4 10.5l4 4 8-9" : "M5 5l10 10M15 5L5 15"} />
              </svg>
            )}
          </Arrive>
        );
      })}
    </CardGrid>
  );
}

export function Forecasts({ items }: { items: Forecast[] }) {
  if (!items.length) return <Card><Empty title="Nothing to forecast">No budgets, queue or chains in flight.</Empty></Card>;
  return (
    <CardGrid min={260}>
      {items.map((f) => (
        <article key={f.id} className={cx("fc", f.tone === "warn" && "warn", f.tone === "bad" && "bad")}>
          <span className="nm">{f.label}</span>
          <span className="big">{f.headline}</span>
          <p>{f.detail}</p>
          <div className="foot"><Confidence value={f.confidence} /><EvidenceLinks items={f.evidence} /></div>
        </article>
      ))}
    </CardGrid>
  );
}

export function Anomalies({ items }: { items: Anomaly[] }) {
  if (!items.length) return <Card><Empty title="No anomalies">No stalls, flapping nodes, repeated gate failures or token spikes right now.</Empty></Card>;
  return (
    <CardGrid min={320}>
      {items.map((a) => <InsightCard key={a.id} kind="Anomaly" title={a.title} why={a.detail} tone={a.severity} confidence={a.confidence} evidence={a.evidence} />)}
    </CardGrid>
  );
}

export { findAnomalies };

const none = (what: string) => <Empty title={`No ${what}`} />;

export function Operations({ live }: { live: Live }) {
  const now = live.generated_at;
  const running = live.workers.filter((w) => w.task_id);
  const runCols: Column<Worker>[] = [
    { key: "t", header: "Task", cell: (w) => <Link to={`/work/${w.work_item ?? w.task_id}`} title={w.task_id ?? ""}>{workerText(w, now).primary}</Link>, truncate: true },
    { key: "m", header: "Model", label: "Model", cell: (w) => workerText(w, now).secondary, truncate: true },
    { key: "k", header: "Stage", cell: (w) => workerText(w, now).meta, hideStacked: true },
    { key: "e", header: "Elapsed", numeric: true, cell: (w) => duration(w.elapsed_s) },
  ];
  const queueCols: Column<QueueRow>[] = [
    { key: "t", header: "Queued", cell: (q) => <Truncate text={q.title} />, truncate: true },
    { key: "w", header: "Waiting on", cell: (q) => queueText(q) },
    { key: "a", header: "Age", numeric: true, cell: (q) => duration(q.queued_s) },
  ];
  const machineCols: Column<Machine>[] = [
    { key: "n", header: "Machine", cell: (m) => <Link to={`/nodes/${m.node}`}>{m.name ?? m.node}</Link>, truncate: true },
    { key: "s", header: "State", cell: (m) => {
      const w = machineWarnings(m);
      return w.length ? <>{w.map((x) => <StatusPill key={x.label} tone={x.tone}>{x.label}</StatusPill>)}</> : <StatusPill tone="ok">Healthy</StatusPill>;
    } },
    { key: "l", header: "Load", numeric: true, hideStacked: true, cell: (m) => (m.load5 == null ? "—" : m.load5.toFixed(1)) },
  ];
  const accountCols: Column<Account>[] = [
    { key: "a", header: "Account", cell: (a) => `${a.label ?? a.account} (${a.agent})`, truncate: true },
    { key: "s", header: "Status", cell: (a) => <StatusPill tone={accountTone(a)}>{a.status}</StatusPill> },
    { key: "r", header: "Running", numeric: true, cell: (a) => a.running },
  ];
  const alerts = live.alerts.filter((a) => a.state === "open");
  const alertCols: Column<Alert>[] = [
    { key: "a", header: "Alert", cell: (a) => alertText(a), truncate: true },
    { key: "t", header: "When", numeric: true, cell: (a) => ago(a.ts, now) },
  ];
  return (
    <div className="ops">
      <Card title={<>Running (<Count value={running.length} />)</>} bodyClassName="flush">{running.length ? <Table caption="Running tasks" columns={runCols} rows={running} rowKey={(w) => w.task_id!} /> : none("running tasks")}</Card>
      <Card title={<>Queue (<Count value={live.queue.length} />)</>} bodyClassName="flush">{live.queue.length ? <Table caption="Queue" columns={queueCols} rows={live.queue} rowKey={(q) => q.task_id} /> : none("queued tasks")}</Card>
      <Card title="Machines" bodyClassName="flush">{live.machines.length ? <Table caption="Machines" columns={machineCols} rows={live.machines} rowKey={(m) => m.node} /> : none("machines")}</Card>
      <Card title="Accounts" bodyClassName="flush">{live.accounts.length ? <Table caption="Accounts" columns={accountCols} rows={live.accounts} rowKey={(a) => a.account} /> : none("accounts")}</Card>
      <Card title={<>Alerts (<Count value={alerts.length} />)</>} bodyClassName="flush">{alerts.length ? <Table caption="Open alerts" columns={alertCols} rows={alerts} rowKey={(a) => String(a.id)} /> : none("open alerts")}</Card>
    </div>
  );
}
