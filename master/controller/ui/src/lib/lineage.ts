// Task lineage on the client: status pills, the attempt timeline and the Overview groupings.
// The controller computes `display` (docs/SPEC-task-lineage.md 4.5); these helpers only present it.
import type { Task } from "./api";
import { STATE } from "./format";

export type DisplayTone = "running" | "ok" | "paused" | "attention" | "resolved" | "muted";
export interface Display { tone: DisplayTone; label: string }
export type Link = "retry" | "resume" | "review" | "fix" | "merge" | "check";
export interface Outcome { kind: "merged" | "done" | "abandoned"; commit?: string; at: number }
export interface Resolution { kind: "resolved_by" | "dismissed" | "done_elsewhere" | "handed_off" | "handing_off"; task_id?: string; title?: string; commit?: string; by?: string; note?: string; model?: string | null; label?: string | null; at: number }

export interface Attempt {
  id: string;
  title: string;
  agent: string;
  model: string | null;
  link: Link | null;
  state: string;
  display: Display;
  failure_reason: string | null;
  started_at: number | null;
  ended_at: number | null;
  /** 1 for the first run of a task, higher for runs after it was resumed. */
  run?: number;
  // Limit handoff (docs/SPEC-limit-handoff.md 5); absent on older controllers.
  kind?: string | null;
  account_label?: string | null;
  /** This attempt continues another attempt's checkpoint after a usage limit. */
  handoff?: boolean;
  /** This attempt stopped at a usage limit and its work moved to a handoff attempt. */
  handed_off?: boolean;
}

export interface WorkItem {
  root_id: string;
  title: string;
  outcome: Outcome | null;
  display: Display;
  updated_at: number;
  attempts: Attempt[];
}

const TONE_CLASS: Record<DisplayTone, string> = {
  running: "tone-accent",
  ok: "tone-ok",
  paused: "tone-warn",
  attention: "tone-bad",
  resolved: "tone-quiet",
  muted: "tone-quiet",
};

export const toneClass = (tone: DisplayTone): string => TONE_CLASS[tone] ?? "tone-quiet";

/** Pill text: resolved work gets a tick so it reads as dealt with, not just grey. */
export const pillText = (d: Display): string => (d.tone === "resolved" ? `✓ ${d.label}` : d.label);

const DISPLAY_FROM_STATE: Record<string, DisplayTone> = { accent: "running", ok: "ok", info: "ok", warn: "attention", bad: "attention", quiet: "muted" };

/** The controller's display, or one derived from the state when it's an older controller. */
export function displayOf(t: Pick<Task, "state" | "display">): Display {
  if (t.display) return t.display;
  const s = STATE[t.state];
  return { tone: DISPLAY_FROM_STATE[s?.tone ?? "quiet"] ?? "muted", label: s?.label ?? t.state };
}

const RUNNING_STATES = new Set(["queued", "starting", "working"]);

export interface OverviewSets { working: Task[]; attention: Task[]; paused: Task[] }

/** Overview groups: running work, things that need a person, and work paused by a usage limit. Checks are never shown. */
export function overviewSets(tasks: Task[]): OverviewSets {
  const shown = tasks.filter((t) => !t.hidden);
  return {
    working: shown.filter((t) => RUNNING_STATES.has(t.state)),
    attention: shown.filter((t) => !RUNNING_STATES.has(t.state) && displayOf(t).tone === "attention"),
    paused: shown.filter((t) => !RUNNING_STATES.has(t.state) && displayOf(t).tone === "paused"),
  };
}

/** Whether a task belongs in "Needs attention" (the same rule Overview uses). */
export const isAttention = (t: Task): boolean => !t.hidden && !RUNNING_STATES.has(t.state) && displayOf(t).tone === "attention";

export const ATTENTION_CAP = 5;

/** The first `cap` items, and how many more there are. */
export function capped<T>(items: T[], cap = ATTENTION_CAP): { shown: T[]; more: number } {
  return { shown: items.slice(0, cap), more: Math.max(0, items.length - cap) };
}

/** Tasks ready for review: state review and still counted as ok (not dismissed or done elsewhere). */
export const readyForReview = (tasks: Task[]): Task[] =>
  tasks.filter((t) => !t.hidden && t.state === "review" && displayOf(t).tone === "ok");

export function outcomeLabel(o: Outcome | null | undefined): string | null {
  if (!o) return null;
  if (o.kind === "merged") return `Merged ${(o.commit ?? "").slice(0, 7)}`.trim();
  return o.kind === "done" ? "Done" : "Abandoned";
}

/** One step per attempt (or run), then the outcome: ["Sonnet task · Paused: …", "resumed · Ready for review", "merged ec2ab20"]. */
export function timelineSteps(attempts: Attempt[], outcome?: Outcome | null): string[] {
  const who = (a: Attempt) => `${a.model || a.agent}${a.account_label ? ` (${a.account_label})` : ""}`;
  const steps = attempts.map((a) => {
    if ((a.run ?? 1) > 1) return `resumed · ${a.display.label}`;
    // A handed-off attempt reads as where it stopped; its successor as where the work went (one story, two accounts).
    if (a.handoff) return `handed off to ${who(a)} · ${a.display.label}`;
    const label = a.handed_off ? "paused: limit" : a.display.label;
    return `${who(a)} ${a.link ?? a.kind ?? "task"} · ${label}`;
  });
  const end = outcomeLabel(outcome);
  if (end) steps.push(end.charAt(0).toLowerCase() + end.slice(1));
  return steps;
}

export const timelineText = (attempts: Attempt[], outcome?: Outcome | null): string => timelineSteps(attempts, outcome).join(" → ");
