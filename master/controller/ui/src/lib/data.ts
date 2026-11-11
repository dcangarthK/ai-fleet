import { useEffect, useState } from "react";
import { QueryClient, useQuery, useQueryClient } from "@tanstack/react-query";
import type { WorkItem } from "./lineage";
import { ApiError, get, post, type CatalogModel, type Fleet, type FleetEvent, type Me, type Task, type UsageRow } from "./api";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 5_000, refetchInterval: 30_000, retry: 1, refetchOnWindowFocus: true },
  },
});

export const useFleet = () => useQuery({ queryKey: ["fleet"], queryFn: () => get<Fleet>("/api/fleet") });
export const useTasks = () => useQuery({ queryKey: ["tasks"], queryFn: () => get<Task[]>("/api/tasks?limit=300") });
export const useWork = (includeHidden: boolean) =>
  useQuery({ queryKey: ["work", includeHidden], queryFn: () => get<WorkItem[]>(`/api/work?limit=100&include_hidden=${includeHidden ? 1 : 0}`) });
export const useTask = (id: string | undefined) =>
  useQuery({ queryKey: ["task", id], queryFn: () => get<Task>(`/api/tasks/${id}`), enabled: !!id });
export const useUsage = (days = 1) =>
  useQuery({ queryKey: ["usage", days], queryFn: () => get<UsageRow[]>(`/api/usage?days=${days}`) });
/** The catalog with per-node install state; polls faster while anything is installing. */
export const useModels = () =>
  useQuery({
    queryKey: ["models"],
    queryFn: () => get<CatalogModel[]>("/api/models"),
    refetchInterval: (q) => (q.state.data ?? []).some((m) => Object.values(m.nodes).some((n) => n.state && BUSY.has(n.state))) ? 1500 : 30_000,
  });
export const BUSY = new Set(["pending", "checking", "pulling", "configuring", "verifying", "draining", "removing"]);
export const useEvents = () =>
  useQuery({ queryKey: ["events"], queryFn: () => get<FleetEvent[]>("/api/events/list?after=0&limit=1000") });

/** Run a mutation, keep its error for display, then refresh every query it may have changed. */
export function useRun() {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return undefined;
    } finally {
      setBusy(false);
      qc.invalidateQueries();
    }
  }
  return { run, busy, error, clearError: () => setError(null) };
}

export const taskAction = (id: string, action: string) => post<Task>(`/api/tasks/${id}/${action}`);

/**
 * Live updates: the controller's SSE stream says *that* something changed; we refetch.
 * Debounced so a burst of events (a task finishing a gate) causes one refresh.
 */
export function useLiveUpdates(): boolean {
  const qc = useQueryClient();
  const [live, setLive] = useState(false);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const es = new EventSource("/api/events");
    es.onopen = () => setLive(true);
    es.onerror = () => setLive(false);
    es.onmessage = () => {
      clearTimeout(timer);
      timer = setTimeout(() => qc.invalidateQueries(), 250);
    };
    return () => {
      clearTimeout(timer);
      es.close();
    };
  }, [qc]);
  return live;
}

/** Incrementally follow a task's agent log while it's open. */
export function useTaskLog(id: string | undefined, active: boolean) {
  const [text, setText] = useState("");
  useEffect(() => {
    setText("");
    if (!id) return;
    let offset = 0;
    let stopped = false;
    const tick = async () => {
      try {
        const r = await get<{ offset: number; text: string }>(`/api/tasks/${id}/log?offset=${offset}`);
        if (stopped) return;
        if (r.text) setText((t) => (t + r.text).slice(-200_000));
        offset = r.offset;
      } catch {
        /* runner may be offline; keep what we have */
      }
    };
    tick();
    const h = setInterval(tick, active ? 2000 : 10000);
    return () => {
      stopped = true;
      clearInterval(h);
    };
  }, [id, active]);
  return text;
}

const RANK = { view: 0, operate: 1, admin: 2 } as const;

/**
 * What this browser's token may do. Fails closed: while the scope is loading or can't be read, mutation
 * controls stay hidden. (A controller too old to have /api/me answers 404; then show them and let the
 * server enforce, as before.)
 */
export function useCan() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => get<Me>("/api/me"), staleTime: 300_000, refetchInterval: false, retry: 0 });
  const legacy = me.error instanceof ApiError && me.error.status === 404;
  const scope = me.data?.scope;
  return {
    me: me.data,
    operate: legacy || (scope ? RANK[scope] >= RANK.operate : false),
    admin: legacy || (scope ? RANK[scope] >= RANK.admin : false),
  };
}
