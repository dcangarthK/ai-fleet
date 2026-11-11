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
    diff: !!t.branch,
  };
}
