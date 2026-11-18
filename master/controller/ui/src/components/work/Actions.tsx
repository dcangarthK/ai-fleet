import { useState } from "react";
import { post } from "../../lib/api";
import { useRun } from "../../lib/data";
import { Button, Field, Input, Select, useConfirm } from "../ui";
import { ErrorBanner } from "../bits";
import { describeRequest } from "../../lib/request";
import { FOLLOW_KINDS, followUpBody, followUpError } from "../../lib/spawn";
import { isCommitHash, type DetailAttempt } from "../../lib/work";
import { SpawnPreview } from "./SpawnPreview";

const LIVE = new Set(["queued", "awaiting_approval", "starting", "working", "blocked"]);
const RESUMABLE = new Set(["review", "blocked", "failed", "interrupted", "done"]);

/** Message, cancel, complete and spawn follow-up for the selected attempt. Every action asks first. */
export function Actions({ root, attempt, repo }: { root: string; attempt: DetailAttempt; repo: string | null }) {
  const confirm = useConfirm();
  const { run, busy, error, clearError } = useRun();
  const [text, setText] = useState("");
  const [follow, setFollow] = useState(false);
  const [kind, setKind] = useState<string>("fix");
  const [agent, setAgent] = useState(attempt.agent || "claude");
  const [prompt, setPrompt] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const live = LIVE.has(attempt.state);
  const [resolving, setResolving] = useState(false);
  const [resKind, setResKind] = useState("dismissed");
  const [resBy, setResBy] = useState("");
  const [resCommit, setResCommit] = useState("");
  const [outcomeOpen, setOutcomeOpen] = useState(false);
  const [outKind, setOutKind] = useState("done");
  const [outCommit, setOutCommit] = useState("");

  async function send() {
    if (!text.trim()) return;
    const ok = await confirm({ title: "Send message?", message: `Resumes ${attempt.id} with your message.`, confirmLabel: "Send" });
    if (ok && (await run(() => post(`/api/tasks/${attempt.id}/message`, { text }))) !== undefined) setText("");
  }
  async function cancel() {
    if (await confirm({ title: "Cancel this attempt?", message: `${attempt.id} stops now. Work in its worktree stays.`, confirmLabel: "Cancel attempt", cancelLabel: "Keep running", danger: true })) {
      await run(() => post(`/api/tasks/${attempt.id}/cancel`));
    }
  }
  async function complete() {
    if (await confirm({ title: "Mark complete?", message: `${attempt.id} is closed as done.`, confirmLabel: "Complete" })) {
      await run(() => post(`/api/tasks/${attempt.id}/complete`));
    }
  }
  const waiting = attempt.state === "blocked" || attempt.state === "awaiting_approval";
  async function approve() {
    const path = `/api/tasks/${attempt.id}/approve`;
    if (await confirm({ title: "Approve this attempt?", message: describeRequest("POST", path), confirmLabel: "Approve" })) await run(() => post(path));
  }
  async function reject() {
    const path = `/api/tasks/${attempt.id}/reject`;
    if (await confirm({ title: "Reject this attempt?", message: describeRequest("POST", path), confirmLabel: "Reject", danger: true })) await run(() => post(path));
  }
  async function resolve() {
    const path = `/api/tasks/${attempt.id}/resolve`;
    const body = { kind: resKind, by: resBy.trim() || undefined, commit: resCommit.trim() || undefined };
    if (resKind === "done_elsewhere" && !body.by) { setErr("done_elsewhere needs who finished it."); return; }
    if (body.commit && !isCommitHash(body.commit)) { setErr("A commit is 7 to 40 hex characters."); return; }
    setErr(null);
    if (await confirm({ title: "Mark resolved?", message: describeRequest("POST", path, body), confirmLabel: "Resolve" })) {
      if ((await run(() => post(path, body))) !== undefined) { setResolving(false); setResBy(""); setResCommit(""); }
    }
  }
  async function outcome() {
    const path = `/api/tasks/${attempt.id}/outcome`;
    const body = { kind: outKind, commit: outCommit.trim() || undefined };
    if ((outKind === "merged" && !body.commit) || (body.commit && !isCommitHash(body.commit))) { setErr("merged needs a commit (7 to 40 hex characters)."); return; }
    setErr(null);
    if (await confirm({ title: `Record outcome: ${outKind}?`, message: describeRequest("POST", path, body), confirmLabel: "Record" })) {
      if ((await run(() => post(path, body))) !== undefined) { setOutcomeOpen(false); setOutCommit(""); }
    }
  }
  async function cleanup() {
    const path = `/api/tasks/${attempt.id}/cleanup`;
    if (await confirm({ title: "Clean up worktree?", message: describeRequest("POST", path), confirmLabel: "Clean up", danger: true })) await run(() => post(path));
  }
  async function spawn() {
    const f = { rootId: root, parentId: attempt.id, kind, agent, prompt, repo };
    const bad = followUpError(f);
    setErr(bad);
    if (bad) return;
    if (await confirm({ title: `Spawn ${kind} task?`, message: <><p>{agent} runs a {kind} under {attempt.id}. This may use paid-model budget.</p><SpawnPreview kind={kind} agent={agent} /></>, confirmLabel: "Spawn" })) {
      if ((await run(() => post("/api/tasks", followUpBody(f)))) !== undefined) { setFollow(false); setPrompt(""); }
    }
  }

  return (
    <div className="stack tight">
      <ErrorBanner error={error} onDismiss={clearError} />
      <div className="row">
        <Input aria-label="Message to the agent" placeholder="Message this attempt" value={text} disabled={!RESUMABLE.has(attempt.state)} onChange={(e) => setText(e.target.value)} />
        <Button disabled={busy || !text.trim() || !RESUMABLE.has(attempt.state)} onClick={send}>Send</Button>
        <Button variant="danger" disabled={busy || !live} onClick={cancel}>Cancel</Button>
        <Button disabled={busy || live} onClick={complete}>Complete</Button>
        {waiting && <Button variant="primary" disabled={busy} onClick={approve}>Approve</Button>}
        {waiting && <Button variant="danger" disabled={busy} onClick={reject}>Reject</Button>}
        <Button aria-expanded={resolving} disabled={busy} onClick={() => setResolving((v) => !v)}>Resolve</Button>
        {attempt.id === root && <Button aria-expanded={outcomeOpen} disabled={busy} onClick={() => setOutcomeOpen((v) => !v)}>Outcome</Button>}
        <Button variant="danger" disabled={busy || live} onClick={cleanup}>Clean up</Button>
        <Button aria-expanded={follow} onClick={() => setFollow((v) => !v)}>Spawn follow-up</Button>
      </div>
      {resolving && (
        <div className="row">
          <Field label="Resolution">{(p) => <Select {...p} value={resKind} onChange={(e) => setResKind(e.target.value)}>{["dismissed", "done_elsewhere"].map((k) => <option key={k}>{k}</option>)}</Select>}</Field>
          <Field label="Resolved by">{(p) => <Input {...p} value={resBy} placeholder="who finished it" onChange={(e) => setResBy(e.target.value)} />}</Field>
          <Field label="Commit (optional)">{(p) => <Input {...p} value={resCommit} onChange={(e) => setResCommit(e.target.value)} />}</Field>
          <Button variant="primary" disabled={busy} onClick={resolve}>Record resolution</Button>
        </div>
      )}
      {outcomeOpen && (
        <div className="row">
          <Field label="Outcome">{(p) => <Select {...p} value={outKind} onChange={(e) => setOutKind(e.target.value)}>{["merged", "done", "abandoned"].map((k) => <option key={k}>{k}</option>)}</Select>}</Field>
          <Field label="Commit (needed for merged)">{(p) => <Input {...p} value={outCommit} onChange={(e) => setOutCommit(e.target.value)} />}</Field>
          <Button variant="primary" disabled={busy} onClick={outcome}>Record outcome</Button>
        </div>
      )}
      {follow && (
        <div className="stack tight">
          <div className="row">
            <Field label="Kind">{(p) => <Select {...p} value={kind} onChange={(e) => setKind(e.target.value)}>{FOLLOW_KINDS.map((k) => <option key={k}>{k}</option>)}</Select>}</Field>
            <Field label="Agent">{(p) => <Select {...p} value={agent} onChange={(e) => setAgent(e.target.value)}>{["claude", "codex", "ollama"].map((k) => <option key={k}>{k}</option>)}</Select>}</Field>
          </div>
          <SpawnPreview kind={kind} agent={agent} />
          <Field label="What should it do?" error={err}>{(p) => <textarea {...p} rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />}</Field>
          <div><Button variant="primary" disabled={busy} onClick={spawn}>Spawn</Button></div>
        </div>
      )}
    </div>
  );
}
