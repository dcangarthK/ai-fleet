/** Pure layout for the Now-page fleet constellation (docs/UI-MOTION-SCOPE-2026-11-18.md, "Signature moment").
 *  No DOM, no randomness: the same input always gives the same picture. */

export const VIEW_W = 800;
export const VIEW_H = 180;

export type Health = "up" | "degraded" | "down" | "pending" | "drained";

export interface CNodeIn { id: string; name: string; health: Health; models: number }
export interface CRoleIn { node_id: string; role: string }
export interface CAgentIn { id: string; label: string }
/** One running task: which agent mark feeds which node. */
export interface CLiveIn { task_id: string; agent: string; node_id: string; title?: string }
export interface CInput { nodes: CNodeIn[]; roles: CRoleIn[]; agents: CAgentIn[]; live: CLiveIn[] }

export interface CNode { id: string; name: string; health: Health; x: number; y: number; r: number; orbits: { role: string; r: number }[] }
export interface CAgent { id: string; label: string; x: number; y: number; live: boolean }
export interface CLink { id: string; task_id: string; agent: string; node: string; path: string; title: string }
export interface Layout { w: number; h: number; nodes: CNode[]; agents: CAgent[]; links: CLink[]; anyLive: boolean }

const r2 = (n: number) => Math.round(n * 100) / 100;

export function nodeRadius(models: number): number {
  return 5 + Math.min(Math.max(models, 0), 6);
}

/** Nodes sit on a centre line, agents on an elliptical outer ring starting at the top and going clockwise. */
export function layout(input: CInput, w = VIEW_W, h = VIEW_H): Layout {
  const cx = w / 2, cy = h / 2;
  const nodes = [...input.nodes].sort((a, b) => a.id.localeCompare(b.id));
  const span = Math.min(w * 0.5, 120 * Math.max(nodes.length, 1));
  const cn: CNode[] = nodes.map((n, i) => {
    const x = nodes.length === 1 ? cx : cx - span / 2 + (span * i) / (nodes.length - 1);
    const r = nodeRadius(n.models);
    const roles = [...new Set(input.roles.filter((ro) => ro.node_id === n.id).map((ro) => ro.role))].sort();
    return { id: n.id, name: n.name, health: n.health, x: r2(x), y: cy, r, orbits: roles.map((role, k) => ({ role, r: r + 7 + k * 5 })) };
  });
  const agents = [...input.agents].sort((a, b) => a.id.localeCompare(b.id));
  const rx = w * 0.44, ry = h * 0.4;
  const liveAgents = new Set(input.live.map((l) => l.agent));
  const ca: CAgent[] = agents.map((a, i) => {
    const ang = -Math.PI / 2 + (2 * Math.PI * i) / Math.max(agents.length, 1);
    return { id: a.id, label: a.label, x: r2(cx + rx * Math.cos(ang)), y: r2(cy + ry * Math.sin(ang)), live: liveAgents.has(a.id) };
  });
  const byAgent = new Map(ca.map((a) => [a.id, a]));
  const byNode = new Map(cn.map((n) => [n.id, n]));
  const links: CLink[] = [];
  for (const l of [...input.live].sort((a, b) => a.task_id.localeCompare(b.task_id))) {
    const a = byAgent.get(l.agent), n = byNode.get(l.node_id);
    if (!a || !n) continue; // a link needs both ends
    // Control point: midpoint pushed away from the centre so links bow outward.
    const mx = (a.x + n.x) / 2, my = (a.y + n.y) / 2;
    const dx = mx - cx, dy = my - cy;
    const len = Math.hypot(dx, dy) || 1;
    const bow = 18;
    links.push({
      id: a.id + ">" + l.task_id, task_id: l.task_id, agent: l.agent, node: l.node_id, title: l.title ?? l.task_id,
      path: "M" + a.x + " " + a.y + " Q" + r2(mx + (dx / len) * bow) + " " + r2(my + (dy / len) * bow) + " " + n.x + " " + n.y,
    });
  }
  return { w, h, nodes: cn, agents: ca, links, anyLive: links.length > 0 };
}

/** Health tone for a fleet node. */
export function nodeHealth(n: { status: string; healthy: boolean; runner_ok: boolean }): Health {
  if (n.status === "pending") return "pending";
  if (n.status === "drained") return "drained";
  if (!n.healthy) return "down";
  return n.runner_ok ? "up" : "degraded";
}

/** One-line text summary for assistive tech. */
export function summary(l: Layout): string {
  const n = l.nodes.length;
  return n + (n === 1 ? " node, " : " nodes, ") + l.agents.length + (l.agents.length === 1 ? " agent, " : " agents, ")
    + l.links.length + (l.links.length === 1 ? " live task." : " live tasks.");
}
