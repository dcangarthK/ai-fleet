import { useQuery } from "@tanstack/react-query";
import { get, post } from "../../lib/api";
import { useCan, useRun } from "../../lib/data";
import { formatTokens } from "../../lib/budget";
import { canMarkSignedIn, slotName, slotPill, type ResourceSlot } from "../../lib/handoff";
import { ErrorBanner } from "../bits";
import { Button, Card, Empty, ErrorState, Loading, StatusPill, Table, useConfirm, type Column } from "../ui";

/** Paid accounts per node (docs/SPEC-limit-handoff.md 5). Labels only; directories are never sent. */
export function ResourcesPanel() {
  const q = useQuery({ queryKey: ["budget", "resources"], queryFn: () => get<ResourceSlot[]>("/api/resources"), refetchInterval: 30_000 });
  const admin = useCan().admin;
  const confirm = useConfirm();
  const { run, busy, error, clearError } = useRun();
  const rows = q.data ?? [];

  async function signedIn(s: ResourceSlot) {
    const ok = await confirm({ title: "Signed in again", message: `Has ${s.label ?? s.profile} on ${s.node_name ?? s.node} been signed in again?`, confirmLabel: "Yes, signed in" });
    if (ok) run(() => post("/api/resources/signed-in", { node: s.node, agent: s.agent, profile: s.profile }));
  }

  const cols: Column<ResourceSlot>[] = [
    { key: "node", header: "Node", truncate: true, cell: (s) => s.node_name ?? s.node },
    { key: "agent", header: "Agent", cell: (s) => s.agent },
    { key: "account", header: "Account", cell: (s) => s.label ?? <span className="quiet">{s.profile}</span> },
    { key: "model", header: "Model", truncate: true, cell: (s) => <span className="mono">{slotName(s)}</span> },
    {
      key: "state", header: "State",
      cell: (s) => {
        const pill = slotPill(s);
        return (
          <span className="row" style={{ gap: 8, flexWrap: "wrap" }}>
            <StatusPill tone={pill.tone}>{pill.label}</StatusPill>
            {admin && canMarkSignedIn(s) && <Button size="sm" disabled={busy} onClick={() => signedIn(s)}>Signed in again</Button>}
          </span>
        );
      },
    },
    { key: "kinds", header: "Kinds", hideStacked: true, cell: (s) => <span className="small">{s.kinds.join(", ")}</span> },
    { key: "tokens", header: "Tokens today", numeric: true, cell: (s) => formatTokens(s.today_tokens) },
  ];

  return (
    <Card title="Resources" actions={<span className="small quiet">paid accounts per node</span>}>
      <ErrorBanner error={error} onDismiss={clearError} />
      {q.isLoading ? <Loading rows={3} label="Loading resources" /> : q.error ? <ErrorState error={q.error} onRetry={() => q.refetch()} /> : rows.length === 0 ? (
        <Empty title="No paid agents on any node yet">Each runner lists its signed-in profiles in runner.json.</Empty>
      ) : <Table columns={cols} rows={rows} rowKey={(s) => `${s.node}/${s.agent}/${s.profile}/${s.model}`} caption="Paid accounts per node" />}
    </Card>
  );
}
