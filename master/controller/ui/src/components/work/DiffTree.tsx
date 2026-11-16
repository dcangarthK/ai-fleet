import { useMemo, useState } from "react";
import { get } from "../../lib/api";
import { useRun } from "../../lib/data";
import { diffAnchor, splitDiff } from "../../lib/work";
import { Button } from "../ui";
import { Diff, ErrorBanner } from "../bits";

/** File tree of an attempt's diff: +/- counts per file, a click scrolls to that file's changes. */
export function DiffTree({ taskId }: { taskId: string }) {
  const { run, busy, error, clearError } = useRun();
  const [loaded, setLoaded] = useState<{ id: string; diff: string } | null>(null);
  const shown = loaded?.id === taskId;
  const files = useMemo(() => (loaded && loaded.id === taskId ? splitDiff(loaded.diff) : []), [loaded, taskId]);

  async function load() {
    const r = await run(() => get<{ stat: string; diff: string }>(`/api/tasks/${taskId}/diff`));
    if (r) setLoaded({ id: taskId, diff: r.diff ?? "" });
  }
  function jump(path: string) {
    const el = document.getElementById(diffAnchor(path));
    el?.scrollIntoView?.({ behavior: "smooth", block: "start" });
    el?.focus?.();
  }

  return (
    <div className="stack" style={{ gap: 8 }}>
      <ErrorBanner error={error} onDismiss={clearError} />
      {!shown && <div><Button disabled={busy} onClick={load}>Show changes</Button></div>}
      {shown && files.length === 0 && <p className="muted">This attempt has no changes.</p>}
      {shown && files.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-start" }}>
          <ul aria-label="Changed files" style={{ listStyle: "none", margin: 0, padding: 0, flex: "1 1 220px", minWidth: 0, maxWidth: "100%" }}>
            {files.map((f) => (
              <li key={f.path}>
                <button type="button" className="linklike mono wrap-any" onClick={() => jump(f.path)}>
                  {f.path} <span>+{f.added}</span> <span>-{f.removed}</span>
                </button>
              </li>
            ))}
          </ul>
          <div style={{ flex: "3 1 280px", minWidth: 0, maxWidth: "100%", overflow: "auto" }}>
            {files.map((f) => (
              <section key={f.path} id={diffAnchor(f.path)} tabIndex={-1} aria-label={f.path}>
                <h3 className="mono wrap-any">{f.path}</h3>
                <Diff text={f.text} />
              </section>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
