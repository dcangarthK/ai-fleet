import { useEffect, useMemo, useState } from "react";
import { minuteTick } from "../lib/pageMotion";
import { useQuery } from "@tanstack/react-query";
import { get } from "../lib/api";
import { PageHead } from "../components/bits";
import { clockTime, formatTokens, modelLabel, type BudgetStatus } from "../lib/budget";
import { Card, ErrorState, Loading, TabPanel, Tabs } from "../components/ui";
import { LimitBars } from "../components/budget/LimitBars";
import { ForecastCard } from "../components/budget/ForecastCard";
import { AccountsPanel } from "../components/budget/AccountsPanel";
import { ResourcesPanel } from "../components/budget/ResourcesPanel";
import { RulesEditor } from "../components/budget/RulesEditor";
import { DecisionsList } from "../components/budget/DecisionsList";
import { ReviewersPanel, SavingsPanel, UsagePanel } from "../components/budget/UsagePanels";

const FLEET_ONLY = "Fleet tasks only: your own interactive use draws on the same limits, so real headroom is lower.";

/** One plain sentence from the status snapshot: tokens today and any model that is limited or at its soft budget. */
function summary(status: BudgetStatus | undefined): string {
  if (!status) return "";
  const total = status.models.reduce((s, m) => s + m.today_tokens, 0);
  const limited = status.models.filter((m) => m.state === "limited");
  const soft = status.models.filter((m) => m.state === "soft");
  const parts = [`${formatTokens(total)} tokens today across ${status.models.length} paid model${status.models.length === 1 ? "" : "s"}.`];
  for (const m of limited) parts.push(`${modelLabel(m.key)} is limited${m.limited_until ? ` until ${clockTime(m.limited_until)}` : ""}.`);
  for (const m of soft) parts.push(`${modelLabel(m.key)} is at its soft budget.`);
  if (!limited.length && !soft.length && status.models.length) parts.push("Nothing is limited.");
  return parts.join(" ");
}

export function BudgetPage() {
  const status = useQuery({ queryKey: ["budget", "status"], queryFn: () => get<BudgetStatus>("/api/budget/status") });
  const [tab, setTab] = useState("rules");
  // Reset countdowns re-render once a minute (plain text, no animation); the minute tick is the only source.
  const [minute, setMinute] = useState(() => minuteTick(Date.now()));
  useEffect(() => {
    const id = setInterval(() => setMinute(minuteTick(Date.now())), 60_000);
    return () => clearInterval(id);
  }, []);
  const now = useMemo(() => Math.floor(Date.now() / 1000), [status.dataUpdatedAt, minute]); // eslint-disable-line react-hooks/exhaustive-deps
  const s = status.data;
  const shared = (s?.profiles ?? []).filter((p) => p.codex_profile === "shared");

  return (
    <>
      <PageHead title="Budget" intro={<>{summary(s) || "Paid-model spend, the rules the controller enforces, and what local models saved."} {FLEET_ONLY}</>} />

      {status.error && <ErrorState error={status.error} onRetry={() => status.refetch()} title="Couldn't load budget status" />}
      {s?.rules_invalid && (
        <div className="banner error" role="alert">
          Stored rules invalid; defaults in force. {s.rules_error}. Fix them under Rules or restore an earlier version.
        </div>
      )}
      {shared.length > 0 && (
        <div className="banner note" role="status">
          Codex fleet tasks use your own Codex profile on {shared.map((p) => p.name).join(", ")} (plugins, memories and its default effort included).
          Sign the fleet profile in once: <span className="mono">CODEX_HOME=~/.codex-fleet codex login</span>
        </div>
      )}

      <div className="stack" style={{ gap: "var(--s5)" }}>
        {status.isLoading ? <Loading rows={4} label="Loading budget status" /> : <LimitBars status={s} now={now} loading={false} />}
        <ForecastCard status={s} now={now} />
        <AccountsPanel />
        <ResourcesPanel />

        <Card bodyClassName="stack">
          <Tabs
            label="Budget detail" value={tab} onChange={setTab} idPrefix="budget"
            tabs={[
              { id: "rules", label: "Rules" },
              { id: "decisions", label: "Decisions" },
              { id: "usage", label: "Usage" },
              { id: "savings", label: "Savings and local reviewers" },
            ]}
          />
          <TabPanel id="rules" value={tab} idPrefix="budget"><RulesEditor /></TabPanel>
          <TabPanel id="decisions" value={tab} idPrefix="budget"><DecisionsList /></TabPanel>
          <TabPanel id="usage" value={tab} idPrefix="budget"><UsagePanel /></TabPanel>
          <TabPanel id="savings" value={tab} idPrefix="budget">
            <div className="stack"><SavingsPanel /><ReviewersPanel /></div>
          </TabPanel>
        </Card>
      </div>
    </>
  );
}
