import { useEffect, useState, type FormEvent } from "react";
import { get, post, type Task } from "../lib/api";
import { taskAction, useFleet, useRun, useTask, useTaskLog } from "../lib/data";
import { ago, denialTools, duration, nodeName, taskActions } from "../lib/format";
import { Diff, ErrorBanner, StateLabel } from "./bits";

/** Task detail as a side sheet over the task list. Closes on Escape. */
export function TaskSheet({ id, onClose }: { id: string; onClose: () => void }) {
  const { data: task, error } = useTask(id);
  const fleet = useFleet();
  const log = useTaskLog(id, task?.state === "working" || task?.state === "starting");
  const { run, busy, error: actionError, clearError } = useRun();
  const [diff, setDiff] = useState<{ stat: string; diff: string } | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  useEffect(() => setDiff(null), [id]);

  const nodes = fleet.data?.nodes ?? [];
  const act = (action: string) => run(() => taskAction(id, action));

  async function send(e: FormEvent) {
    e.preventDefault();
    const ok = await run(() => post<Task>(`/api/tasks/${id}/message`, { text: message }));
    if (ok) setMessage("");
  }

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} />
      <aside className="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title">
        <div className="sheet-head">
          <div style={{ minWidth: 0 }}>
            <h2 id="sheet-title">{task?.title ?? "Loading task…"}</h2>
            {task && (
              <div className="row small quiet" style={{ marginTop: 4 }}>
                <StateLabel state={task.state} />
                <span>{task.agent}{task.model ? ` (${task.model})` : ""} on {nodeName(nodes, task.node_id)}</span>
                <span>{task.mode}</span>
                <span>updated {ago(task.updated_at)}</span>
              </div>
            )}
          </div>
          <button onClick={onClose} aria-label="Close task details">Close</button>
        </div>
        <div className="sheet-body">
          <ErrorBanner error={error instanceof Error ? error.message : null} />
          <ErrorBanner error={actionError} onDismiss={clearError} />
          {task && <TaskBody task={task} busy={busy} act={act} diff={diff} loadDiff={() => run(async () => setDiff(await get(`/api/tasks/${id}/diff`)))} />}
          {task && taskActions(task).message && (
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
      </aside>
    </>
  );
}

function TaskBody({ task, busy, act, diff, loadDiff }: {
  task: Task;
  busy: boolean;
  act: (a: string) => void;
  diff: { stat: string; diff: string } | null;
  loadDiff: () => void;
}) {
  const can = taskActions(task);
  const gate = task.gate_result;
  return (
    <>
      <div className="row" style={{ marginBottom: 14 }}>
        {can.approve && <button className="primary" disabled={busy} onClick={() => act("approve")}>Approve</button>}
        {can.reject && <button className="danger" disabled={busy} onClick={() => act("reject")}>Reject</button>}
        {can.cancel && <button className="danger" disabled={busy} onClick={() => act("cancel")}>Cancel task</button>}
        {can.complete && <button disabled={busy} onClick={() => act("complete")}>Mark done</button>}
        {can.diff && <button disabled={busy} onClick={loadDiff}>Show changes</button>}
        {can.cleanup && (
          <button disabled={busy} onClick={() => confirm("Remove this task's worktree? Its branch is kept.") && act("cleanup")}>
            Remove worktree
          </button>
        )}
      </div>

      {task.error && <div className="banner error">{task.error}</div>}
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
      {task.result && (<><h3>Result</h3><pre className="out">{task.result}</pre></>)}
      {gate?.output_tail && (<><h3>Gate output</h3><pre className="out">{gate.output_tail}</pre></>)}
      {diff && (<><h3>Changes</h3><pre className="out">{diff.stat}</pre><Diff text={diff.diff} /></>)}
    </>
  );
}
