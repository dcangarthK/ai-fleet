import { useCallback, useMemo, useState } from "react";
import { ErrorBanner, PageHead } from "../components/bits";
import { ErrorState, Loading } from "../components/ui";
import { IntentBar, IntentHost, intentCtx } from "../components/now/Intent";
import "../components/now/intentProvider";
import { Anomalies, Briefing, Forecasts, NextActions, Operations, Section } from "../components/now/Sections";
import { useNowData } from "../components/now/data";
import "../components/now/now.css";
import { Constellation } from "../components/motion/Constellation";
import { useQueryClient } from "@tanstack/react-query";
import { useRun } from "../lib/data";
import { anomalies, briefing, forecasts, nextActions } from "../lib/insights";

/** Now: the briefing, ranked next actions, forecasts and anomalies, then the live operations tables. Nothing auto-executes. */
export function NowPage() {
  const d = useNowData();
  const qc = useQueryClient();
  const { run, error: runError, clearError } = useRun();
  const [intentError, setIntentError] = useState<string | null>(null);
  const onIntentError = useCallback((m: string | null) => setIntentError(m), []);

  const { inputs } = d;
  const view = useMemo(() => ({
    brief: briefing(inputs), actions: nextActions(inputs), fc: forecasts(inputs), an: anomalies(inputs),
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [d.tasks.data, d.live.data, d.budget.data, d.inputs.tokenRows, d.inputs.spend, d.inputs.admin]);
  const ctx = useMemo(() => intentCtx((k) => qc.getQueryData(k)), [qc, d.tasks.data, d.fleet.data, d.budget.data]);

  return (
    <>
      <PageHead title="Now" intro="What needs you, what is likely next, and the state of the fleet. Every proposal shows its evidence and waits for your confirmation." />
      <Constellation fleet={d.fleet.data} live={d.live.data} />
      <IntentHost onError={onIntentError} />
      <div className="now">
        <ErrorBanner error={runError ?? intentError} onDismiss={() => { clearError(); setIntentError(null); }} />
        <IntentBar ctx={ctx} onError={onIntentError} />
        {d.loading ? <Loading rows={5} label="Loading the fleet state" />
          : d.error ? <ErrorState error={d.error} onRetry={d.retry} title="Couldn't load the fleet state" />
          : (
            <>
              <Briefing data={view.brief} />
              <Section title="Next best actions"><NextActions items={view.actions} run={run} /></Section>
              <Section title="Forecasts"><Forecasts items={view.fc} /></Section>
              <Section title="Anomalies"><Anomalies items={view.an} /></Section>
              {d.live.data && <Section title="Operations"><Operations live={d.live.data} /></Section>}
            </>
          )}
      </div>
    </>
  );
}
