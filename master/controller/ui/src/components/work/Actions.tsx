import { useState } from "react";
import { post } from "../../lib/api";
import { useRun } from "../../lib/data";
import { Button, Field, Input, Select, useConfirm } from "../ui";
import { ErrorBanner } from "../bits";
import { FOLLOW_KINDS, followUpBody, followUpError } from "../../lib/spawn";
import type { DetailAttempt } from "../../lib/work";

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
  async function spawn() {
    const f = { rootId: root, parentId: attempt.id, kind, agent, prompt, repo };
    const bad = followUpError(f);
    setErr(bad);
    if (bad) return;
    if (await confirm({ title: `Spawn ${kind} task?`, message: `${agent} runs a ${kind} under ${attempt.id}. This may use paid-model budget.`, confirmLabel: "Spawn" })) {
      if ((await run(() => post("/api/tasks", followUpBody(f)))) !== undefined) { setFollow(false); setPrompt(""); }
    }
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <ErrorBanner error={error} onDismiss={clearError} />
      <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
        <Input aria-label="Message to the agent" placeholder="Message this attempt" value={text} disabled={!RESUMABLE.has(attempt.state)} onChange={(e) => setText(e.target.value)} />
        <Button disabled={busy || !text.trim() || !RESUMABLE.has(attempt.state)} onClick={send}>Send</Button>
        <Button variant="danger" disabled={busy || !live} onClick={cancel}>Cancel</Button>
        <Button disabled={busy || live} onClick={complete}>Complete</Button>
        <Button aria-expanded={follow} onClick={() => setFollow((v) => !v)}>Spawn follow-up</Button>
      </div>
      {follow && (
        <div className="stack" style={{ gap: 8 }}>
          <div className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <Field label="Kind">{(p) => <Select {...p} value={kind} onChange={(e) => setKind(e.target.value)}>{FOLLOW_KINDS.map((k) => <option key={k}>{k}</option>)}</Select>}</Field>
            <Field label="Agent">{(p) => <Select {...p} value={agent} onChange={(e) => setAgent(e.target.value)}>{["claude", "codex", "ollama"].map((k) => <option key={k}>{k}</option>)}</Select>}</Field>
          </div>
          <Field label="What should it do?" error={err}>{(p) => <textarea {...p} rows={3} value={prompt} onChange={(e) => setPrompt(e.target.value)} />}</Field>
          <div><Button variant="primary" disabled={busy} onClick={spawn}>Spawn</Button></div>
        </div>
      )}
    </div>
  );
}
