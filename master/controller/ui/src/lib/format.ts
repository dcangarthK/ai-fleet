import type { FleetNode, Task, TaskState } from "./api";

export type Tone = "ok" | "warn" | "bad" | "info" | "accent" | "quiet";

export const STATE: Record<TaskState, { tone: Tone; label: string }> = {
  queued: { tone: "quiet", label: "Queued" },
  awaiting_approval: { tone: "warn", label: "Needs approval" },
  starting: { tone: "accent", label: "Starting" },
  working: { tone: "accent", label: "Working" },
  blocked: { tone: "warn", label: "Blocked" },
  review: { tone: "info", label: "Ready for review" },
  failed: { tone: "bad", label: "Failed" },
  interrupted: { tone: "bad", label: "Interrupted" },
  done: { tone: "ok", label: "Done" },
  cancelled: { tone: "quiet", label: "Cancelled" },
};

export const TASK_TABS: Record<string, { label: string; states: TaskState[] | null }> = {
  active: { label: "Active", states: ["queued", "starting", "working"] },
  waiting: { label: "Needs you", states: ["awaiting_approval", "blocked"] },
  review: { label: "Review", states: ["review"] },
  problems: { label: "Problems", states: ["failed", "interrupted"] },
  finished: { label: "Finished", states: ["done", "cancelled"] },
  attention: { label: "Needs attention", states: null }, // filtered by display tone in the Tasks page
  all: { label: "All", states: null },
};

export function inTab(task: Task, tab: string): boolean {
  const states = TASK_TABS[tab]?.states;
  return !states || states.includes(task.state);
}

export const needsYou = (t: Task) => t.state === "awaiting_approval" || t.state === "blocked";

