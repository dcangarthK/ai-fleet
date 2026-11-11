import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { NewTask } from "../components/NewTask";
import { TaskSheet } from "../components/TaskSheet";
import { LoadError, PageHead, StatusPill } from "../components/bits";
import { useCan, useFleet, useTasks, useWork } from "../lib/data";
import { ago, inTab, nodeName, TASK_TABS } from "../lib/format";
import { displayOf, isAttention, outcomeLabel, type WorkItem } from "../lib/lineage";

export function TasksPage() {
  const [params, setParams] = useSearchParams();
  // Links from elsewhere carry ?tab=…, which only the flat list understands.
  const view = params.get("view") ?? (params.has("tab") ? "all" : "work");
  const tab = params.get("tab") ?? "active";
  const open = params.get("task");
  const showChecks = params.get("checks") === "1";
  const [creating, setCreating] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const tasks = useTasks();
  const work = useWork(showChecks);
  const can = useCan();
  const fleet = useFleet();
  const all = (tasks.data ?? []).filter((t) => showChecks || !t.hidden);
  const inView = (t: (typeof all)[number], key: string) => (key === "attention" ? isAttention(t) : inTab(t, key));
  const shown = all.filter((t) => inView(t, tab));
  const nodes = fleet.data?.nodes ?? [];

  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v === null) next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: k === "task" ? false : true });
  };
  const toggle = (id: string) => setExpanded((cur) => {
    const next = new Set(cur);
    if (!next.delete(id)) next.add(id);
    return next;
  });

  return (
    <>
      <PageHead
        title="Tasks"
        intro="Work delegated to agents on the fleet. Each task runs in its own worktree and branch."
        actions={can.operate && <button className="primary" onClick={() => setCreating(true)}>New task</button>}
      />
      <LoadError error={tasks.error ?? work.error} />
      <div className="panel">
        <div className="row view-switch">
          <div className="tabs" role="tablist" aria-label="Task view">
            <button role="tab" aria-selected={view === "work"} onClick={() => set("view", "work")}>Work items</button>
            <button role="tab" aria-selected={view === "all"} onClick={() => set("view", "all")}>All attempts</button>
          </div>
          <label className="small quiet check">
            <input type="checkbox" checked={showChecks} onChange={(e) => set("checks", e.target.checked ? "1" : null)} /> Show checks
          </label>
        </div>
        {view === "work" ? (
          <WorkList items={work.data ?? []} loading={work.isLoading} expanded={expanded} toggle={toggle}
                    openTask={(id) => set("task", id)} open={open} />
        ) : (
          <>
            <div className="tabs" role="tablist" aria-label="Task filter">
              {Object.entries(TASK_TABS).map(([key, t]) => (
                <button key={key} role="tab" aria-selected={tab === key} onClick={() => set("tab", key)}>
                  {t.label}<span className="n">{all.filter((x) => inView(x, key)).length}</span>
                </button>
              ))}
            </div>
            <table>
              <thead>
                <tr><th>Task</th><th>Status</th><th className="hide-sm">Agent and node</th><th className="hide-sm">Mode</th><th>Updated</th></tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <tr key={t.id} className={`click ${open === t.id ? "selected" : ""}`} onClick={() => set("task", t.id)}
                      tabIndex={0} onKeyDown={(e) => e.key === "Enter" && set("task", t.id)}>
                    <td>
                      <b>{t.title}</b>
                      <div className="sub"><span className="mono">{t.id}</span>{t.parent_id && ` · ${t.link ?? "child"} of ${t.parent_id}`}</div>
                      {t.error && <div className="sub tone-bad">{t.error}</div>}
                    </td>
                    <td><StatusPill display={displayOf(t)} /></td>
                    <td className="hide-sm">{t.agent}{t.model && <span className="quiet"> ({t.model})</span>}<div className="sub">{nodeName(nodes, t.node_id)}</div></td>
                    <td className="hide-sm">{t.mode}</td>
                    <td className="quiet small">{ago(t.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!tasks.isLoading && shown.length === 0 && (
              <div className="empty">
                {tab === "active" ? "Nothing running. Start a task to delegate work to the fleet." : `No tasks in “${TASK_TABS[tab]?.label ?? tab}”.`}
              </div>
            )}
          </>
        )}
      </div>
      {open && <TaskSheet id={open} onClose={() => set("task", null)} />}
      <NewTask open={creating} onClose={() => setCreating(false)}
               onCreated={(t) => { setCreating(false); setParams({ view: "all", tab: "all", task: t.id }); }} />
    </>
  );
}

function WorkList({ items, loading, expanded, toggle, openTask, open }: {
  items: WorkItem[];
  loading: boolean;
  expanded: Set<string>;
  toggle: (id: string) => void;
  openTask: (id: string) => void;
  open: string | null;
}) {
  return (
    <>
      <table>
        <thead><tr><th>Work item</th><th>Status</th><th className="hide-sm">Latest attempt</th><th>Updated</th></tr></thead>
        <tbody>
          {items.map((w) => {
            const last = w.attempts[w.attempts.length - 1];
            const isOpen = expanded.has(w.root_id);
            return (
              <WorkRows key={w.root_id} w={w} last={last} isOpen={isOpen} toggle={toggle} openTask={openTask} selected={open} />
            );
          })}
        </tbody>
      </table>
      {!loading && items.length === 0 && <div className="empty">No work yet. Start a task to delegate work to the fleet.</div>}
    </>
  );
}

function WorkRows({ w, last, isOpen, toggle, openTask, selected }: {
  w: WorkItem;
  last: WorkItem["attempts"][number] | undefined;
  isOpen: boolean;
  toggle: (id: string) => void;
  openTask: (id: string) => void;
  selected: string | null;
}) {
  const status = w.display;
  return (
    <>
      <tr className="click" onClick={() => toggle(w.root_id)} tabIndex={0} aria-expanded={isOpen}
          onKeyDown={(e) => e.key === "Enter" && toggle(w.root_id)}>
        <td>
          <b>{isOpen ? "▾" : "▸"} {w.title}</b>
          <div className="sub"><span className="mono">{w.root_id}</span> · {w.attempts.length} attempt{w.attempts.length === 1 ? "" : "s"}</div>
        </td>
        <td><StatusPill display={status} /></td>
        <td className="hide-sm">{last ? <>{last.agent}{last.model && <span className="quiet"> ({last.model})</span>}</> : "—"}</td>
        <td className="quiet small">{ago(w.updated_at)}</td>
      </tr>
      {isOpen && (
        <tr className="timeline-row">
          <td colSpan={4}>
            <ol className="timeline" aria-label={`Attempts for ${w.title}`}>
              {w.attempts.map((a, i) => (
                <li key={`${a.id}-${a.run ?? i}`}>
                  <button type="button" className={`ghost attempt ${selected === a.id ? "selected" : ""}`} onClick={() => openTask(a.id)}>
                    <span className="small">{(a.run ?? 1) > 1 ? "resumed" : `${a.model || a.agent} ${a.link ?? "task"}`}</span>
                    <StatusPill display={a.display} />
                  </button>
                </li>
              ))}
              {outcomeLabel(w.outcome) && <li><span className="state tone-ok">{outcomeLabel(w.outcome)}</span></li>}
            </ol>
          </td>
        </tr>
      )}
    </>
  );
}
