import type { NavIcon } from "../lib/nav";

const PATHS: Record<NavIcon | "menu" | "search" | "plus", string> = {
  now: "M3 12h4l3-8 4 16 3-8h4",
  work: "M4 6h16M4 12h16M4 18h10",
  plan: "M5 4h14v16H5zM9 9h6M9 13h6M9 17h3",
  budget: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  nodes: "M4 5h16v6H4zM4 13h16v6H4zM8 8h.01M8 16h.01",
  models: "M12 3l8 4.5v9L12 21l-8-4.5v-9zM12 12l8-4.5M12 12v9M12 12L4 7.5",
  roles: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0",
  policies: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  skills: "M13 3L4 14h7l-1 7 9-11h-7z",
  evals: "M9 3h6M10 3v6l-5 9a2 2 0 002 3h10a2 2 0 002-3l-5-9V3",
  activity: "M12 7v5l3 2M12 21a9 9 0 100-18 9 9 0 000 18z",
  settings: "M4 7h10M18 7h2M4 17h2M10 17h10M16 4v6M8 14v6",
  menu: "M4 6h16M4 12h16M4 18h16",
  search: "M11 18a7 7 0 100-14 7 7 0 000 14zM21 21l-5-5",
  plus: "M12 5v14M5 12h14",
};

export function Icon({ name, className = "ico" }: { name: NavIcon | "menu" | "search" | "plus"; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={PATHS[name]} />
    </svg>
  );
}
