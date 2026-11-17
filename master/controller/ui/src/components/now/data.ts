import { useQuery } from "@tanstack/react-query";
import { get, post, put, del, api, type Task } from "../../lib/api";
import { useCan, useFleet, useRun, useTasks } from "../../lib/data";
import type { BudgetStatus } from "../../lib/budget";
import type { Inputs } from "../../lib/insights";
import type { Live, TokenRow, TokenSpend } from "../../lib/live";

/** Everything the Now page reads, through the same query keys the Live and Budget pages use so the cache is shared. */
export function useNowData() {
  const tasks = useTasks();
  const live = useQuery({ queryKey: ["live", false], queryFn: () => get<Live>("/api/live"), refetchInterval: 20_000 });
  const budget = useQuery({ queryKey: ["budget", "status"], queryFn: () => get<BudgetStatus>("/api/budget/status") });
  const rows = useQuery({ queryKey: ["tokens"], queryFn: () => get<{ tasks: TokenRow[] }>(`/api/tokens?since=${Math.floor(Date.now() / 1000) - 86400}&limit=30`), retry: 0 });
  const spend = useQuery({ queryKey: ["tokens-spend"], queryFn: () => get<TokenSpend>("/api/tokens/summary"), retry: 0 });
  const fleet = useFleet();
  const can = useCan();
  const now = Math.floor(Date.now() / 1000);
  const inputs: Inputs = {
    now, tasks: (tasks.data ?? []) as Task[], live: live.data ?? null, budget: budget.data ?? null,
    tokenRows: rows.data?.tasks, spend: spend.data ?? null, admin: can.admin,
  };
  return {
    inputs, tasks, live, budget, fleet, can,
    loading: tasks.isLoading || live.isLoading || budget.isLoading,
    error: tasks.error ?? live.error ?? budget.error,
    retry: () => { tasks.refetch(); live.refetch(); budget.refetch(); },
  };
}

export { api, post, put, del, useRun };
