import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { post, put, type CatalogModel, type FleetNode, type ModelParams, type NodeModelState } from "../lib/api";
import { BUSY, useCan, useFleet, useModels, useRun } from "../lib/data";
import { explainReason, FIT_LABEL, INSTALL_LABEL, progressFraction, TRUSTED_RULE } from "../lib/format";
import { ErrorBanner, LoadError, NodeLight, PageHead, Section } from "../components/bits";
import { Button, Empty, Panel, Table, useConfirm, type Column } from "../components/ui";

const ROLE_ORDER = ["coder", "reviewer", "fast"];

export function ModelsPage() {
  const models = useModels();
  const fleet = useFleet();
  const { run, busy, error, clearError } = useRun();
  const [adding, setAdding] = useState(false);
  const can = useCan();
  const confirm = useConfirm();
  const [installing, setInstalling] = useState<{ model: CatalogModel; node?: string } | null>(null);
  const nodes = (fleet.data?.nodes ?? []).filter((n) => n.status === "approved");
  const catalog = models.data ?? [];
  const assignments = fleet.data?.assignments ?? [];
  const inCatalog = new Set(catalog.map((m) => m.reference));
  const uncatalogued = Array.from(new Set(nodes.flatMap((n) => n.models))).filter((m) => !inCatalog.has(m) && !m.startsWith("fleet-")).sort();

  async function remove(m: CatalogModel, nodeId: string, state: NodeModelState) {
    const node = nodes.find((n) => n.id === nodeId)?.name ?? nodeId;
    const roles = assignments.filter((a) => a.node_id === nodeId && a.model === m.reference).map((a) => a.role);
    const lines = [];
    if (roles.length) lines.push(`Roles that stop routing here: ${roles.join(", ")}.`);
    if (state.ownership === "pre-existing") lines.push("This model was on the node before the fleet managed it.");
    const ok = await confirm({
      title: `Remove ${m.reference} from ${node}?`,
      message: lines.length ? <>{lines.map((l) => <p key={l}>{l}</p>)}</> : undefined,
      confirmLabel: "Remove model", danger: true,
    });
    if (!ok) return;
    await run(() => put(`/api/nodes/${encodeURIComponent(nodeId)}/models/${encodeURIComponent(m.id)}`,
      { desired: "absent", confirm_preexisting: state.ownership === "pre-existing" }));
  }

  const cols: Column<CatalogModel>[] = [
    { key: "model", header: "Model", cell: (m) => (
      <>
        <b>{m.display_name || m.reference}</b>
        <div className="sub mono wrap-any">{m.reference}</div>
        {roleTags(assignments.filter((a) => a.model === m.reference).map((a) => a.role))}
        {m.trusted_reviewer && <span className="tag tone-ok" title={TRUSTED_RULE} tabIndex={0}>Trusted reviewer</span>}
        {m.derived_trusted_reviewer && (
          <span className="tag tone-ok" tabIndex={0}
                title={`With custom settings this model is served as ${m.derived_model}, which is benchmarked separately. ${TRUSTED_RULE}`}>
            Trusted with settings
          </span>
        )}
        {m.trusted_reviewer && !m.derived_trusted_reviewer && m.derived_model && (
          <div className="sub">With custom settings, {m.derived_model} is judged on its own benchmark.</div>
        )}
      </>
    ) },
    { key: "size", header: "Size", numeric: true, hideStacked: true, cell: (m) => (m.download_gb ? `${m.download_gb.toFixed(1)} GB` : "—") },
    { key: "evals", header: "Eval scores", hideStacked: true, cell: (m) => <span className="small">{scores(m.evals)}</span> },
    ...nodes.map((n): Column<CatalogModel> => ({
      key: `node-${n.id}`,
      header: <span className="row" style={{ gap: "var(--s2)" }}><NodeLight node={n} />{n.name}</span>,
      label: n.name,
      cell: (m) => (
        <InstallCell state={m.nodes[n.id]} busy={busy || !can.operate}
          onInstall={() => setInstalling({ model: m, node: n.id })}
          onRemove={() => remove(m, n.id, m.nodes[n.id])}
          onRetry={() => run(() => put(`/api/nodes/${encodeURIComponent(n.id)}/models/${encodeURIComponent(m.id)}`,
            { desired: "present", params: m.nodes[n.id]?.params ?? undefined }))} />
      ),
    })),
  ];

  return (
    <>
      <PageHead
        title="Models"
        intro="The models your fleet can run. Add one to the catalog, then install it on a node; the node downloads and tests it in the background, and roles that use it switch on when it's ready."
        actions={can.admin && <Button variant="primary" onClick={() => setAdding(true)}>Add model</Button>}
      />
      <LoadError error={models.error ?? fleet.error} />
      <ErrorBanner error={error} onDismiss={clearError} />

      <Panel>
        <Table columns={cols} rows={catalog} rowKey={(m) => m.id} caption="Model catalog" />
        {!models.isLoading && catalog.length === 0 && (
          <Empty title="The catalog is empty">Add a model by its Ollama name, for example <span className="mono">qwen3:8b</span>.</Empty>
        )}
      </Panel>

      {can.admin && uncatalogued.length > 0 && (
        <Section title="On nodes but not in the catalog" aside="Add them to manage and route them from here">
          <div className="row">
            {uncatalogued.map((ref) => (
              <Button key={ref} className="mono" size="sm" disabled={busy} onClick={() => run(() => post("/api/models", { reference: ref }))}>
                Add {ref}
              </Button>
            ))}
          </div>
        </Section>
      )}

      <AddModelDialog open={adding} onClose={() => setAdding(false)} />
      {installing && (
        <InstallDialog model={installing.model} nodes={nodes} initialNode={installing.node} onClose={() => setInstalling(null)} />
      )}
    </>
  );
}

