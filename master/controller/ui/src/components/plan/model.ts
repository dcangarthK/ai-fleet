// Plan page logic (controller/leader.py: GET /api/plan, PUT /api/plan, PUT /api/plan/{item}, GET /api/leader). Pure.

import { ApiError } from "../../lib/api";

export const STATUSES =["open", "blocked", "pending_review", "review_queued", "done"] as const;
export type PlanStatus = (typeof STATUSES)[number];

export interface PlanItem { work_item: string; next_steps: string[]; status: string; updated_at: number | null; revision: number }
export interface PlanView { revision: number; items: PlanItem[] }
export interface LeaderRecord {
  account: string | null;
  model: string | null;
  since: number | null;
  session_task_id: string | null;
  not_before: number | null;
  plan_revision: number;
  halted: boolean;
}

export const STATUS_LABEL: Record<string, string> = {
  open: "Open", blocked: "Blocked", pending_review: "Pending review", review_queued: "Review queued", done: "Done",
};

export type Tone = "ok" | "warn" | "bad" | "info" | "accent" | "quiet";
export const STATUS_TONE: Record<string, Tone> = { open: "info", blocked: "bad", pending_review: "warn", review_queued: "accent", done: "ok" };

/** An open item sitting this long without a change is flagged. */
export const STALE_S = 2 * 3600;

export interface PlanHealth { blocked: string[]; noSteps: string[]; stale: string[]; pendingReview: string[] }

/** Counted straight from the plan, no model involved. */
export function planHealth(items: PlanItem[], now: number): PlanHealth {
  const open = items.filter((i) => i.status !== "done");
  return {
    blocked: items.filter((i) => i.status === "blocked").map((i) => i.work_item),
    // The leader only runs an open item that has a next step (leader.py _next_item).
    noSteps: items.filter((i) => i.status === "open" && i.next_steps.length === 0).map((i) => i.work_item),
    stale: open.filter((i) => i.updated_at != null && now - i.updated_at > STALE_S).map((i) => i.work_item),
    pendingReview: items.filter((i) => i.status === "pending_review" || i.status === "review_queued").map((i) => i.work_item),
  };
}

export function healthSentence(h: PlanHealth): string {
  const parts: string[] = [];
  const n = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`;
  if (h.blocked.length) parts.push(n(h.blocked.length, "blocked item", "blocked items"));
  if (h.noSteps.length) parts.push(`${n(h.noSteps.length, "open item", "open items")} with no next step`);
  if (h.stale.length) parts.push(`${n(h.stale.length, "item", "items")} unchanged for over 2h`);
  if (h.pendingReview.length) parts.push(`${n(h.pendingReview.length, "item", "items")} waiting for review`);
  return parts.length ? `${parts.join(", ")}.` : "Nothing blocked, stale or waiting.";
}

export function healthTone(h: PlanHealth): "ok" | "warn" | "bad" {
  if (h.blocked.length) return "bad";
  return h.noSteps.length || h.stale.length || h.pendingReview.length ? "warn" : "ok";
}

/** Leader state in words: halted, waiting for a limit to reset, active, or no leader yet. */
export function leaderState(l: LeaderRecord | null | undefined, now: number): { tone: Tone; label: string } {
  if (!l || (!l.account && !l.model && !l.session_task_id)) return { tone: "quiet", label: "No leader yet" };
  if (l.halted) return { tone: "bad", label: "Halted" };
  if (l.not_before && l.not_before > now) return { tone: "warn", label: "Waiting for a limit to reset" };
  return { tone: "ok", label: l.session_task_id ? "Leading" : "Idle" };
}

/** The leader's last plan revision against the stored one: behind means plan edits it has not seen. */
export function leaderLag(l: LeaderRecord | null | undefined, plan: PlanView | undefined): number {
  if (!l || !plan) return 0;
  return Math.max(0, plan.revision - l.plan_revision);
}

export const stepsToText = (steps: string[]): string => steps.join("\n");
export function textToSteps(text: string): string[] {
  return text.split("\n").map((s) => s.trim()).filter(Boolean);
}

/** Client-side mirror of leader.py _check_item, so a bad edit is caught before the request. */
export function validateItem(workItem: string, steps: string[], status: string): string | null {
  if (!workItem.trim() || workItem.trim().length > 128) return "Work item needs 1 to 128 characters.";
  if (!(STATUSES as readonly string[]).includes(status)) return "Pick a status.";
  if (steps.length > 50) return "At most 50 next steps.";
  if (steps.some((s) => s.length > 1000)) return "Each next step must be under 1000 characters.";
  return null;
}

/** The server answers 409 when the item changed under the editor. */
export function isConflict(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409;
}
