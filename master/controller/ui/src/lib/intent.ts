// The intent bar (docs/SPEC-ui-revamp.md 6a): a deterministic parser from plain commands to the exact API request.
// It never executes anything: callers show `fields` and `request` and run it only after useConfirm().
import type { Task } from "./api";
import { KINDS, type BudgetStatus } from "./budget";
import { SONNET, FABLE, type ApiRequest, pickReviewer } from "./insights";

export interface IntentCtx {
  tasks: Pick<Task, "id" | "title" | "agent" | "model" | "repo" | "branch" | "state">[];
  nodes: { id: string; name: string }[];
  budget?: BudgetStatus | null;
}

export interface IntentField { label: string; value: string }
export interface Intent {
  kind: "spawn" | "message" | "drain" | "task_action";
  summary: string;
  /** Null when the text was understood but is not safe to run (ambiguous or unknown target). */
  request: ApiRequest | null;
  fields: IntentField[];
  warnings: string[];
}

interface ModelChoice { agent: "claude" | "codex"; model: string; effort?: string }
const MODEL_ALIASES: Record<string, ModelChoice> = {
  fable: { agent: "claude", model: FABLE },
  sonnet: { agent: "claude", model: SONNET },
  opus: { agent: "claude", model: "claude-opus-5-5" },
  haiku: { agent: "claude", model: "claude-haiku-5-5" },
  codex: { agent: "codex", model: "gpt-5.6-terra", effort: "medium" },
};

const REVIEW_KINDS: Record<string, string> = { review: "review", "final review": "final_review", "security review": "security_review", "re-review": "re_review" };
const ID_RE = /^t-[0-9a-f]{2,}$/i;

type Resolved = { ok: string } | { ambiguous: string[] } | null;
function resolveTask(ref: string, ctx: IntentCtx): Resolved {
  if (!ID_RE.test(ref)) return null;
  const hits = ctx.tasks.filter((t) => t.id === ref || t.id.startsWith(ref.toLowerCase())).map((t) => t.id);
  if (hits.includes(ref)) return { ok: ref };
  if (hits.length === 1) return { ok: hits[0] };
  return hits.length ? { ambiguous: hits.slice(0, 5) } : null;
}

function resolveNode(ref: string, ctx: IntentCtx): Resolved {
  const r = ref.toLowerCase();
  const exact = ctx.nodes.filter((n) => n.id.toLowerCase() === r || n.name.toLowerCase() === r);
  if (exact.length === 1) return { ok: exact[0].id };
  const hits = ctx.nodes.filter((n) => n.name.toLowerCase().startsWith(r) || n.id.toLowerCase().startsWith(r));
  if (hits.length === 1) return { ok: hits[0].id };
  return hits.length ? { ambiguous: hits.map((n) => n.name || n.id) } : null;
}

const globRe = (g: string) => new RegExp(`^${g.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*")}$`);

/** The budget status line that applies to this agent and model, for the "exact request" preview. */
export function budgetNote(budget: BudgetStatus | null | undefined, agent: string, model: string | null): string {
  if (!budget) return "budget status not loaded";
  const m = budget.models.find((x) => {
    if (agent === "codex") return x.key === "codex" || x.agent === "codex";
    const [a, glob] = x.key.split(":", 2);
    return a === agent && (!glob || (model != null && globRe(glob).test(model)));
  });
  if (!m) return agent === "ollama" ? "local model, no paid budget" : "no rule listed for this model (unlisted policy applies)";
  const state = m.state === "ok" ? "OK" : m.state === "soft" ? "at soft budget" : "limited";
  const conc = m.max_concurrent ? `, ${m.running}/${m.max_concurrent} running` : "";
  return `${m.key}${m.tier ? ` (${m.tier})` : ""}: ${state}${conc}`;
}

