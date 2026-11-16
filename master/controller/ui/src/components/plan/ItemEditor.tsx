import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { put } from "../../lib/api";
import { Button, Dialog, Field, Input, Select } from "../ui";
import { STATUSES, STATUS_LABEL, isConflict, stepsToText, textToSteps, validateItem, type PlanItem, type PlanView } from "./model";

/**
 * Edit one plan item (PUT /api/plan/{item} with its revision) or add one (PUT /api/plan). A revision conflict is
 * shown with the server's version beside yours; "overwrite" is deliberately not offered, because it would drop the
 * leader's change unseen.
 */
export function ItemEditor({ open, onClose, item, latest }: {
  open: boolean;
  onClose: () => void;
  /** The item being edited as it was loaded; null to add a new one. */
  item: PlanItem | null;
  /** The newest copy of that item from the plan query, used after a conflict. */
  latest: PlanItem | undefined;
}) {
  const qc = useQueryClient();
  const [workItem, setWorkItem] = useState("");
  const [status, setStatus] = useState("open");
  const [steps, setSteps] = useState("");
  const [revision, setRevision] = useState<number | null>(null);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setWorkItem(item?.work_item ?? "");
    setStatus(item?.status ?? "open");
    setSteps(stepsToText(item?.next_steps ?? []));
    setRevision(item?.revision ?? null);
    setConflict(false);
    setError(null);
  }, [open, item]);

  async function save() {
    const list = textToSteps(steps);
    const bad = validateItem(workItem, list, status);
    if (bad) { setError(bad); return; }
    setBusy(true);
    setError(null);
    try {
      if (item) await put<PlanView>(`/api/plan/${encodeURIComponent(item.work_item)}`, { next_steps: list, status, revision });
      else await put<PlanView>("/api/plan", { items: [{ work_item: workItem.trim(), next_steps: list, status }] });
      await qc.invalidateQueries({ queryKey: ["plan"] });
      onClose();
    } catch (e) {
      if (isConflict(e)) {
        setConflict(true);
        await qc.invalidateQueries({ queryKey: ["plan"] });
      } else setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const theirs = latest ? stepsToText(latest.next_steps) : null;

  return (
    <Dialog
      open={open} onClose={onClose} title={item ? `Edit ${item.work_item}` : "Add plan item"}
      footer={<>
        <Button onClick={onClose}>Cancel</Button>
        <Button variant="primary" disabled={busy || conflict} onClick={save}>{busy ? "Saving…" : "Save"}</Button>
      </>}
    >
      <div className="stack">
        {error && <div className="banner error" role="alert">{error}</div>}
        {conflict && (
          <div className="banner error" role="alert">
            <b>Save rejected: this item changed since you opened it.</b>
            {latest ? <> It is now revision {latest.revision} ({STATUS_LABEL[latest.status] ?? latest.status}).</> : <> It may have been removed.</>}
            {theirs !== null && theirs !== stepsToText(item?.next_steps ?? []) && (
              <pre className="mono small" style={{ whiteSpace: "pre-wrap", margin: "8px 0" }} aria-label="Server's next steps">{theirs || "(no next steps)"}</pre>
            )}
            <div className="row" style={{ flexWrap: "wrap" }}>
              {latest && <Button size="sm" onClick={() => { setRevision(latest.revision); setConflict(false); }}>Keep my edit on revision {latest.revision}</Button>}
              <Button size="sm" onClick={onClose}>Discard my edit</Button>
            </div>
            <p className="small quiet" style={{ marginTop: 6 }}>Overwrite is not offered: it would drop the other change without you seeing it.</p>
          </div>
        )}
        <Field label="Work item" hint={item ? undefined : "The task id this item tracks (leader.py brief reads it), or any short name."}>
          {(p) => <Input {...p} value={workItem} readOnly={!!item} onChange={(e) => setWorkItem(e.target.value)} />}
        </Field>
        <Field label="Status">
          {(p) => <Select {...p} value={status} onChange={(e) => setStatus(e.target.value)}>{STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}</Select>}
        </Field>
        <Field label="Next steps (one per line; the leader runs the first)" hint="Up to 50 steps, 1000 characters each.">
          {(p) => <textarea {...p} rows={6} value={steps} onChange={(e) => setSteps(e.target.value)} />}
        </Field>
      </div>
    </Dialog>
  );
}
