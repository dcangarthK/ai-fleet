import { useEffect, useRef, useState, type FormEvent } from "react";
import { post, type Mode, type Task } from "../lib/api";
import { useFleet, useRun } from "../lib/data";
import { ErrorBanner } from "./bits";

type Agent = "codex" | "claude" | "ollama";

/** "New task" dialog. Supervised and full modes go to the approval inbox before they start. */
export function NewTask({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (t: Task) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const fleet = useFleet();
  const { run, busy, error, clearError } = useRun();
  const [agent, setAgent] = useState<Agent>("codex");
  const [mode, setMode] = useState<Mode>("auto-edit");
  const [node, setNode] = useState("");
  const [model, setModel] = useState("");
  const [repo, setRepo] = useState("");
  const [gate, setGate] = useState("");
  const [prompt, setPrompt] = useState("");
  const [bypass, setBypass] = useState(false);

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved" && n.runner_ok);
  const chosen = nodes.find((n) => n.id === node);
  const profiles = Array.from(new Set((chosen ? [chosen] : nodes).flatMap((n) => n.runner?.gate_profiles ?? [])));
  const ollamaModels = Array.from(new Set((chosen ? [chosen] : nodes).flatMap((n) => n.models)));

  async function submit(e: FormEvent) {
    e.preventDefault();
    const body: Record<string, string | boolean> = { agent, mode, prompt };
    if (bypass && agent !== "ollama") body.trusted_bypass = true;
    if (node) body.node = node;
    if (repo.trim()) body.repo = repo.trim();
    if (gate.trim()) body.gate = gate.trim();
    if (agent === "ollama" && model) body.model = model;
    const t = await run(() => post<Task>("/api/tasks", body));
    if (t) {
      setPrompt("");
      onCreated(t);
    }
  }

  return (
    <dialog ref={ref} className="modal" onClose={onClose} aria-labelledby="new-task-title">
      <form onSubmit={submit}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 id="new-task-title">New task</h2>
          <button type="button" className="ghost" onClick={onClose}>Close</button>
        </div>
        <ErrorBanner error={error} onDismiss={clearError} />
        <div className="grid-form">
          <label className="field">Agent
            <select value={agent} onChange={(e) => setAgent(e.target.value as Agent)}>
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
              <option value="ollama">Local model (Ollama)</option>
            </select>
          </label>
          <label className="field">Permission mode
            <select value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
              {(fleet.data?.modes ?? []).map((m) => <option key={m}>{m}</option>)}
            </select>
          </label>
          <label className="field">Node
            <select value={node} onChange={(e) => setNode(e.target.value)}>
              <option value="">Pick automatically</option>
              {nodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
            </select>
          </label>
          {agent === "ollama" && (
            <label className="field">Model
              <select value={model} onChange={(e) => setModel(e.target.value)} required>
                <option value="" disabled>Choose a model</option>
                {ollamaModels.map((m) => <option key={m}>{m}</option>)}
              </select>
            </label>
          )}
        </div>
        <div className="grid-form">
          <label className="field">Repository on the node (optional)
            <input className="mono" value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="~/code/my-project" />
          </label>
          <label className="field">Gate (must pass before review)
            <input list="gate-profiles" value={gate} onChange={(e) => setGate(e.target.value)} placeholder={profiles[0] ?? "none"} />
            <datalist id="gate-profiles">{profiles.map((p) => <option key={p}>{p}</option>)}</datalist>
          </label>
        </div>
        <label className="field">What should the agent do?
          <textarea value={prompt} onChange={(e) => setPrompt(e.target.value)} required />
        </label>
        {agent !== "ollama" && (
          <label className="row small" style={{ gap: 8 }}>
            <input type="checkbox" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
            <span>Skip permission prompts: {agent === "claude" ? "Claude" : "Codex"} runs in full mode without asking. Needs the node to allow full mode and a policy that pre-approves it.</span>
          </label>
        )}
        <div className="row">
          <button className="primary" disabled={busy || !prompt.trim()}>Start task</button>
          <span className="quiet small">Supervised and full modes wait for your approval before they start.</span>
        </div>
      </form>
    </dialog>
  );
}