function spawnIntent(args: {
  kind: string; choice: ModelChoice; prompt: string; title: string; parent?: Task | IntentCtx["tasks"][number];
  link?: string; reviewOf?: string; ctx: IntentCtx; warnings?: string[]; summary: string;
}): Intent {
  const { kind, choice, ctx } = args;
  const effort = choice.effort ? (kind === "final_review" || kind === "security_review" ? "high" : choice.effort) : undefined;
  const parent = args.parent;
  const body: Record<string, unknown> = {
    agent: choice.agent, model: choice.model, kind, prompt: args.prompt, title: args.title.slice(0, 120),
    mode: args.reviewOf ? "read-only" : "auto-edit",
  };
  if (effort) body.effort = effort;
  if (parent) { body.parent_id = parent.id; if (parent.repo) body.repo = parent.repo; }
  if (args.link) body.link = args.link;
  if (args.reviewOf) body.review_of = args.reviewOf;
  const warnings = [...(args.warnings ?? [])];
  if (args.reviewOf && parent && parent.agent === choice.agent && parent.model === choice.model) {
    warnings.push(`${choice.model} built ${parent.id}; a review by the builder's model is refused unless the independence is waived.`);
  }
  const fields: IntentField[] = [
    { label: "Agent", value: choice.agent }, { label: "Model", value: choice.model }, { label: "Kind", value: kind },
    ...(effort ? [{ label: "Effort", value: effort }] : []),
    ...(parent ? [{ label: "Parent", value: parent.id }, { label: "Link", value: args.link ?? "—" }] : []),
    { label: "Mode", value: String(body.mode) },
    { label: "Budget rule", value: budgetNote(ctx.budget, choice.agent, choice.model) },
  ];
  return { kind: "spawn", summary: args.summary, request: { method: "POST", path: "/api/tasks", body }, fields, warnings };
}

const blocked = (kind: Intent["kind"], summary: string, warnings: string[]): Intent => ({ kind, summary, request: null, fields: [], warnings });
const ambiguous = (kind: Intent["kind"], what: string, hits: string[]): Intent =>
  blocked(kind, `Which ${what}?`, [`“${what}” is ambiguous: ${hits.join(", ")}. Type more of it.`]);

function modelFrom(word: string | undefined, fallback: ModelChoice): { choice: ModelChoice } | { error: string } {
  if (!word) return { choice: fallback };
  const c = MODEL_ALIASES[word.toLowerCase()];
  return c ? { choice: c } : { error: `Unknown model “${word}”. Known: ${Object.keys(MODEL_ALIASES).join(", ")}.` };
}

const WITH_RE = /\s+(?:with|using|by)\s+([\w.-]+)\s*$/i;

/**
 * Parse one line of intent. Returns null when it is not a command (the palette then just searches).
 * Forms: review|final review|security review <task> [with <model>]; merge <task> [with <model>];
 * message|tell <task>: <text>; approve|reject|cancel <task>; drain <node>; spawn [<kind>] [with <model>]: <prompt>.
 */