/** "12s ago", "5m ago", "3h ago", "2d ago"; `now` in seconds for tests. */
export function ago(t: number | null | undefined, now = Date.now() / 1000): string {
  if (!t) return "never";
  const s = Math.max(0, Math.round(now - t));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

/** Duration in seconds → "—", "42s", "7m", "1.5h". */
export function duration(s: number | null | undefined): string {
  if (!s) return "—";
  if (s < 60) return `${Math.round(s)}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

export type Signal = "up" | "degraded" | "down" | "pending" | "drained";

/** One light per node: what an operator needs to know at a glance. */
export function nodeSignal(n: FleetNode): Signal {
  if (n.status === "pending") return "pending";
  if (n.status === "drained") return "drained";
  const servesModels = !n.engine || n.healthy;
  if (n.runner_ok && servesModels) return "up";
  if (n.runner_ok || n.healthy) return "degraded";
  return "down";
}

export const SIGNAL_LABEL: Record<Signal, string> = {
  up: "Up",
  degraded: "Partly up",
  down: "Offline",
  pending: "Waiting for approval",
  drained: "Drained",
};

export function nodeName(nodes: FleetNode[], id: string | null | undefined): string {
  if (!id) return "—";
  return nodes.find((n) => n.id === id)?.name ?? id;
}

export function denialTools(t: Task): string[] {
  return t.denials.map((d) => d.tool_name || d.tool || "unknown tool");
}

/** Which task actions make sense in a state; mirrors the controller's own rules. */
export function taskActions(t: Task) {
  return {
    approve: t.state === "awaiting_approval" || t.state === "blocked",
    reject: t.state === "awaiting_approval" || t.state === "blocked",
    cancel: t.state === "working" || t.state === "starting" || t.state === "queued",
    complete: ["review", "failed", "interrupted", "blocked"].includes(t.state),
    cleanup: ["done", "cancelled", "review", "failed", "interrupted"].includes(t.state) && !!t.branch,
    message: ["review", "failed", "blocked", "interrupted"].includes(t.state),
    resolve: ["failed", "interrupted", "blocked", "cancelled", "review"].includes(t.state) && !t.resolution,
    diff: !!t.branch,
  };
}

export const INSTALL_LABEL: Record<string, { tone: Tone; label: string }> = {
  pending: { tone: "quiet", label: "Queued" },
  checking: { tone: "accent", label: "Checking" },
  pulling: { tone: "accent", label: "Downloading" },
  configuring: { tone: "accent", label: "Configuring" },
  verifying: { tone: "accent", label: "Testing" },
  ready: { tone: "ok", label: "Installed" },
  draining: { tone: "accent", label: "Draining" },
  removing: { tone: "accent", label: "Removing" },
  removed: { tone: "quiet", label: "Removed" },
  failed: { tone: "bad", label: "Failed" },
  blocked: { tone: "warn", label: "Blocked" },
  cancelled: { tone: "quiet", label: "Cancelled" },
};

export const FIT_LABEL: Record<string, { tone: Tone; label: string }> = {
  fits: { tone: "ok", label: "Fits" },
  tight: { tone: "warn", label: "Tight fit" },
  wont_fit: { tone: "bad", label: "Won't fit" },
  unknown: { tone: "quiet", label: "Fit unknown" },
};

/** Turn a runner reason code into a sentence that says what to do. */
export function explainReason(reason: string | null | undefined): string {
  if (!reason) return "";
  if (reason.startsWith("engine_upgrade:")) return "This node's Ollama is too old for this model. Update Ollama on the node, then retry. (" + reason.slice(15).trim() + ")";
  if (reason.startsWith("no_space")) return "Not enough free disk on the node. Free some space or remove a model, then retry.";
  if (reason.startsWith("wont_fit")) return "The model needs more memory than the node can spare. Pick a bigger node, a smaller context, or override the fit check.";
  if (reason.startsWith("size unknown")) return "The registry didn't report this model's size, so the node won't download it.";
  return reason;
}

/** 0..1 download progress, or null when unknown. */
export function progressFraction(p: { completed?: number; total?: number } | null | undefined): number | null {
  if (!p || !p.total) return null;
  return Math.max(0, Math.min(1, (p.completed ?? 0) / p.total));
}

/** 0..1 rate → "42%" (one decimal under 10%), or "—" when absent. */
export function pctText(rate: number | null | undefined): string {
  if (rate == null || !Number.isFinite(rate)) return "—";
  const p = rate * 100;
  return p > 0 && p < 10 ? `${p.toFixed(1)}%` : `${Math.round(p)}%`;
}

/** "name:tag" form; an untagged "qwen3" is "qwen3:latest" (mirrors the controller's canonical_reference). */
export function canonicalRef(model: string): string {
  return model.slice(model.lastIndexOf("/") + 1).includes(":") ? model : `${model}:latest`;
}

/** Whether the model (as named in the leaderboard) is a catalog entry flagged trusted_reviewer. */
export function isTrustedModel(
  catalog: { reference: string; trusted_reviewer?: boolean; derived_model?: string; derived_trusted_reviewer?: boolean }[],
  model: string,
): boolean {
  const ref = canonicalRef(model);
  return catalog.some((m) =>
    (m.trusted_reviewer === true && canonicalRef(m.reference) === ref) ||
    (m.derived_trusted_reviewer === true && !!m.derived_model && canonicalRef(m.derived_model) === ref));
}

/** Why a pipeline review does or doesn't count as trusted, from fields the review record carries. */
export function trustReason(
  reviewer: { kind: string; role?: string; agent?: string },
  rv: { status: string; verdict?: string; trusted?: boolean; trusted_by?: string; fallback?: boolean },
): string {
  if (rv.fallback) return "fallback answer, never trusted";
  if (rv.trusted) {
    if (reviewer.kind === "agent") return "agent reviewer";
    return rv.trusted_by === "benchmark" ? "role's models are benchmark-trusted" : "role named trusted by this pipeline";
  }
  if (rv.status !== "valid" && rv.verdict !== "abstain") return "no usable review";
  return reviewer.kind === "role" ? "role not named trusted and its models aren't all benchmark-trusted" : "agent isn't a trusted kind";
}

export const TRUSTED_RULE =
  "Trusted reviewer: held-out precision ≥ 0.9 by default (the controller's FLEET_TRUSTED_PRECISION can change it) and recall ≥ 0.5 on the real-diff benchmark (reviewer_real).";
