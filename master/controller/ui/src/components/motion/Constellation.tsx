import { useMemo, type CSSProperties, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import type { Fleet } from "../../lib/api";
import { layout, nodeHealth, summary, type CAgentIn, type CInput, type CLiveIn } from "../../lib/constellation";
import type { Live } from "../../lib/live";
import { useReducedMotion } from "../lineage/hooks";
import { useVisible } from "./useVisible";
import "./now-motion.css";

/** Agent mark id: agent plus its account/profile, so two Claude accounts stay two marks. */
const agentId = (agent: string, account: string | null | undefined) => (account ? agent + ":" + account : agent);

/** Build the layout input from data the Now page already fetched: fleet status (nodes, role assignments)
 *  and live workers/accounts. Exported for reuse; pure. */
export function constellationInput(fleet: Fleet | undefined, live: Live | null | undefined): CInput {
  const nodes = (fleet?.nodes ?? []).map((n) => ({ id: n.id, name: n.name, health: nodeHealth(n), models: n.models.length }));
  const roles = (fleet?.assignments ?? []).map((a) => ({ node_id: a.node_id, role: a.role }));
  const agents = new Map<string, CAgentIn>();
  for (const a of live?.accounts ?? []) {
    if (a.agent === "ollama") continue; // local models are the nodes themselves
    agents.set(agentId(a.agent, a.account), { id: agentId(a.agent, a.account), label: a.label ?? a.agent });
  }
  const liveTasks: CLiveIn[] = [];
  for (const w of live?.workers ?? []) {
    if (!w.task_id || !w.node || w.agent === "ollama") continue;
    const id = agentId(w.agent, w.profile);
    if (!agents.has(id)) agents.set(id, { id, label: w.account_label ?? w.agent });
    liveTasks.push({ task_id: w.task_id, agent: id, node_id: w.node, title: w.title });
  }
  return { nodes, roles, agents: [...agents.values()], live: liveTasks };
}

/** Fleet constellation: nodes as dots (colour = health, size = models), role orbits, agents on an outer ring.
 *  Each live task (live.workers) draws a curved link with flow particles. Fully still when nothing runs,
 *  under reduced motion, or while the tab is hidden. Click a node -> /nodes/:id, a link -> /work/:task. */
export function Constellation({ fleet, live }: { fleet: Fleet | undefined; live: Live | null | undefined }) {
  const navigate = useNavigate();
  const reduced = useReducedMotion();
  const visible = useVisible();
  const lay = useMemo(() => layout(constellationInput(fleet, live)), [fleet, live]);
  const flowing = lay.anyLive && !reduced && visible;
  // Cap animated particles at 150: past that, only the most recent task's link flows (last live worker).
  const overCap = lay.links.length * 3 > 150;
  const focusId = overCap ? lay.links[lay.links.length - 1]?.task_id : null;
  if (!lay.nodes.length) return null;
  const key = (go: () => void) => (e: KeyboardEvent) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
  return (
    <svg className={"constellation" + (flowing ? " flowing" : "")} viewBox={"0 0 " + lay.w + " " + lay.h} preserveAspectRatio="xMidYMid meet"
         role="group" aria-label={"Fleet constellation: " + summary(lay)}>
      {lay.nodes.map((n) => n.orbits.map((o) => (
        <circle key={n.id + o.role} className="c-orbit" cx={n.x} cy={n.y} r={o.r} fill="none"><title>{o.role}</title></circle>
      )))}
      {lay.links.map((l) => {
        const go = () => navigate("/work/" + encodeURIComponent(l.task_id));
        return (
          <g key={l.id} className="c-link" role="link" tabIndex={0} aria-label={"Live task " + l.title} onClick={go} onKeyDown={key(go)}>
            <path className="c-link-hit" d={l.path} />
            <path className="c-link-line" d={l.path} id={"cl-" + l.task_id} />
            {flowing && (!overCap || l.task_id === focusId) && [0, 1, 2].map((i) => (
              <circle key={i} className="c-particle" r="2.2" style={{ offsetPath: 'path("' + l.path + '")', animationDelay: -i * 0.8 + "s" } as CSSProperties} />
            ))}
            <title>{l.title}</title>
          </g>
        );
      })}
      {lay.agents.map((a) => (
        <g key={a.id} className={"c-agent" + (a.live ? " live" : "")}>
          <rect x={a.x - 4} y={a.y - 4} width="8" height="8" rx="2" />
          <text x={a.x} y={a.y < lay.h / 2 ? a.y - 9 : a.y + 16} textAnchor="middle">{a.label}</text>
        </g>
      ))}
      {lay.nodes.map((n) => {
        const go = () => navigate("/nodes/" + encodeURIComponent(n.id));
        return (
          <g key={n.id} className={"c-node " + n.health} role="link" tabIndex={0} aria-label={"Node " + n.name + ", " + n.health} onClick={go} onKeyDown={key(go)}>
            <circle cx={n.x} cy={n.y} r={Math.max(n.r, 11)} fill="transparent" />
            <circle className="c-dot" cx={n.x} cy={n.y} r={n.r} />
            <text x={n.x} y={n.y + n.r + 12 + n.orbits.length * 5} textAnchor="middle">{n.name}</text>
            <title>{n.name + " (" + n.health + ")"}</title>
          </g>
        );
      })}
    </svg>
  );
}