export function parseIntent(input: string, ctx: IntentCtx): Intent | null {
  const text = input.trim().replace(/\s+/g, " ");
  if (!text) return null;

  const spawn = /^spawn(?:\s+([a-z_]+))?(?:\s+(?:with|using|by)\s+([\w.-]+))?\s*:\s*(.+)$/i.exec(text);
  if (spawn) {
    const kind = (spawn[1] ?? "draft").toLowerCase();
    if (!(KINDS as readonly string[]).includes(kind)) return blocked("spawn", `Spawn ${kind}`, [`Unknown kind “${kind}”. Known: ${KINDS.join(", ")}.`]);
    const m = modelFrom(spawn[2], { agent: "claude", model: SONNET });
    if ("error" in m) return blocked("spawn", "Spawn a task", [m.error]);
    return spawnIntent({ kind, choice: m.choice, prompt: spawn[3], title: spawn[3], ctx, summary: `Spawn a ${kind} task on ${m.choice.model}` });
  }

  const msg = /^(?:message|tell|send)\s+(t-[0-9a-f]+)\s*[: ]\s*(.+)$/i.exec(text);
  if (msg) {
    const r = resolveTask(msg[1], ctx);
    if (!r) return blocked("message", `Message ${msg[1]}`, [`No task matches ${msg[1]}.`]);
    if ("ambiguous" in r) return ambiguous("message", msg[1], r.ambiguous);
    return {
      kind: "message", summary: `Send a message to ${r.ok}`,
      request: { method: "POST", path: `/api/tasks/${r.ok}/message`, body: { text: msg[2] } },
      fields: [{ label: "Task", value: r.ok }, { label: "Text", value: msg[2] }], warnings: [],
    };
  }

  const act = /^(approve|reject|cancel)\s+(\S+)$/i.exec(text);
  if (act) {
    const verb = act[1].toLowerCase();
    const r = resolveTask(act[2], ctx);
    if (r && "ambiguous" in r) return ambiguous("task_action", act[2], r.ambiguous);
    if (r) {
      return {
        kind: "task_action", summary: `${verb[0].toUpperCase()}${verb.slice(1)} ${r.ok}`,
        request: { method: "POST", path: `/api/tasks/${r.ok}/${verb}` },
        fields: [{ label: "Task", value: r.ok }, { label: "Action", value: verb }],
        warnings: verb === "approve" || verb === "reject" ? ["Approvals are admin-only; the controller refuses this for other scopes."] : [],
      };
    }
    if (verb === "approve") return null;  // "approve laptop" is a node approval: not offered here, use the Nodes page
    return null;
  }

  const drain = /^drain\s+(\S+)$/i.exec(text);
  if (drain) {
    const r = resolveNode(drain[1], ctx);
    if (!r) return blocked("drain", `Drain ${drain[1]}`, [`No node matches “${drain[1]}”.`]);
    if ("ambiguous" in r) return ambiguous("drain", drain[1], r.ambiguous);
    const name = ctx.nodes.find((n) => n.id === r.ok)?.name ?? r.ok;
    return {
      kind: "drain", summary: `Drain ${name}`,
      request: { method: "POST", path: `/api/nodes/${encodeURIComponent(r.ok)}/drain` },
      fields: [{ label: "Node", value: name }, { label: "Effect", value: "No new work is placed on it until it is approved again." }], warnings: [],
    };
  }

  const merge = /^merge\s+(t-[0-9a-f]+)$/i.exec(text.replace(WITH_RE, ""));
  if (merge) {
    const word = WITH_RE.exec(text)?.[1];
    const r = resolveTask(merge[1], ctx);
    if (!r) return blocked("spawn", `Merge ${merge[1]}`, [`No task matches ${merge[1]}.`]);
    if ("ambiguous" in r) return ambiguous("spawn", merge[1], r.ambiguous);
    const m = modelFrom(word, { agent: "claude", model: SONNET });
    if ("error" in m) return blocked("spawn", `Merge ${r.ok}`, [m.error]);
    const t = ctx.tasks.find((x) => x.id === r.ok)!;
    return spawnIntent({
      kind: "merge", choice: m.choice, parent: t, link: "merge", ctx, summary: `Merge ${r.ok} on ${m.choice.model}`, title: `Merge ${t.id}: ${t.title}`,
      prompt: `Merge branch ${t.branch ?? t.id} into main. Follow the Builder checklist in CLAUDE.md. Do not change files outside the merge.`,
    });
  }

  const rv = /^(final review|security review|re-review|review)\s+(t-[0-9a-f]+)$/i.exec(text.replace(WITH_RE, ""));
  if (rv) {
    const word = WITH_RE.exec(text)?.[1];
    const kind = REVIEW_KINDS[rv[1].toLowerCase()];
    const r = resolveTask(rv[2], ctx);
    if (!r) return blocked("spawn", `Review ${rv[2]}`, [`No task matches ${rv[2]}.`]);
    if ("ambiguous" in r) return ambiguous("spawn", rv[2], r.ambiguous);
    const t = ctx.tasks.find((x) => x.id === r.ok)!;
    const m = modelFrom(word, pickReviewer(t));
    if ("error" in m) return blocked("spawn", `Review ${r.ok}`, [m.error]);
    return spawnIntent({
      kind, choice: m.choice, parent: t, link: "review", reviewOf: t.id, ctx, summary: `${rv[1][0].toUpperCase()}${rv[1].slice(1)} ${t.id} with ${m.choice.model}`,
      title: `Review ${t.id}: ${t.title}`,
      prompt: `Review the diff of task ${t.id} (${t.title}). Report findings as HIGH/MED/LOW blocks. Do not edit files.`,
    });
  }
  return null;
}
