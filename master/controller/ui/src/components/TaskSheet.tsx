import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { get, post, type Task } from "../lib/api";
import { taskAction, useCan, useFleet, useRun, useTask, useTaskLog } from "../lib/data";
import { ago, denialTools, duration, nodeName, taskActions } from "../lib/format";
import { Diff, ErrorBanner, StateLabel, StatusPill } from "./bits";
import { displayOf } from "../lib/lineage";
import { handoffSlotName, plannedText, type HandoffSlot } from "../lib/handoff";

/** Task detail as a side sheet over the task list. Closes on Escape. */
export function TaskSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const { data: task, error } = useTask(id);
  const fleet = useFleet();
  const log = useTaskLog(id, task?.state === "working" || task?.state === "starting");
  const { run, busy, error: actionError, clearError } = useRun();
  const [diff, setDiff] = useState<{ stat: string; diff: string } | null>(null);
  const [message, setMessage] = useState("");
  const can = useCan();

  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    // A native modal dialog gives focus containment, an inert background and Escape-to-close.
    const d = ref.current;
    const opener = document.activeElement as HTMLElement | null;
    if (d && !d.open) d.showModal();
    return () => opener?.focus?.();
  }, []);
  useEffect(() => setDiff(null), [id]);

  const nodes = fleet.data?.nodes ?? [];
  const act = (action: string, body?: unknown) => run(() => (body === undefined ? taskAction(id, action) : post<Task>(`/api/tasks/${id}/${action}`, body)));

  async function send(e: FormEvent) {
    e.preventDefault();
    const ok = await run(() => post<Task>(`/api/tasks/${id}/message`, { text: message }));
    if (ok) setMessage("");
  }

  return (
    <dialog ref={ref} className="sheet" aria-labelledby="sheet-title" onClose={onClose}
            onClick={(e) => e.target === e.currentTarget && onClose()}>
        <div className="sheet-head">
          <div style={{ minWidth: 0 }}>
            <h2 id="sheet-title">{task?.title ?? "Loading task…"}</h2>
            {task && (
              <div className="row small quiet" style={{ marginTop: 4 }}>
                <StateLabel state={task.state} />
                {task.display && <StatusPill display={task.display} />}
                <span>{task.agent}{task.model ? ` (${task.model})` : ""} on {nodeName(nodes, task.node_id)}</span>
                <span>{task.mode}</span>
                <span>updated {ago(task.updated_at)}</span>
              </div>
            )}
          </div>
          <button type="button" onClick={onClose} autoFocus aria-label="Close task details">Close</button>
        </div>
        <div className="sheet-body">
          <ErrorBanner error={error instanceof Error ? error.message : null} />
          <ErrorBanner error={actionError} onDismiss={clearError} />
          {task && <TaskBody task={task} busy={busy} act={act} diff={diff} loadDiff={() => run(async () => setDiff(await get(`/api/tasks/${id}/diff`)))} />}
          {task && can.operate && taskActions(task).message && (
            <form onSubmit={send} className="row" style={{ marginTop: 8 }}>
              <label className="field" style={{ flex: 1, minWidth: 220 }}>
                Follow-up instructions (resumes the same session)
                <input value={message} onChange={(e) => setMessage(e.target.value)} required />
              </label>
              <button className="primary" disabled={busy || !message.trim()} style={{ alignSelf: "flex-end" }}>Send</button>
            </form>
          )}
          <h3>Agent activity</h3>
          <pre className="out" aria-live="polite">{log || "No output yet."}</pre>
        </div>
    </dialog>
  );
}

