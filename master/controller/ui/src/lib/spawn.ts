// Spawn a follow-up task under a work item (docs/SPEC-ui-revamp.md 6a). Pure body builder, so it is testable.
export interface FollowUp { rootId: string; parentId: string; kind: string; agent: string; prompt: string; repo?: string | null; stage?: string }

export const FOLLOW_KINDS = ["fix", "review", "re_review", "merge", "check"] as const;
const LINK: Record<string, string> = { fix: "fix", review: "review", re_review: "review", merge: "merge", check: "check" };

export function followUpBody(f: FollowUp): Record<string, unknown> {
  const body: Record<string, unknown> = {
    agent: f.agent, prompt: f.prompt.trim(), parent_id: f.parentId, kind: f.kind, link: LINK[f.kind] ?? "retry",
  };
  if (f.repo) body.repo = f.repo;
  if (f.stage) body.stage = f.stage;
  return body;
}

export function followUpError(f: Pick<FollowUp, "prompt" | "agent" | "parentId">): string | null {
  if (!f.parentId) return "Pick the attempt this follows.";
  if (!f.agent) return "Pick an agent.";
  if (!f.prompt.trim()) return "Say what the follow-up should do.";
  return null;
}
