import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { get, post, type Task } from "../lib/api";
import { taskAction, useCan, useFleet, useRun, useTask, useTaskLog } from "../lib/data";
import { ago, denialTools, duration, nodeName, taskActions } from "../lib/format";
import { Diff, ErrorBanner, StateLabel, StatusPill } from "./bits";
import { useQuery } from "@tanstack/react-query";
import { Terminal } from "./Terminal";
import { TaskDecisions } from "./ControllerDecisions";
import type { ReviewerProposal } from "../lib/decisions";
import { Button, Dialog, Field, IconButton, Input, useConfirm } from "./ui";
import { isCommitHash } from "../lib/work";
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
          <div className="min-w-0">
            <h2 id="sheet-title">{task?.title ?? "Loading task…"}</h2>
            {task && (
              <div className="row small quiet mt-1">
                <StateLabel state={task.state} />
                {task.display && <StatusPill display={task.display} />}
                <span>{task.agent}{task.model ? ` (${task.model})` : ""} on {nodeName(nodes, task.node_id)}</span>
                <span>{task.mode}</span>
                <span>updated {ago(task.updated_at)}</span>
              </div>
            )}
          </div>
          <IconButton label="Close" className="dlg-close" onClick={onClose} autoFocus><span aria-hidden="true">×</span></IconButton>
        </div>
        <div className="sheet-body">
          <ErrorBanner error={error instanceof Error ? error.message : null} />
          <ErrorBanner error={actionError} onDismiss={clearError} />
          {task && <TaskBody task={task} busy={busy} act={act} diff={diff} loadDiff={() => run(async () => setDiff(await get(`/api/tasks/${id}/diff`)))} />}
          {task && can.operate && taskActions(task).message && (
            <form onSubmit={send} className="row end mt-2">
              <Field className="grow" label="Follow-up instructions (resumes the same session)">
                {(p) => <Input {...p} name="message" autoComplete="off" value={message} onChange={(e) => setMessage(e.target.value)} required />}
              </Field>
              <Button type="submit" variant="primary" disabled={busy || !message.trim()}>Send</Button>
            </form>
          )}
          <h3>Agent activity</h3>
          <Terminal title={terminalTitle(task, nodes)} text={log} live={task?.state === "working" || task?.state === "starting"} label="Agent activity" />
        </div>
    </dialog>
  );
}

/** `node:~/repo (branch)`; the task record carries the repo path and branch, not the worktree path. */
function terminalTitle(task: Task | undefined, nodes: Parameters<typeof nodeName>[0]): string {
  if (!task) return "agent";
  const repo = (task.repo ?? "").split("/").filter(Boolean).pop() ?? "";
  return `${nodeName(nodes, task.node_id)}:~/${repo}${task.branch ? ` (${task.branch})` : ""}`;
}