function TaskBody({ task, busy, act, diff, loadDiff }: {
  task: Task;
  busy: boolean;
  act: (a: string, body?: unknown) => void;
  diff: { stat: string; diff: string } | null;
  loadDiff: () => void;
}) {
  const scope = useCan();
  const all = taskActions(task);
  const can = { ...all, approve: all.approve && scope.admin, reject: all.reject && scope.admin, resolve: all.resolve && scope.admin,
    cancel: all.cancel && scope.operate, complete: all.complete && scope.operate, cleanup: all.cleanup && scope.operate };
  const gate = task.gate_result;
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        {can.approve && <button className="primary" disabled={busy} onClick={() => act("approve")}>Approve</button>}
        {can.reject && <button className="danger" disabled={busy} onClick={() => act("reject")}>Reject</button>}
        {can.cancel && <button className="danger" disabled={busy} onClick={() => act("cancel")}>Cancel task</button>}
        {can.complete && <button disabled={busy} onClick={() => act("complete")}>Mark done</button>}
        {can.resolve && (
          <button disabled={busy} onClick={() => {
            const by = prompt("Who finished this? (for example: Sonnet 5.5 (orchestrator session))");
            if (!by?.trim()) return;
            const commit = prompt("Commit that has the work (7 to 40 hex characters, or leave empty)") ?? "";
            act("resolve", { kind: "done_elsewhere", by: by.trim(), ...(commit.trim() ? { commit: commit.trim() } : {}) });
          }}>Mark done elsewhere…</button>
        )}
        {can.resolve && <button disabled={busy} onClick={() => act("resolve", { kind: "dismissed" })}>Dismiss</button>}
        {can.diff && <button disabled={busy} onClick={loadDiff}>Show changes</button>}
        {can.cleanup && (
          <button disabled={busy} onClick={() => confirm("Remove this task's worktree? Its branch is kept.") && act("cleanup")}>
            Remove worktree
          </button>
        )}
        {all.handoff && scope.admin && <HandOffNow id={task.id} busy={busy} act={act} />}
      </div>

      {task.error && <div className="banner error">{task.error}</div>}
      <Lineage task={task} />
      {plannedText(task.planned) && <div className="banner note">{plannedText(task.planned)}</div>}
      {task.state === "blocked" && (
        <div className="banner note">
          The agent was denied: {denialTools(task).map((t) => <span className="tag" key={t}>{t}</span>)} Approve to rerun it with those tools allowed.
        </div>
      )}

      <dl className="kv">
        <dt>Task</dt><dd className="mono">{task.id}{task.parent_id && <span className="quiet"> (child of {task.parent_id})</span>}</dd>
        <dt>Branch</dt><dd className="mono">{task.branch ?? "—"}</dd>
        <dt>Repo</dt><dd className="mono">{task.repo ?? "—"}</dd>
        <dt>Gate</dt>
        <dd>
          {task.gate ? <span className="mono">{task.gate}</span> : "none"}
          {gate && <> · <span className={`tone-${gate.state === "passed" ? "ok" : gate.state === "failed" ? "bad" : "quiet"}`}>{gate.state}</span></>}
        </dd>
        <dt>Runs</dt><dd>{task.runs}{task.exit_code != null && ` · last exit ${task.exit_code}`} · agent time {duration(task.agent_seconds)}</dd>
        <dt>Session</dt><dd className="mono">{task.session_id ?? "—"}</dd>
      </dl>

      <h3>Prompt</h3>
      <pre className="out">{task.prompt}</pre>
      {(task.instructions || (task.policy && Object.keys(task.policy).length > 0)) && (
        <details>
          <summary className="small">Instructions and policy this task runs with{task.policy_hash && <span className="mono quiet"> · {task.policy_hash.slice(0, 12)}</span>}</summary>
          {task.instructions && <pre className="out">{task.instructions}</pre>}
          {task.policy && <pre className="out">{JSON.stringify(task.policy, null, 2)}</pre>}
        </details>
      )}
      {task.result && (<><h3>Result</h3><pre className="out">{task.result}</pre></>)}
      {gate?.output_tail && (<><h3>Gate output</h3><pre className="out">{gate.output_tail}</pre></>)}
      {diff && (<><h3>Changes</h3><pre className="out">{diff.stat}</pre><Diff text={diff.diff} /></>)}
    </>
  );
}

/** "Hand off now…" (admin only): the eligible slots, then the same handoff sequence as an automatic one. */
function HandOffNow({ id, busy, act }: { id: string; busy: boolean; act: (a: string, body?: unknown) => void }) {
  const [slots, setSlots] = useState<HandoffSlot[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function load() {
    setError(null);
    try {
      setSlots((await get<{ slots: HandoffSlot[] }>(`/api/tasks/${id}/handoff`)).slots);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <>
      <button disabled={busy} onClick={load}>Hand off now…</button>
      {error && <span className="small tone-bad">{error}</span>}
      {slots && slots.length === 0 && <span className="small quiet">No eligible slot right now.</span>}
      {slots?.map((s) => (
        <button key={`${s.node}/${s.agent}/${s.profile}/${s.model}`} disabled={busy}
                onClick={() => confirm(`Hand this work off to ${handoffSlotName(s)}?`) && act("handoff", { slot: s })}>
          {handoffSlotName(s)}
        </button>
      ))}
    </>
  );
}

/** Why the task ended as it did, when a limit lifts, and how it was dealt with. */
function Lineage({ task }: { task: Task }) {
  const r = task.resolution;
  const display = displayOf(task);
  return (
    <>
      {task.failure_reason && (
        <p className="small">
          <b>Reason</b> <span className="mono">{task.failure_reason}</span>
          {task.resets_at && <> · limit resets {new Date(task.resets_at * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</>}
        </p>
      )}
      {r && (
        <p className="small">
          <b>Resolution</b> <span className="quiet">{display.label}</span>
          {(r.kind === "resolved_by" || r.kind === "handed_off") && r.task_id && <> · <Link to={`/tasks?view=all&tab=all&task=${encodeURIComponent(r.task_id)}`}>open that task</Link></>}
          {r.note && <span className="quiet"> · {r.note}</span>}
        </p>
      )}
      {task.outcome && <p className="small"><b>Outcome</b> {task.outcome.kind}{task.outcome.commit && <span className="mono"> {task.outcome.commit}</span>}</p>}
    </>
  );
}
