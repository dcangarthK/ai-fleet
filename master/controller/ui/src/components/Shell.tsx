import { useCallback, useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { BUSY, useCan, useFleet, useLiveUpdates, useModels, useTasks } from "../lib/data";
import { INSTALL_LABEL, needsYou, progressFraction } from "../lib/format";
import { breadcrumbs, NAV_GROUPS } from "../lib/nav";
import { HelpDialog, Palette, useShortcuts } from "./Palette";
import { NewTask } from "./NewTask";
import { Icon } from "./icons";
import { Button, IconButton } from "./ui";

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

  const waiting = (tasks.data ?? []).filter(needsYou).length;
  const crumbs = breadcrumbs(loc.pathname);

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

  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>

      <aside className={`side${drawer ? " open" : ""}`} id="rail" ref={railRef} aria-label="Primary">
        <div className="brand"><i /><span className="brand-text">Fleet console</span></div>
        <nav aria-label="Pages" style={{ display: "flex", flexDirection: "column", gap: "var(--s5)" }}>
          {NAV_GROUPS.map((g) => (
            <div className="group" key={g.id} role="group" aria-label={g.label}>
              <h3>{g.label}</h3>
              <div className="nav">
                {g.pages.map((p) => {
                  const count = p.id === "now" ? waiting : 0;
                  return (
                    <NavLink key={p.id} to={p.to} end={p.end} title={p.label} aria-label={count ? `${p.label}, ${count} need you` : p.label}
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

      <div className="main-col">
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
              <span className={`light sse ${live ? "on" : ""}`} />
              <span className="live-text">{live ? "live" : "offline"}</span>
            </span>
            {waiting > 0 && <Link className="needs-you" to="/">{waiting} need you</Link>}
            <button type="button" className="cmdk" aria-label="Open command palette" onClick={openPalette}>
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
    <div className="ops" role="status" aria-live="polite">
      {ops.map(({ m, node, n }) => {
        const frac = progressFraction(n.progress);
        return (
          <Link key={m.id + node} to="/models" className="op" style={{ color: "inherit", textDecoration: "none" }}>
            <span>{INSTALL_LABEL[n.state ?? "pending"].label} <b>{m.display_name || m.reference}</b> on {name(node)}</span>
            <span className="bar"><span style={{ width: frac == null ? "30%" : `${Math.round(frac * 100)}%` }} className={frac == null ? "indeterminate" : ""} /></span>
          </Link>
        );
      })}
    </div>
  );
}