function roleTags(roles: string[]): ReactNode {
  const unique = Array.from(new Set(roles));
  if (!unique.length) return null;
  return <div style={{ marginTop: 4 }}>{unique.map((r) => <span className="tag" key={r}>{r}</span>)}</div>;
}

function scores(evals: Record<string, number> | null): ReactNode {
  if (!evals || !Object.keys(evals).length) return <span className="quiet">not evaluated</span>;
  const keys = Object.keys(evals).sort((a, b) => (ROLE_ORDER.indexOf(a) + 99) % 100 - (ROLE_ORDER.indexOf(b) + 99) % 100);
  return keys.map((k) => (
    <span key={k} style={{ marginRight: 10, whiteSpace: "nowrap" }}>
      {k} <b className={evals[k] >= 85 ? "tone-ok" : evals[k] < 60 ? "tone-bad" : ""}>{Math.round(evals[k])}</b>
    </span>
  ));
}

function ProgressBar({ value }: { value: number | null }) {
  return (
    <span className="bar" role="progressbar" aria-valuemin={0} aria-valuemax={100}
          aria-valuenow={value == null ? undefined : Math.round(value * 100)}>
      <span style={{ width: value == null ? "30%" : `${Math.round(value * 100)}%` }} className={value == null ? "indeterminate" : ""} />
    </span>
  );
}

function InstallCell({ state, busy, onInstall, onRemove, onRetry }: {
  state: NodeModelState | undefined;
  busy: boolean;
  onInstall: () => void;
  onRemove: () => void;
  onRetry: () => void;
}) {
  if (!state) return <span className="quiet">—</span>;
  const fit = FIT_LABEL[state.fit] ?? FIT_LABEL.unknown;
  const s = state.state ?? undefined;
  if (s && BUSY.has(s)) {
    const frac = progressFraction(state.progress);
    return (
      <div className="small">
        <span className={`state tone-${INSTALL_LABEL[s].tone}`}>{INSTALL_LABEL[s].label}{frac != null && ` ${Math.round(frac * 100)}%`}</span>
        <ProgressBar value={s === "pulling" ? frac : null} />
      </div>
    );
  }
  if (s === "blocked" || s === "failed") {
    return (
      <div className="small">
        <span className={`state tone-${INSTALL_LABEL[s].tone}`}>{INSTALL_LABEL[s].label}</span>
        <div className="sub" style={{ maxWidth: 260 }}>{explainReason(state.reason)}</div>
        <Button variant="ghost" size="sm" disabled={busy} onClick={onRetry}>Retry</Button>
      </div>
    );
  }
  if (state.installed || s === "ready") {
    return (
      <div className="small">
        <span className="state tone-ok">Installed</span>
        {state.ownership === "pre-existing" && <div className="sub">was already on the node</div>}
        <div><Button variant="ghost" size="sm" disabled={busy} onClick={onRemove}>Remove</Button></div>
      </div>
    );
  }
  return (
    <div className="small">
      <span className={`state tone-${fit.tone}`}>{fit.label}</span>
      <div><Button size="sm" disabled={busy} onClick={onInstall}>Install</Button></div>
    </div>
  );
}

function useDialog(open: boolean) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return ref;
}

/** Parameter fields shared by "add model" (defaults) and "install" (per-node overrides). */
function ParamFields({ value, onChange }: { value: ModelParams; onChange: (v: ModelParams) => void }) {
  const num = (s: string) => (s === "" ? undefined : Number(s));
  return (
    <div className="grid-form">
      <label className="field">Context length (tokens)
        <input type="number" min={512} max={131072} step={512} value={value.num_ctx ?? ""} placeholder="model default"
               onChange={(e) => onChange({ ...value, num_ctx: num(e.target.value) })} />
      </label>
      <label className="field">Temperature
        <input type="number" min={0} max={2} step={0.05} value={value.temperature ?? ""} placeholder="model default"
               onChange={(e) => onChange({ ...value, temperature: num(e.target.value) })} />
      </label>
      <label className="field">Thinking
        <select value={value.think === undefined ? "" : String(value.think)}
                onChange={(e) => onChange({ ...value, think: e.target.value === "" ? undefined : e.target.value === "true" })}>
          <option value="">Model default</option>
          <option value="false">Off (faster, fewer timeouts)</option>
          <option value="true">On</option>
        </select>
      </label>
    </div>
  );
}

