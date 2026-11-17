import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { get } from "../lib/api";
import { useCan } from "../lib/data";
import { PageHead } from "../components/bits";
import { Button, Card, ErrorState, InsightCard, Loading } from "../components/ui";
import { ItemEditor } from "../components/plan/ItemEditor";
import { LeaderCard } from "../components/plan/LeaderCard";
import { PlanItems } from "../components/plan/PlanItems";
import { healthSentence, healthTone, planHealth, type LeaderRecord, type PlanItem, type PlanView } from "../components/plan/model";

/** The orchestrator plan and leader state (docs/SPEC-ui-revamp.md 4, 6a). Edits go through PUT /api/plan/{item} with the item's revision. */
export function PlanPage() {
  const plan = useQuery({ queryKey: ["plan"], queryFn: () => get<PlanView>("/api/plan") });
  const leader = useQuery({ queryKey: ["plan", "leader"], queryFn: () => get<LeaderRecord>("/api/leader") });
  const canEdit = useCan().operate;
  const now = useMemo(() => Math.floor(Date.now() / 1000), [plan.dataUpdatedAt]); // eslint-disable-line react-hooks/exhaustive-deps
  const [editing, setEditing] = useState<PlanItem | null>(null);
  const [open, setOpen] = useState(false);

  const items = plan.data?.items ?? [];
  const health = planHealth(items, now);
  const tone = healthTone(health);
  const latest = editing ? items.find((i) => i.work_item === editing.work_item) : undefined;

  const evidence = [
    ...health.blocked.map((id) => ({ label: `${id} blocked`, to: `/work/${encodeURIComponent(id)}` })),
    ...health.noSteps.map((id) => ({ label: `${id} has no next step` })),
    ...health.stale.map((id) => ({ label: `${id} unchanged over 2h` })),
  ].slice(0, 6);

  return (
    <>
      <PageHead
        title="Plan"
        intro={plan.data
          ? <>Plan revision r{plan.data.revision}, {items.length} item{items.length === 1 ? "" : "s"}. The leader edits it as work lands; your edit is checked against the revision you loaded.</>
          : "The orchestrator plan and leader state."}
        actions={<>
          <Button onClick={() => { plan.refetch(); leader.refetch(); }}>Reload plan</Button>
          {canEdit && <Button variant="primary" onClick={() => { setEditing(null); setOpen(true); }}>Add item</Button>}
        </>}
      />

      {plan.isLoading ? <Loading rows={5} label="Loading plan" /> : plan.error ? <ErrorState error={plan.error} onRetry={() => plan.refetch()} title="Couldn't load the plan" /> : (
        <div className="stack" style={{ gap: "var(--s5)" }}>
          {items.length > 0 && (
            <InsightCard
              kind="Plan health" tone={tone === "ok" ? "ok" : tone}
              title={healthSentence(health)}
              why="Counted directly from the plan (status, next steps, last change). No model was used, so nothing here is a guess."
              confidence={1}
              evidence={evidence}
            />
          )}

          <div style={{ display: "flex", flexWrap: "wrap", gap: "var(--s5)", alignItems: "flex-start" }}>
            <div style={{ flex: "2 1 420px", minWidth: 0 }}>
              <Card title="Plan items" actions={<span className="small quiet">r{plan.data?.revision}</span>}>
                <PlanItems items={items} now={now} canEdit={canEdit} onAdd={() => { setEditing(null); setOpen(true); }} onEdit={(i) => { setEditing(i); setOpen(true); }} />
              </Card>
            </div>
            <div className="stack" style={{ flex: "1 1 280px", minWidth: 0 }}>
              {leader.error ? <ErrorState error={leader.error} onRetry={() => leader.refetch()} title="Couldn't load the leader" /> :
                leader.isLoading ? <Loading rows={3} label="Loading leader" /> :
                <LeaderCard leader={leader.data} plan={plan.data} now={now} />}
            </div>
          </div>
        </div>
      )}

      <ItemEditor open={open} onClose={() => setOpen(false)} item={editing} latest={latest} />
    </>
  );
}