function TaskBody({ task, busy, act, diff, loadDiff }: {
  task: Task;
  busy: boolean;
  act: (a: string, body?: unknown) => void;
  diff: { stat: string; diff: string } | null;
  loadDiff: () => void;
}) {
  const scope = useCan();
  const confirm = useConfirm();
  const all = taskActions(task);
  const can = { ...all, approve: all.approve && scope.admin, reject: all.reject && scope.admin, resolve: all.resolve && scope.admin,
    cancel: all.cancel && scope.operate, complete: all.complete && scope.operate, cleanup: all.cleanup && scope.operate };
  const gate = task.gate_result;
  const [elsewhere, setElsewhere] = useState(false);
  const [by, setBy] = useState("");
  const [commit, setCommit] = useState("");
  const byErr = !by.trim() ? "Say who finished it." : null;
  const commitErr = commit.trim() && !isCommitHash(commit) ? "A commit is 7 to 40 hex characters." : null;
  async function submitElsewhere() {
    if (byErr || commitErr) return;
    const body = { kind: "done_elsewhere", by: by.trim(), ...(commit.trim() ? { commit: commit.trim() } : {}) };
    if (await confirm({ title: "Mark done elsewhere?", message: `Closes ${task.id} as done by ${body.by}.`, confirmLabel: "Mark done" })) {
      act("resolve", body);
      setElsewhere(false); setBy(""); setCommit("");
    }
  }
  return (
    <>
      <Dialog open={elsewhere} onClose={() => setElsewhere(false)} title="Mark done elsewhere"
        footer={<><Button onClick={() => setElsewhere(false)}>Cancel</Button><Button variant="primary" disabled={!!byErr || !!commitErr} onClick={submitElsewhere}>Continue</Button></>}>
        <div className="stack tight">
          <Field label="Who finished this?" hint="For example: Sonnet 5.5 (orchestrator session)">{(p) => <Input {...p} value={by} onChange={(e) => setBy(e.target.value)} />}</Field>
          <Field label="Commit (optional)" error={commitErr}>{(p) => <Input {...p} value={commit} placeholder="7 to 40 hex characters" onChange={(e) => setCommit(e.target.value)} />}</Field>
        </div>
      </Dialog>
      <div className="row mb-3">
        {can.approve && <button className="primary" disabled={busy} onClick={() => act("approve")}>Approve</button>}
        {can.reject && <button className="danger" disabled={busy} onClick={() => act("reject")}>Reject</button>}
        {can.cancel && <button className="danger" disabled={busy} onClick={() => act("cancel")}>Cancel task</button>}
        {can.complete && <button disabled={busy} onClick={() => act("complete")}>Mark done</button>}
        {can.resolve && (
          <button disabled={busy} onClick={() => setElsewhere(true)}>Mark done elsewhere…</button>
        )}
        {can.resolve && <button disabled={busy} onClick={() => act("resolve", { kind: "dismissed" })}>Dismiss</button>}
        {can.diff && <button disabled={busy} onClick={loadDiff}>Show changes</button>}
        {can.cleanup && (
          <button disabled={busy} onClick={async () => (await confirm({ title: "Remove worktree?", message: "Remove this task's worktree? Its branch is kept.", confirmLabel: "Remove worktree", danger: true })) && act("cleanup")}>
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
        <dt>Gate provenance</dt>
        <dd>{gate ? <>{gate.state}{gate.reason && <span className="quiet"> · {gate.reason}</span>}{gate.profile && <span className="mono quiet"> · profile {gate.profile}</span>}{gate.exit_code != null && <span className="quiet"> · exit {gate.exit_code}</span>}</> : <span className="quiet">no gate result recorded</span>}</dd>
        <dt>Cost source</dt><dd className="mono">{task.cost_source ?? task.usage?.cost_source ?? "—"}</dd>
        <dt>Runs</dt><dd>{task.runs}{task.exit_code != null && ` · last exit ${task.exit_code}`} · agent time {duration(task.agent_seconds)}</dd>
        <dt>Session</dt><dd className="mono">{task.session_id ?? "—"}</dd>
      </dl>

      {(task.kind === "build" || task.kind === "fix") && task.state === "review" && <SuggestedReviewer id={task.id} />}
      {task.brief && (task.brief.files_allowed?.length || task.brief.acceptance?.length) ? (
        <>
          <h3>Brief</h3>
          {task.brief.files_allowed?.length ? <><p className="small"><b>Files allowed</b></p><ul className="small">{task.brief.files_allowed.map((f) => <li key={f} className="mono">{f}</li>)}</ul></> : null}
          {task.brief.acceptance?.length ? <><p className="small"><b>Acceptance</b></p><ul className="small">{task.brief.acceptance.map((a) => <li key={a}>{a}</li>)}</ul></> : null}
        </>
      ) : null}
      <TaskDecisions id={task.id} />

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
      {gate?.output_tail && (<><h3>Gate output</h3><Terminal title={`gate: ${task.gate ?? "gate"}`} text={gate.output_tail} height={240} idPrefix="gate-" label="Gate output" /></>)}
      {diff && (<><h3>Changes</h3><pre className="out">{diff.stat}</pre><Diff text={diff.diff} /></>)}
    </>
  );
}

/** Read-only reviewer suggestion (GET /api/tasks/{id}/propose-reviewer). */
function SuggestedReviewer({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["propose-reviewer", id], queryFn: () => get<ReviewerProposal>(`/api/tasks/${id}/propose-reviewer`), retry: false });
  return (
    <>
      <h3>Suggested reviewer</h3>
      {q.error ? <p className="small quiet">No suggestion available{q.error instanceof Error ? `: ${q.error.message}` : ""}.</p> :
        !q.data ? <p className="small quiet">Loading…</p> : (
          <p className="small"><b>{q.data.agent}</b>{q.data.model && <span className="mono"> ({q.data.model})</span>} <span className="quiet">· {q.data.reason}</span></p>
        )}
    </>
  );
}

/** "Hand off now…" (admin only): the eligible slots, then the same handoff sequence as an automatic one. */
function HandOffNow({ id, busy, act }: { id: string; busy: boolean; act: (a: string, body?: unknown) => void }) {
  const confirm = useConfirm();
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
                onClick={async () => (await confirm({ title: "Hand off work?", message: `Hand this work off to ${handoffSlotName(s)}?`, confirmLabel: "Hand off" })) && act("handoff", { slot: s })}>
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
          {(r.kind === "resolved_by" || r.kind === "handed_off") && r.task_id && <> · <Link to={`/work?task=${encodeURIComponent(r.task_id)}`}>open that task</Link></>}
          {r.note && <span className="quiet"> · {r.note}</span>}
        </p>
      )}
      {task.outcome && <p className="small"><b>Outcome</b> {task.outcome.kind}{task.outcome.commit && <span className="mono"> {task.outcome.commit}</span>}</p>}
    </>
  );
}
