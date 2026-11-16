import { useQuery } from "@tanstack/react-query";
import { get, post, put } from "../../lib/api";
import { useCan, useRun } from "../../lib/data";
import { clockTime } from "../../lib/budget";
import { accountTone, type Account, type Live } from "../../lib/live";
import { ErrorBanner } from "../bits";
import { Button, Card, Empty, ErrorState, Loading, StatusPill, Table, useConfirm, type Column } from "../ui";

interface Row { account: string; agent: string; label: string | null; live: Account | null; held: boolean }

/**
 * Accounts with their limit state, plus the owner's two account controls: "Limit reset by owner" and the budget hold
 * (PUT /api/budget/rules/hold, one account at a time, admin only). A hold freezes the account: placement moves
 * nothing onto or off it; running tasks finish.
 */
export function AccountsPanel() {
  const live = useQuery({ queryKey: ["live"], queryFn: () => get<Live>("/api/live"), refetchInterval: 30_000 });
  const hold = useQuery({ queryKey: ["budget", "hold"], queryFn: () => get<{ hold: string[] }>("/api/budget/rules/hold") });
  const admin = useCan().admin;
  const confirm = useConfirm();
  const { run, busy, error, clearError } = useRun();

  const held = new Set(hold.data?.hold ?? []);
  const rows: Row[] = (live.data?.accounts ?? []).map((a) => ({ account: a.account, agent: a.agent, label: a.label, live: a, held: held.has(a.account) }));
  for (const key of held) if (!rows.some((r) => r.account === key)) rows.push({ account: key, agent: key.split(/[@/]/)[0], label: null, live: null, held: true });

  async function toggleHold(r: Row) {
    const on = !r.held;
    const name = r.label ?? r.account;
    const ok = await confirm({
      title: on ? `Hold ${name}` : `Release ${name}`,
      message: (
        <div className="stack">
          <div><b>Exact request</b><pre className="mono small wrap-any" style={{ whiteSpace: "pre-wrap", margin: "4px 0 0" }}>{`PUT /api/budget/rules/hold\n{ "account": "${r.account}", "hold": ${on} }`}</pre></div>
          <p>{on
            ? "Placement will move nothing onto or off this account. Tasks already running finish. Release it here when you want it used again."
            : "Placement may use this account again."}</p>
        </div>
      ),
      confirmLabel: on ? "Hold account" : "Release account",
    });
    if (ok) run(() => put("/api/budget/rules/hold", { account: r.account, hold: on }));
  }

  async function resetLimit(r: Row) {
    const name = r.label ?? r.account;
    const ok = await confirm({
      title: "Limit reset by owner",
      message: `Did you reset the usage limit of ${name} yourself? The fleet will treat its limits up to now as cleared.`,
      confirmLabel: "Yes, I reset it",
    });
    if (ok) run(() => post("/api/budget/accounts/reset", { account: r.account }));
  }

  const columns: Column<Row>[] = [
    { key: "account", header: "Account", cell: (r) => <>{r.agent} · {r.label ?? <span className="mono">{r.account}</span>}</>, truncate: true },
    {
      key: "status", header: "Status",
      cell: (r) => r.live ? <StatusPill tone={accountTone(r.live)}>{r.live.status}</StatusPill> : <span className="quiet">not seen</span>,
    },
    {
      key: "last", header: "Last limit", hideStacked: true,
      cell: (r) => r.live?.last_limit ? `${clockTime(r.live.last_limit.at)}${r.live.last_limit.resets_at ? `, resets ${clockTime(r.live.last_limit.resets_at)}` : ""}` : "—",
    },
    { key: "running", header: "Running", numeric: true, cell: (r) => r.live?.running ?? 0 },
    {
      key: "hold", header: "Hold",
      cell: (r) => (
        <button
          type="button" role="switch" aria-checked={r.held} aria-label={`Budget hold for ${r.label ?? r.account}`}
          disabled={!admin || busy || hold.isLoading} onClick={() => toggleHold(r)}
          title={admin ? undefined : "Admin only"}
          className="row" style={{ gap: 8 }}
        >
          <span aria-hidden="true" style={{ width: 28, height: 14, borderRadius: 7, background: r.held ? "var(--warn)" : "var(--sunken)", border: "1px solid var(--line)", position: "relative", flex: "none" }}>
            <span style={{ position: "absolute", top: 1, left: r.held ? 15 : 1, width: 10, height: 10, borderRadius: "50%", background: "var(--ink)" }} />
          </span>
          {r.held ? "On" : "Off"}
        </button>
      ),
    },
    {
      key: "reset", header: "", label: "Actions",
      cell: (r) => admin && r.live && (r.live.limited_until || r.live.status.startsWith("limit hit")) ? (
        <Button size="sm" disabled={busy} onClick={() => resetLimit(r)}>Limit reset by owner</Button>
      ) : null,
    },
  ];

  return (
    <Card title="Accounts" actions={<span className="small quiet">limit state from real task output</span>}>
      <ErrorBanner error={error} onDismiss={clearError} />
      <p className="small quiet" style={{ marginBottom: 8 }}>
        Hold freezes one account: nothing is placed on or moved off it, and running tasks finish. Local roles are unaffected.
        {!admin && " Only an admin token can change it."}
      </p>
      {live.isLoading || hold.isLoading ? <Loading rows={3} label="Loading accounts" /> :
        live.error ? <ErrorState error={live.error} onRetry={() => live.refetch()} /> :
        hold.error ? <ErrorState error={hold.error} title="Couldn't load holds" onRetry={() => hold.refetch()} /> :
        rows.length === 0 ? <Empty title="No paid accounts yet" /> :
        <Table columns={columns} rows={rows} rowKey={(r) => r.account} caption="Accounts and holds" />}
    </Card>
  );
}
