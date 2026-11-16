/** Command palette model: fuzzy scoring and the provider registry (the extension point for intents and search). */
import { NAV_GROUPS } from "./nav";

export interface PaletteItem {
  id: string;
  title: string;
  /** Right-aligned secondary text (group, shortcut). */
  hint?: string;
  /** Extra words that should match but aren't shown. */
  keywords?: string;
  group?: string;
  /** Navigate here when chosen... */
  to?: string;
  /** ...or run this. */
  run?: () => void;
}

export interface PaletteProvider {
  id: string;
  /** Items for the current query. May be async (e.g. `/api/search`). Must not execute anything itself. */
  items(query: string): PaletteItem[] | Promise<PaletteItem[]>;
}

const providers = new Map<string, PaletteProvider>();

/** Add a source of palette items. Returns an unregister function. Later intent/search providers plug in here. */
export function registerPaletteProvider(p: PaletteProvider): () => void {
  providers.set(p.id, p);
  return () => {
    if (providers.get(p.id) === p) providers.delete(p.id);
  };
}

export const getPaletteProviders = (): PaletteProvider[] => [...providers.values()];

/** Subsequence match score; 0 = no match, higher = better. Prefix and word-start hits rank first. */
export function fuzzyScore(query: string, text: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 1;
  const t = text.toLowerCase();
  if (t === q) return 1000;
  if (t.startsWith(q)) return 800 - t.length;
  const at = t.indexOf(q);
  if (at >= 0) return (t[at - 1] === " " ? 600 : 400) - at;
  let ti = 0;
  let score = 0;
  let streak = 0;
  for (const ch of q) {
    const i = t.indexOf(ch, ti);
    if (i < 0) return 0;
    streak = i === ti ? streak + 1 : 0;
    score += 1 + streak * 2 + (i === 0 || t[i - 1] === " " ? 3 : 0);
    ti = i + 1;
  }
  return Math.max(1, score);
}

export function rankItems(items: PaletteItem[], query: string): PaletteItem[] {
  return items
    .map((it) => ({ it, s: Math.max(fuzzyScore(query, it.title), fuzzyScore(query, it.keywords ?? "") * 0.5) }))
    .filter((x) => x.s > 0)
    .sort((a, b) => b.s - a.s)
    .map((x) => x.it);
}

/** Built-in provider: every page from nav.ts. */
const PAGE_ITEMS: PaletteItem[] = NAV_GROUPS.flatMap((g) =>
  g.pages.map((p) => ({
    id: `page:${p.id}`, title: p.label, group: "Pages", to: p.to,
    hint: p.go ? `${g.label} · g ${p.go}` : g.label, keywords: g.label,
  })),
);

export const pagesProvider: PaletteProvider = {
  id: "pages",
  items: (query) => rankItems(PAGE_ITEMS, query),
};
registerPaletteProvider(pagesProvider);
