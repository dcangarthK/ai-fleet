/** Navigation model (docs/SPEC-ui-revamp.md section 3): groups, pages and the legacy redirect map. */

export type NavIcon =
  | "now" | "work" | "lineage" | "plan" | "budget" | "nodes" | "models" | "roles"
  | "policies" | "skills" | "evals" | "activity" | "settings";

export interface NavPage {
  id: string;
  to: string;
  label: string;
  icon: NavIcon;
  /** Second key of a `g <key>` jump. */
  go?: string;
  /** Match only the exact path (for `/`). */
  end?: boolean;
}

export interface NavGroup {
  id: string;
  label: string;
  pages: NavPage[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    id: "operate", label: "Operate",
    pages: [
      { id: "now", to: "/", label: "Now", icon: "now", go: "n", end: true },
      { id: "work", to: "/work", label: "Work", icon: "work", go: "w" },
      { id: "lineage", to: "/lineage", label: "Lineage", icon: "lineage", go: "l" },
      { id: "plan", to: "/plan", label: "Plan", icon: "plan", go: "p" },
    ],
  },
  { id: "spend", label: "Spend", pages: [{ id: "budget", to: "/budget", label: "Budget", icon: "budget", go: "b" }] },
  {
    id: "fleet", label: "Fleet",
    pages: [
      { id: "nodes", to: "/nodes", label: "Nodes", icon: "nodes" },
      { id: "models", to: "/models", label: "Models", icon: "models" },
      { id: "roles", to: "/roles", label: "Roles", icon: "roles" },
    ],
  },
  {
    id: "configure", label: "Configure",
    pages: [
      { id: "policies", to: "/policies", label: "Policies", icon: "policies" },
      { id: "skills", to: "/skills", label: "Skills", icon: "skills" },
      { id: "evals", to: "/evals", label: "Evals", icon: "evals" },
      { id: "activity", to: "/activity", label: "Activity", icon: "activity" },
      { id: "settings", to: "/settings", label: "Settings", icon: "settings" },
    ],
  },
];

export const NAV_PAGES: NavPage[] = NAV_GROUPS.flatMap((g) => g.pages);

/** Old route -> new home (section 3). */
export const LEGACY_REDIRECTS: Record<string, string> = {
  "/live": "/",
  "/overview": "/",
  "/tasks": "/work",
  "/pipelines": "/work",
};

function trimSlash(p: string): string {
  return p.length > 1 ? p.replace(/\/+$/, "") : p;
}

/** Where a legacy path should go, or null when it isn't legacy. `/tasks?task=x` keeps its `task` parameter. */
export function resolveLegacy(pathname: string, search = ""): string | null {
  const path = trimSlash(pathname);
  const target = LEGACY_REDIRECTS[path];
  if (!target) return null;
  if (path === "/tasks") {
    const task = new URLSearchParams(search).get("task");
    if (task) return `${target}?task=${encodeURIComponent(task)}`;
  }
  return target;
}

export function pageForPath(pathname: string): NavPage | undefined {
  const path = trimSlash(pathname);
  return NAV_PAGES.find((p) => (p.end ? path === p.to : path === p.to || path.startsWith(p.to + "/")));
}

export function groupOf(page: NavPage): NavGroup | undefined {
  return NAV_GROUPS.find((g) => g.pages.includes(page));
}

export interface Crumb {
  label: string;
  to?: string;
}

/** Breadcrumb for the top bar: group, page, and the id for detail routes. */
export function breadcrumbs(pathname: string): Crumb[] {
  const page = pageForPath(pathname);
  if (!page) return [{ label: "Not found" }];
  const group = groupOf(page);
  const crumbs: Crumb[] = group ? [{ label: group.label }] : [];
  const rest = trimSlash(pathname).slice(page.to.length === 1 ? 0 : page.to.length).split("/").filter(Boolean);
  if (rest.length === 0) return [...crumbs, { label: page.label }];
  return [...crumbs, { label: page.label, to: page.to }, { label: decodeURIComponent(rest[0]) }];
}

/** `g <key>` -> path, derived from the page list so the two can't drift. */
export const GO_TARGETS: Record<string, string> = Object.fromEntries(
  NAV_PAGES.filter((p) => p.go).map((p) => [p.go as string, p.to]),
);
