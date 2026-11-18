import { Link } from "react-router-dom";
import "../motion/pages-motion.css";
import { useLiveSignal } from "../motion/useLiveSignal";
import { useVisible } from "../motion/useVisible";
import { currentPlanItem, doneFraction } from "../../lib/pageMotion";
import { ago } from "../../lib/format";
import { Button, Empty, StatusPill, Table, type Column } from "../ui";
import { STALE_S, STATUS_LABEL, STATUS_TONE, type PlanItem } from "./model";

/** Status pill that settles (pop) when `status` changes; the current item breathes (the page's only pulse). */
function StatusCell({ status, current }: { status: string; current: boolean }) {
  const settled = useLiveSignal(status, 400); // source: the item's status from /api/plan
  const visible = useVisible();
  const cls = [settled ? "settle-pop" : "", current ? `plan-current${visible ? "" : " paused"}` : ""].filter(Boolean).join(" ");
  return <span className={cls || undefined} style={{ display: "inline-block" }}><StatusPill tone={STATUS_TONE[status] ?? "quiet"}>{STATUS_LABEL[status] ?? status}</StatusPill></span>;
}

/** Progress of done items; fills by scaleX with an ease-out (source: item statuses). */
function PlanProgress({ items }: { items: PlanItem[] }) {
  const f = doneFraction(items);
  return (
    <div role="meter" aria-label="Plan items done" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(f * 100)}
      style={{ height: 4, borderRadius: 2, background: "var(--sunken)", overflow: "hidden", marginBottom: "var(--s3)" }}>
      <span className="bar-fill" style={{ display: "block", height: "100%", background: "var(--ok)", transform: `scaleX(${f})` }} />
    </div>
  );
}

/** Plan items in stored order. Editing needs operate scope; everyone else sees the table read-only. */
export function PlanItems({ items, now, canEdit, onEdit, onAdd }: {
  items: PlanItem[];
  now: number;
  canEdit: boolean;
  onEdit: (item: PlanItem) => void;
  onAdd: () => void;
}) {
  if (items.length === 0) {
    return (
      <Empty title="The plan is empty" action={canEdit ? <Button variant="primary" onClick={onAdd}>Add plan item</Button> : undefined}>
        The orchestrator stores its next steps here with put_plan; you can add an item yourself.
      </Empty>
    );
  }
  const current = currentPlanItem(items);
  const cols: Column<PlanItem & { n: number }>[] = [
    { key: "n", header: "#", numeric: true, cell: (r) => r.n },
    { key: "item", header: "Work item", truncate: true, cell: (r) => <Link className="mono" to={`/work/${encodeURIComponent(r.work_item)}`} title={r.work_item}>{r.work_item}</Link> },
    { key: "status", header: "Status", cell: (r) => <StatusCell status={r.status} current={r.work_item === current} /> },
    {
      key: "next", header: "Next step", label: "Next",
      cell: (r) => r.next_steps.length
        ? <span className="wrap-any">{r.next_steps[0]}{r.next_steps.length > 1 && <span className="quiet"> (+{r.next_steps.length - 1} more)</span>}</span>
        : <span className="quiet">none</span>,
    },
    { key: "rev", header: "Rev", numeric: true, hideStacked: true, cell: (r) => `r${r.revision}` },
    {
      key: "updated", header: "Updated",
      cell: (r) => r.updated_at == null ? "—" : <span className={r.status !== "done" && now - r.updated_at > STALE_S ? "warn-text" : undefined} style={r.status !== "done" && now - r.updated_at > STALE_S ? { color: "var(--warn)" } : undefined}>{ago(r.updated_at)}</span>,
    },
    {
      key: "act", header: "", label: "Actions",
      cell: (r) => canEdit ? <Button size="sm" aria-label={`Edit ${r.work_item}`} onClick={() => onEdit(r)}>Edit</Button> : null,
    },
  ];
  return (
    <>
      <PlanProgress items={items} />
      <Table columns={cols} rows={items.map((it, i) => ({ ...it, n: i + 1 }))} rowKey={(r) => r.work_item} caption="Plan items" />
    </>
  );
}
