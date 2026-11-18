import { useEffect, useRef, useState, type FormEvent } from "react";
import { post, type Mode, type Task } from "../lib/api";
import { useFleet, useRun } from "../lib/data";
import { ErrorBanner } from "./bits";
import { SpawnPreview } from "./work/SpawnPreview";
import { IconButton } from "./ui";
import { KINDS, type Kind } from "../lib/budget";

type Agent = "codex" | "claude" | "ollama";

const MODELS: Record<Agent, readonly string[]> = {
  codex: ["gpt-5.6-terra"], claude: ["sonnet", "opus", "haiku"], ollama: [],
};

/** "New task" dialog. Supervised and full modes go to the approval inbox before they start. */
export function NewTask({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (t: Task) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const fleet = useFleet();
  const { run, busy, error, clearError } = useRun();
  const [agent, setAgent] = useState<Agent>("codex");
  const [kind, setKind] = useState<Kind>("build");
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
    const body: Record<string, string | boolean> = { agent, mode, prompt, kind };
    if (bypass && agent !== "ollama") body.trusted_bypass = true;
    if (node) body.node = node;
    if (repo.trim()) body.repo = repo.trim();
    if (gate.trim()) body.gate = gate.trim();
    if (model && model !== "auto") body.model = model;
    const t = await run(() => post<Task>("/api/tasks", body));
    if (t) {
      setPrompt("");
      onCreated(t);
    }
  }

  return (
    <dialog ref={ref} className="modal" onClose={onClose} aria-labelledby="new-task-title">
      <form onSubmit={submit}>
        <div className="modal-head">
          <h2 id="new-task-title">New task</h2>
          <IconButton label="Close" className="dlg-close" onClick={onClose}><span aria-hidden="true">×</span></IconButton>
        </div>
        <ErrorBanner error={error} onDismiss={clearError} />
        <label className="field">What should the agent do?
          <textarea name="prompt" autoComplete="off" value={prompt} onChange={(e) => setPrompt(e.target.value)} required autoFocus />
        </label>
        <div className="grid-form">
          <label className="field">Kind
            <select name="kind" autoComplete="off" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
              {KINDS.map((value) => <option key={value} value={value}>{value.replace(/_/g, " ")}</option>)}
            </select>
          </label>
          <label className="field">Agent
            <select name="agent" autoComplete="off" value={agent} onChange={(e) => { setAgent(e.target.value as Agent); setModel("auto"); }}>
              <option value="codex">Codex</option>
              <option value="claude">Claude</option>
              <option value="ollama">Local model (Ollama)</option>
            </select>
          </label>
          <label className="field">Permission mode
            <select name="mode" autoComplete="off" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
              {(fleet.data?.modes ?? []).map((m) => <option key={m}>{m}</option>)}
            </select>
          </label>
          <label className="field">Node
            <select name="node" autoComplete="off" value={node} onChange={(e) => setNode(e.target.value)}>
              <option value="">Pick automatically</option>
              {nodes.map((n) => <option key={n.id} value={n.id}>{n.name}</option>)}
            </select>
          </label>
          <label className="field">Model
            <select name="model" autoComplete="off" value={model || "auto"} onChange={(e) => setModel(e.target.value)}>
              <option value="auto">Auto</option>
              {(agent === "ollama" ? ollamaModels : MODELS[agent]).map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
        </div>
        <div className="grid-form">
          <label className="field">Repository on the node (optional)
            <input className="mono" name="repo" autoComplete="off" spellCheck={false} value={repo} onChange={(e) => setRepo(e.target.value)} placeholder="~/code/my-project…" />
          </label>
          <label className="field">Gate (must pass before review)
            <input list="gate-profiles" name="gate" autoComplete="off" spellCheck={false} value={gate} onChange={(e) => setGate(e.target.value)} placeholder={`${profiles[0] ?? "none"}…`} />
            <datalist id="gate-profiles">{profiles.map((p) => <option key={p}>{p}</option>)}</datalist>
          </label>
        </div>
        {agent !== "ollama" && <SpawnPreview kind={kind} agent={agent} />}
        {agent !== "ollama" && (
          <label className="check small">
            <input type="checkbox" name="bypass" checked={bypass} onChange={(e) => setBypass(e.target.checked)} />
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
