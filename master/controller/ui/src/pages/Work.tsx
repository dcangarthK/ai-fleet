import { useEffect, useMemo } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useWork } from "../lib/data";
import { ago } from "../lib/format";
import { PageHead, StatusPill } from "../components/bits";
import { Card, Empty, ErrorState, Input, Loading, Select, Table, Truncate, type Column } from "../components/ui";
import type { WorkItem } from "../lib/lineage";
import { VIEWS, filterOptions, filterWork, findWorkRoot, groupOf, GROUP_LABEL, latestAttempt, miniChain, modelsUsed, workSummary, type WorkFilters } from "../lib/work";

function Mini({ item }: { item: WorkItem }) {
  const marks = miniChain(item);
  return <span className="mono" aria-label={`${marks.length} attempts`}>{marks.map((m) => (m === "d" ? "●" : m === "b" ? "✕" : "◐")).join(" ")}</span>;
}

export function WorkPage() {
  const [sp, setSp] = useSearchParams();
  const navigate = useNavigate();
  const q = useWork(true);
  const items = q.data ?? [];
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

  const cols: Column<WorkItem>[] = [
    { key: "title", header: "Work", label: "Work", cell: (i) => <Link to={`/work/${i.root_id}`}><Truncate text={i.title || i.root_id} max={380} /></Link> },
    { key: "status", header: "Status", cell: (i) => <><StatusPill display={latestAttempt(i)?.display ?? i.display} /> <span className="muted">{GROUP_LABEL[groupOf(i)]}</span></> },
    { key: "chain", header: "Chain", cell: (i) => <Mini item={i} /> },
    { key: "models", header: "Models", cell: (i) => modelsUsed(i).join(", ") || "-" },
    { key: "upd", header: "Updated", cell: (i) => ago(i.updated_at) },
  ];

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
          : <Table columns={cols} rows={rows} rowKey={(i) => i.root_id} onRowClick={(i) => navigate(`/work/${i.root_id}`)} caption="Work items" />}
      </Card>
    </>
  );
}
