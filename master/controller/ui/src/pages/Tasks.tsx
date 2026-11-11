import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { NewTask } from "../components/NewTask";
import { TaskSheet } from "../components/TaskSheet";
import { LoadError, PageHead, StateLabel } from "../components/bits";
import { useFleet, useTasks } from "../lib/data";
import { ago, inTab, nodeName, TASK_TABS } from "../lib/format";

export function TasksPage() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "active";
  const open = params.get("task");
  const [creating, setCreating] = useState(false);
  const tasks = useTasks();
  const fleet = useFleet();
  const all = tasks.data ?? [];
  const shown = all.filter((t) => inTab(t, tab));
  const nodes = fleet.data?.nodes ?? [];

  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v === null) next.delete(k);
    else next.set(k, v);
    setParams(next, { replace: k === "task" ? false : true });
  };

  return (
    <>
      <PageHead
        title="Tasks"
        intro="Work delegated to agents on the fleet. Each task runs in its own worktree and branch."
        actions={<button className="primary" onClick={() => setCreating(true)}>New task</button>}
      />
      <LoadError error={tasks.error} />
      <div className="panel">
        <div className="tabs" role="tablist" aria-label="Task filter">
          {Object.entries(TASK_TABS).map(([key, t]) => (
            <button key={key} role="tab" aria-selected={tab === key} onClick={() => set("tab", key)}>
              {t.label}<span className="n">{all.filter((x) => inTab(x, key)).length}</span>
            </button>
          ))}
        </div>
        <table>
          <thead>
            <tr><th>Task</th><th>State</th><th className="hide-sm">Agent and node</th><th className="hide-sm">Mode</th><th>Updated</th></tr>
          </thead>
          <tbody>
            {shown.map((t) => (
              <tr key={t.id} className={`click ${open === t.id ? "selected" : ""}`} onClick={() => set("task", t.id)}
                  tabIndex={0} onKeyDown={(e) => e.key === "Enter" && set("task", t.id)}>
                <td>
                  <b>{t.title}</b>
                  <div className="sub"><span className="mono">{t.id}</span>{t.parent_id && ` · child of ${t.parent_id}`}</div>
                  {t.error && <div className="sub tone-bad">{t.error}</div>}
                </td>
                <td><StateLabel state={t.state} /></td>
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
      </div>
      {open && <TaskSheet id={open} onClose={() => set("task", null)} />}
      <NewTask open={creating} onClose={() => setCreating(false)}
               onCreated={(t) => { setCreating(false); setParams({ tab: "all", task: t.id }); }} />
    </>
  );
}
