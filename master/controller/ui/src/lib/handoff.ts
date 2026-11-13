// Account pools and limit handoff on the client (docs/SPEC-limit-handoff.md 5). Pure helpers only, so they're testable.
import { clockTime, modelLabel, type Tone } from "./budget";

export type SlotState = "free" | "busy" | "limited" | "at_budget" | "offline" | "not_signed_in" | "held";

/** One row of GET /api/resources: a (node, agent, profile, model rule) slot. Labels only, never directories. */
export interface ResourceSlot {
  node: string;
  node_name: string | null;
  agent: string;
  profile: string;
  label: string | null;
  model: string;
  state: SlotState;
  running: number;
  max: number | null;
  until: number | null;
  kinds: string[];
  today_tokens: number;
  /** Flagged "not signed in" after a sign-in failure, until the owner clears it (absent on older controllers). */
  signed_out?: boolean;
}

export interface Planned { action: "handoff" | "wait" | "left"; label: string; at?: number }

/** A candidate slot from GET /api/tasks/{id}/handoff. */
export interface HandoffSlot { node: string; agent: string; profile: string; label: string | null; account: string; model: string | null; key: string | null }

export function slotPill(s: ResourceSlot): { tone: Tone; label: string } {
  switch (s.state) {
    case "free":
      return { tone: "ok", label: "Free" };
    case "busy":
      return { tone: "warn", label: s.max != null ? `Busy ${s.running}/${s.max}` : `Busy ${s.running}` };
    case "limited":
      return { tone: "bad", label: s.until ? `Limited until ${clockTime(s.until)}` : "Limited" };
    case "at_budget":
      return { tone: "bad", label: "At budget" };
    case "not_signed_in":
      return { tone: "bad", label: "Not signed in" };
    case "held":
      return { tone: "quiet", label: "On hold" };
    default:
      return { tone: "quiet", label: "Offline" };
  }
}

/** "Opus (owner-max)" for a rule glob, the model itself for a concrete one; the profile id when there's no label. */
export function slotName(s: Pick<ResourceSlot, "model" | "label" | "profile">): string {
  const concrete = !s.model.includes("*") && s.model.includes(":");
  const name = concrete ? s.model.split(":", 2)[1] : modelLabel(s.model).replace(/^Claude /, "");
  return `${name} (${s.label ?? s.profile})`;
}

/** Only a slot the controller flagged can be cleared; a missing profile directory needs the owner's own sign-in. */
export const canMarkSignedIn = (s: ResourceSlot): boolean => s.state === "not_signed_in" && !!s.signed_out;

export const handoffSlotName = (s: HandoffSlot): string => `${s.model ?? s.agent} (${s.label ?? s.profile})`;

export function plannedText(p: Planned | null | undefined): string | null {
  return p ? p.label : null;
}