const clean = (p: ModelParams) => Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined && !Number.isNaN(v))) as ModelParams;

function AddModelDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const ref = useDialog(open);
  const { run, busy, error, clearError } = useRun();
  const [reference, setReference] = useState("");
  const [name, setName] = useState("");
  const [defaults, setDefaults] = useState<ModelParams>({ temperature: 0.2 });

  async function submit(e: FormEvent) {
    e.preventDefault();
    const ok = await run(() => post("/api/models", { reference: reference.trim(), display_name: name.trim() || undefined, defaults: clean(defaults) }));
    if (ok) { setReference(""); setName(""); onClose(); }
  }

  return (
    <dialog ref={ref} className="modal" onClose={onClose} aria-labelledby="add-model-title">
      <form onSubmit={submit}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 id="add-model-title">Add a model to the catalog</h2>
          <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
        </div>
        <ErrorBanner error={error} onDismiss={clearError} />
        <div className="grid-form">
          <label className="field">Ollama name
            <input className="mono" value={reference} onChange={(e) => setReference(e.target.value)} required
                   placeholder="qwen3:8b" pattern="[a-z0-9][a-z0-9._\-/]*(:[A-Za-z0-9._\-]+)?" title="Lowercase name, optional :tag" />
          </label>
          <label className="field">Display name (optional)
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Qwen3 8B" />
          </label>
        </div>
        <p className="small quiet" style={{ margin: 0 }}>The name is checked against the Ollama registry, which also gives the download size. Defaults below apply wherever it's installed unless you change them per node.</p>
        <ParamFields value={defaults} onChange={setDefaults} />
        <div className="row"><Button type="submit" variant="primary" disabled={busy || !reference.trim()}>Add to catalog</Button></div>
      </form>
    </dialog>
  );
}

function InstallDialog({ model, nodes, initialNode, onClose }: {
  model: CatalogModel;
  nodes: FleetNode[];
  initialNode?: string;
  onClose: () => void;
}) {
  const ref = useDialog(true);
  const { run, busy, error, clearError } = useRun();
  const [node, setNode] = useState(initialNode ?? "");
  const [params, setParams] = useState<ModelParams>(model.defaults ?? {});
  const [override, setOverride] = useState(false);
  const fit = node ? model.nodes[node]?.fit ?? "unknown" : "unknown";
  const chosen = nodes.find((n) => n.id === node);
  const canManage = (n: FleetNode) => n.runner?.models?.remote_management !== false;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const ok = await run(() => put(`/api/nodes/${encodeURIComponent(node)}/models/${encodeURIComponent(model.id)}`,
      { desired: "present", params: clean(params), override_fit: override || undefined }));
    if (ok) onClose();
  }

  return (
    <dialog ref={ref} className="modal" onClose={onClose} aria-labelledby="install-title">
      <form onSubmit={submit}>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 id="install-title">Install {model.display_name || model.reference}</h2>
          <Button variant="ghost" size="sm" onClick={onClose}>Close</Button>
        </div>
        <ErrorBanner error={error} onDismiss={clearError} />
        <label className="field">Node
          <select value={node} onChange={(e) => setNode(e.target.value)} required>
            <option value="" disabled>Choose a node</option>
            {nodes.map((n) => (
              <option key={n.id} value={n.id} disabled={!canManage(n)}>
                {n.name}: {FIT_LABEL[model.nodes[n.id]?.fit ?? "unknown"].label}{!canManage(n) ? " (model management is off on this node)" : ""}
              </option>
            ))}
          </select>
        </label>
        {chosen && chosen.runner?.models?.remote_management === false && (
          <div className="banner note small">{chosen.name} doesn't allow remote model installs. Its owner can turn this on in the node's runner.json.</div>
        )}
        {model.download_gb && <p className="small quiet" style={{ margin: 0 }}>Downloads {model.download_gb.toFixed(1)} GB. The node keeps its own disk reserve and refuses if there isn't room.</p>}
        <ParamFields value={params} onChange={setParams} />
        {(fit === "wont_fit" || fit === "tight") && (
          <label className="row small" style={{ gap: 8 }}>
            <input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
            <span className={fit === "wont_fit" ? "tone-bad" : "tone-warn"}>
              {fit === "wont_fit" ? "Install anyway. The model likely needs more memory than this node has; expect swapping or failures." : "Install anyway, knowing it's a tight fit."}
            </span>
          </label>
        )}
        <div className="row">
          <Button type="submit" variant="primary" disabled={busy || !node || (fit === "wont_fit" && !override) || !canManage(nodes.find((n) => n.id === node)!)}>Install</Button>
          <span className="small quiet">You can keep working; progress shows at the top of every page.</span>
        </div>
        <p className="small quiet" style={{ margin: 0 }}>To use it for a role, assign it on <Link to="/roles">Roles</Link> once it's installed, or assign it now and the role switches on when it's ready.</p>
      </form>
    </dialog>
  );
}
