import { useEffect, useRef, useState } from "react";
import { Card, CardGrid, Empty, StatusPill } from "../ui";
import { useCountUp } from "../motion/useCountUp";
import { createCrossingLatch } from "../../lib/pageMotion";
import { clockTime, concurrencyText, formatTokens, meter, modelLabel, statusCard, type BudgetStatus, type ModelStatus, type RateWindow } from "../../lib/budget";
import { countdown } from "../../lib/forecast";
import { Bar, usedTone } from "./Bar";

function WindowBar({ name, w, now, account }: { name: string; w: RateWindow; now: number; account: string }) {
  const used = Math.round(useCountUp(w.used_percent)); // Count: source is the account's rate-limit window
  return (
    <div style={{ display: "grid", gap: 4 }}>
      <div className="row" style={{ justifyContent: "space-between", gap: 8 }}>
        <span className="small quiet">{name}</span>
        <span className="small" style={{ fontVariantNumeric: "tabular-nums" }}>
          {used}% used · resets in {countdown(w.resets_at - now)} <span className="quiet">({clockTime(w.resets_at)})</span>
        </span>
      </div>
      <Bar pct={w.used_percent} tone={usedTone(w.used_percent)} label={`${account} ${name} window`} valueText={`${used} percent used`} />
    </div>
  );
}

function ModelCard({ m, now }: { m: ModelStatus; now: number }) {
  const card = statusCard(m);
  const gauge = meter(m.today_tokens, m.soft, m.hard);
  // Soft-budget crossing: observed per status update, latched so it fires once and is never re-armed.
  const latch = useRef(createCrossingLatch());
  const [crossed, setCrossed] = useState(false);
  useEffect(() => {
    if (latch.current.observe(m.key, m.today_tokens, m.soft)) setCrossed(true);
  }, [m.key, m.today_tokens, m.soft]);
  const accounts = m.accounts ?? [];
  return (
    <Card
      title={modelLabel(m.key)}
      actions={<StatusPill tone={card.tone}>{card.label}</StatusPill>}
      className="limit-card"
    >
      <div className="stack">
        <div className="small quiet mono wrap-any">{m.key} · {concurrencyText(m)}</div>
        <div style={{ display: "grid", gap: 4 }}>
          <Bar pct={gauge.pct == null ? null : gauge.pct * 100} tone={gauge.tone} label={`${modelLabel(m.key)} tokens today`} valueText={gauge.label} crossed={crossed} />
          <span className="small quiet">Today: {gauge.label}</span>
        </div>
        {accounts.map((a) => {
          const windows = [["5-hour", a.rate_limits?.primary], ["Weekly", a.rate_limits?.secondary]] as const;
          return (
            <div key={a.account} className="stack" style={{ gap: 6, paddingTop: 8, borderTop: "1px solid var(--line)" }} data-testid="status-account">
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="mono small">{a.label ?? a.account}</span>
                <StatusPill tone={a.state === "limited" ? "bad" : "ok"}>
                  {a.state === "limited" ? `limited${a.limited_until ? ` until ${clockTime(a.limited_until)}` : ""}` : "ok"}
                </StatusPill>
              </div>
              {windows.map(([name, w]) => w && <WindowBar key={name} name={name} w={w} now={now} account={a.label ?? a.account} />)}
              {!a.rate_limits?.primary && !a.rate_limits?.secondary && <span className="small quiet">No rate-limit windows reported for this account.</span>}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/** Per-model limit cards: tokens today against the daily budget, and each account's rate-limit windows with reset countdowns. */
export function LimitBars({ status, now, loading }: { status?: BudgetStatus; now: number; loading: boolean }) {
  const models = status?.models ?? [];
  if (!loading && models.length === 0) {
    return <Card><Empty title="No paid models in the budget rules">Add Codex or a Claude model under Rules to see its limits here.</Empty></Card>;
  }
  return (
    <CardGrid min={300}>
      {models.map((m) => <ModelCard key={m.key} m={m} now={now} />)}
      {loading && <span className="sr-only">Loading limits…</span>}
      {status && <span className="sr-only">{formatTokens(models.reduce((s, m) => s + m.today_tokens, 0))} tokens today</span>}
    </CardGrid>
  );
}
