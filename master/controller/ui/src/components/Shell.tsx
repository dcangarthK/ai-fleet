import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { BUSY, useCan, useFleet, useLiveUpdates, useModels, useTasks } from "../lib/data";
import { INSTALL_LABEL, needsYou, progressFraction } from "../lib/format";
import { breadcrumbs, NAV_GROUPS } from "../lib/nav";
import { HelpDialog, Palette, useShortcuts } from "./Palette";
import { NewTask } from "./NewTask";
import { Icon } from "./icons";
import { Button, IconButton, ProgressBar } from "./ui";
import { useCountUp } from "./motion/useCountUp";
import { useLiveSignal } from "./motion/useLiveSignal";
import { LiveAnnouncer } from "./motion/LiveAnnouncer";
import "./motion/now-motion.css";

export function Shell() {
  const live = useLiveUpdates();
  const tasks = useTasks();
  const can = useCan();
  const navigate = useNavigate();
  const loc = useLocation();
  const [drawer, setDrawer] = useState(false);
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);
  const [spawning, setSpawning] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const railRef = useRef<HTMLElement>(null);
  const mainColRef = useRef<HTMLDivElement>(null);

  const waiting = (tasks.data ?? []).filter(needsYou).length;
  const crumbs = breadcrumbs(loc.pathname);
  // Motion sources: tasks.dataUpdatedAt moves on every controller-driven refetch (SSE event or the 30s poll);
  // `waiting` is the real count. Both are baselined on first render, so nothing animates on load.
  const pulse = useLiveSignal(tasks.dataUpdatedAt, 1700);
  const waitingShown = Math.round(useCountUp(waiting));
  const chipSettle = useLiveSignal(waiting, 400);

  const openPalette = useCallback(() => setPalette(true), []);
  const openHelp = useCallback(() => setHelp(true), []);
  useShortcuts({ openPalette, openHelp, blocked: palette || help || spawning });

  // The drawer closes on navigation and on Escape, and gives focus back to its toggle.
  useEffect(() => setDrawer(false), [loc.pathname]);
  useEffect(() => {
    if (!drawer) return;
    railRef.current?.querySelector<HTMLElement>("a")?.focus();
    const on = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setDrawer(false); toggleRef.current?.focus(); }
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, [drawer]);

  // While the drawer is open nothing behind it can take focus.
  useEffect(() => {
    const col = mainColRef.current;
    if (col) col.inert = drawer;
    return () => { if (col) col.inert = false; };
  }, [drawer]);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <LiveAnnouncer />

      <aside className={`side${drawer ? " open" : ""}`} id="rail" ref={railRef} aria-label="Primary">
        <div className="brand"><i /><span className="brand-text">Fleet console</span></div>
        <nav aria-label="Pages" className="side-nav">
          {NAV_GROUPS.map((g) => (
            <div className="group" key={g.id} role="group" aria-label={g.label}>
              <h3>{g.label}</h3>
              <div className="nav">
                {g.pages.map((p) => {
                  const count = p.id === "now" ? waiting : 0;
                  return (
                    <NavLink key={p.id} to={p.to} end={p.end} viewTransition title={p.label} aria-label={count ? `${p.label}, ${count} need you` : p.label}
                             className={({ isActive }) => (isActive ? "active" : undefined)}>
                      <Icon name={p.icon} />
                      <span className="label">{p.label}</span>
                      {count > 0 && <span className="count" aria-hidden="true">{count}</span>}
                    </NavLink>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>
        <div className="side-foot">
          <div className="kbd-row"><span>Jump</span><span><kbd>g</kbd> <kbd>n</kbd> · <kbd>g</kbd> <kbd>w</kbd> · <kbd>g</kbd> <kbd>b</kbd></span></div>
          <div className="kbd-row"><span>Help</span><kbd>?</kbd></div>
        </div>
      </aside>
      <div className={`scrim${drawer ? " open" : ""}`} onClick={() => setDrawer(false)} aria-hidden="true" />

      <div className="main-col" ref={mainColRef}>
        <header className="top">
          <IconButton ref={toggleRef} className="menu-btn" label={drawer ? "Close navigation" : "Open navigation"} aria-expanded={drawer} aria-controls="rail" onClick={() => setDrawer((d) => !d)}>
            <Icon name="menu" />
          </IconButton>
          <nav className="crumbs" aria-label="Breadcrumb">
            {crumbs.map((c, i) => {
              const last = i === crumbs.length - 1;
              return (
                <span key={`${c.label}-${i}`} style={{ display: "contents" }}>
                  {i > 0 && <span className="sep" aria-hidden="true">/</span>}
                  {last ? <b aria-current="page" title={c.label}>{c.label}</b>
                    : c.to ? <Link to={c.to}>{c.label}</Link>
                    : <span className={i === 0 && crumbs.length > 1 ? "crumb-group" : undefined}>{c.label}</span>}
                </span>
              );
            })}
          </nav>
          <div className="top-actions">
            <span className="live-dot" title={live ? "Live updates connected" : "Live updates disconnected; refreshing every 30s"}>
              <span className={`light sse ${live ? "on" : ""}${live && pulse ? " pulse" : ""}`} />
              <span className="live-text">{live ? "live" : "offline"}</span>
            </span>
            {waiting > 0 && <Link className={`needs-you${chipSettle ? " settle" : ""}`} to="/" aria-label={`${waiting} need you`}>{waitingShown} need you</Link>}
            <button type="button" className="cmdk" aria-label="Search pages, tasks, actions" aria-keyshortcuts="Meta+K Control+K" onClick={openPalette}>
              <Icon name="search" className="ico cmdk-icon" />
              <span className="cmdk-text">Search pages, tasks, actions</span>
              <kbd>⌘K</kbd>
            </button>
            {can.operate && (
              <Button variant="primary" onClick={() => setSpawning(true)} aria-label="Spawn task">
                <Icon name="plus" /><span className="spawn-text">Spawn task</span>
              </Button>
            )}
          </div>
        </header>

        <main className="main" id="main" tabIndex={-1}>
          <InstallStrip />
          <Outlet />
        </main>
      </div>

      <Palette open={palette} onClose={() => setPalette(false)} />
      <HelpDialog open={help} onClose={() => setHelp(false)} />
      <NewTask open={spawning} onClose={() => setSpawning(false)} onCreated={(t) => { setSpawning(false); navigate(`/work?task=${encodeURIComponent(t.id)}`); }} />
    </div>
  );
}

/** Model installs and removals in progress, shown above every page until they finish. */
function InstallStrip() {
  const models = useModels();
  const fleet = useFleet();
  const ops = (models.data ?? []).flatMap((m) =>
    Object.entries(m.nodes).filter(([, n]) => n.state && BUSY.has(n.state)).map(([node, n]) => ({ m, node, n })));
  if (!ops.length) return null;
  const name = (id: string) => fleet.data?.nodes.find((x) => x.id === id)?.name ?? id;
  return (
    <div className="install-strip" role="status" aria-live="polite">
      {ops.map(({ m, node, n }) => {
        const frac = progressFraction(n.progress);
        const what = `${INSTALL_LABEL[n.state ?? "pending"].label} ${m.display_name || m.reference} on ${name(node)}`;
        return (
          <Link key={m.id + node} to="/models" className="op">
            <span>{INSTALL_LABEL[n.state ?? "pending"].label} <b>{m.display_name || m.reference}</b> on {name(node)}</span>
            <ProgressBar value={frac} label={what} />
          </Link>
        );
      })}
    </div>
  );
}
