// Live (docs/SPEC-live-monitor.md 5): who is doing what, where, with which model, on which account, right now.
// Read-only: actions stay on the existing pages, which every row links to.
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { get } from "../lib/api";
import { LoadError, PageHead, Section } from "../components/bits";
import { ago, duration } from "../lib/format";
import { clockTime } from "../lib/budget";
import {
  accountTone, alertText, fill, liveQueryOptions, machineWarnings, presenceText, queueText, rowsClass, stageChip, stageTitle,
  workerText, type Live, type Machine,
} from "../lib/live";

function useVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === "undefined" || document.visibilityState !== "hidden");
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== "hidden");
    document.addEventListener("visibilitychange", on);
    return () => document.removeEventListener("visibilitychange", on);
  }, []);
  return visible;
}

function usePhone(): boolean {
  const query = "(max-width: 860px)";
  const [phone, setPhone] = useState(() => typeof window !== "undefined" && window.matchMedia(query).matches);
  useEffect(() => {
    const m = window.matchMedia(query);
    const on = () => setPhone(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return phone;
}

function useNow(seconds = 5): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now() / 1000), seconds * 1000);
    return () => clearInterval(id);
  }, [seconds]);
  return now;
}

export function LivePage() {
  const visible = useVisible();
  const phone = usePhone();
  const now = useNow();
  // SSE invalidation (Shell) refetches it on any event; a hidden tab neither refetches nor polls.
  const live = useQuery({ queryKey: ["live"], queryFn: () => get<Live>("/api/live"), ...liveQueryOptions(visible) });
  const data = live.data;
  const updated = data ? Math.max(0, Math.round(now - data.generated_at)) : null;
  return (
    <>
      <PageHead title="Live" intro={<>Who is doing what, where, with which model and on which account, right now.{" "}
        {updated != null && <span className="quiet">Updated {updated}s ago{visible ? "" : " (paused while hidden)"}.</span>}</>} />
      <LoadError error={live.error} />
      {!data && live.isLoading && <p className="quiet">Loading…</p>}
      {data && (
        <div className="live">
          <PresenceSection data={data} now={now} />
          <AccountsSection data={data} />
          <WorkersSection data={data} now={now} phone={phone} />
          <ChainsSection data={data} />
          <QueueSection data={data} phone={phone} />
          <MachinesSection data={data} phone={phone} />
          <SelfReportedSection data={data} now={now} />
        </div>
      )}
    </>
  );
}

function PresenceSection({ data, now }: { data: Live; now: number }) {
  return (
    <Section title="Orchestrator" aside={<Link to="/activity">all activity</Link>}>
      {data.orchestrator.length === 0 && (
        <p className="empty panel">No orchestrator session seen yet. It shows here once its MCP bridge sends its session headers
          (FLEET_SESSION_ID, FLEET_SESSION_MODEL, FLEET_SESSION_ACCOUNT).</p>
      )}
      {data.orchestrator.map((s) => (
        <p key={s.session} className={`live-presence ${s.stale ? "tone-quiet" : ""}`} title={`session ${s.session}`}>
          {presenceText(s, now)}{s.stale && !s.stale_since && " (stale)"}
        </p>
      ))}
      {data.alerts.length > 0 && (
        <ul className="live-alerts" aria-label="Recent alerts">
          {data.alerts.map((a) => (
            <li key={a.id}>
              <span className="quiet small">{ago(a.ts, now)}</span>{" "}
              {a.task_id ? <Link to={`/tasks?task=${a.task_id}`}>{alertText(a)}</Link>
                : a.node ? <Link to={`/nodes/${encodeURIComponent(a.node)}`}>{alertText(a)}</Link> : alertText(a)}
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function AccountsSection({ data }: { data: Live }) {
  return (
    <Section title="Accounts" aside={<Link to="/budget">Budget (limits, owner reset)</Link>}>
      <p className="small quiet">Facts only: the 5-hour window start isn't estimated, because use outside the fleet moves it.</p>
      {data.accounts.length === 0 && <p className="empty panel">No paid accounts yet. Runners list their signed-in profiles in runner.json.</p>}
      <div className="live-rows">
        {data.accounts.map((a) => (
          <div key={a.account} className="live-row" title={a.account}>
            <b>{a.agent} · {a.label ?? a.account}</b>
            <span className={`state tone-${accountTone(a)}`}>{a.status}</span>
            <span className="small">running {a.running}</span>
            <span className="small quiet">
              since {a.last_limit ? `limit at ${clockTime(a.last_limit.at)}` : "5 h ago"}: in {a.tokens_since.input ?? 0} · out {a.tokens_since.output ?? 0}
              {" "}· cached {a.tokens_since.cached ?? 0}
              {(a.self_reported_tokens.input + a.self_reported_tokens.output) > 0 &&
                ` · self-reported in ${a.self_reported_tokens.input} · out ${a.self_reported_tokens.output}`}
            </span>
          </div>
        ))}
      </div>
    </Section>
  );
}

function WorkersSection({ data, now, phone }: { data: Live; now: number; phone: boolean }) {
  const running = data.workers.filter((w) => w.task_id);
  const idle = data.workers.filter((w) => !w.task_id);
  return (
    <Section title="Workers" aside={`${running.length} running · ${idle.length} idle`}>
      {data.workers.length === 0 && <p className="empty panel">No workers: no approved node has a runner yet. Add one on Nodes.</p>}
      <div className={rowsClass(phone)}>
        {running.map((w) => {
          const text = workerText(w, now);
          const elapsed = w.started_at ? duration(now - w.started_at) : "—";
          return (
            <Link key={w.task_id} to={`/tasks?task=${w.task_id}`} className="live-row live-running"
                  title={[w.title, w.branch, w.node, w.waiver_reason && `waiver: ${w.waiver_reason}`].filter(Boolean).join("\n")}>
              <b className="live-title">{text.primary}</b>
              <span className="small">{text.secondary}</span>
              <span className="small mono">{text.meta}</span>
              <span className="small mono quiet live-branch">{w.branch ?? ""}</span>
              <span className="small">{elapsed}</span>
              {text.flag && <span className="state tone-warn small">{text.flag}</span>}
            </Link>
          );
        })}
        {idle.map((w) => {
          const text = workerText(w, now);
          return (
            <div key={`${w.node}/${w.agent}/${w.profile}`} className="live-row live-idle">
              <span>{text.primary}</span>
              <span className="small quiet">{w.node ? <Link to={`/nodes/${encodeURIComponent(w.node)}`}>{text.secondary}</Link> : text.secondary}</span>
              <span className={`state small tone-${text.tone}`}>{text.meta}</span>
            </div>
          );
        })}
      </div>
    </Section>
  );
}

function ChainsSection({ data }: { data: Live }) {
  return (
    <Section title="Chains" aside={<Link to="/pipelines">Pipelines</Link>}>
      {data.chains.length === 0 && <p className="empty panel">No pipelines or work items in the last 24 hours.</p>}
      <div className="live-rows">
        {data.chains.map((c) => (
          <div key={c.pipeline_id ?? c.root_id} className="live-row live-chain">
            {c.pipeline_id ? <Link to="/pipelines">{c.title}</Link>
              : <Link to={`/tasks?task=${c.root_id}`}>{c.title}</Link>}
            <ol className="stages" aria-label={`Stages of ${c.title}`}>
              {c.stages.map((s, i) => {
                const chip = stageChip(s);
                const body = <>{chip.label}</>;
                return (
                  <li key={`${s.stage}-${i}`} className={chip.className} aria-current={s.state === "current" ? "step" : undefined}
                      title={stageTitle(s)}>
                    {s.task_id && (s.display ? s.display.tone === "attention" : s.state === "failed") ?<Link to={`/tasks?task=${s.task_id}`}>{body}</Link> : body}
                  </li>
                );
              })}
            </ol>
          </div>
        ))}
      </div>
    </Section>
  );
}

function QueueSection({ data, phone }: { data: Live; phone: boolean }) {
  return (
    <Section title="Queue" aside={`${data.queue.length} waiting`}>
      {data.queue.length === 0 && <p className="empty panel">Nothing waiting: every task has started or settled.</p>}
      <div className={rowsClass(phone)}>
        {data.queue.map((q) => (
          <Link key={q.task_id} to={`/tasks?task=${q.task_id}`} className="live-row">
            <b className="live-title">{q.title}</b>
            <span className="small">{q.agent}{q.model ? ` · ${q.model}` : ""}{q.account_label ? ` · ${q.account_label}` : ""}</span>
            <span className={`state small ${q.wait === "approval" ? "tone-warn" : "tone-quiet"}`}>{queueText(q)}</span>
            <span className="small quiet">{duration(q.queued_s)}</span>
          </Link>
        ))}
      </div>
    </Section>
  );
}

function Meter({ label, used, total, unit }: { label: string; used: number | null; total: number | null; unit: string }) {
  const f = fill(used, total);
  const tone = f == null ? "quiet" : f > 0.9 ? "bad" : f > 0.75 ? "warn" : "ok";
  return (
    <span className="live-meter">
      <span className="small">{label} {used == null ? "unknown" : `${used}${total ? ` / ${total}` : ""} ${unit}`}</span>
      <span className="meter" role="meter" aria-label={label} aria-valuenow={used ?? undefined} aria-valuemax={total ?? undefined}>
        <span className={`fill tone-bg-${tone}`} style={{ width: `${Math.round((f ?? 0) * 100)}%` }} />
      </span>
    </span>
  );
}

function MachineRow({ m }: { m: Machine }) {
  const warnings = machineWarnings(m);
  return (
    <div className="live-row live-machine">
      <Link to={`/nodes/${encodeURIComponent(m.node)}`}><b>{m.name ?? m.node}</b></Link>
      {!m.unknown && !m.offline ? (
        <>
          <Meter label="Memory" used={m.mem_used_gb} total={m.mem_total_gb} unit="GB" />
          <span className="small">Swap {m.swap_used_gb ?? "unknown"} GB · disk free {m.disk_free_gb ?? "unknown"} GB · load {m.load5 ?? "?"}/{m.cores ?? "?"}</span>
        </>
      ) : null}
      <span className="small">
        Ollama {m.ollama_up == null ? "unknown" : m.ollama_up ? "up" : "down"}
        {m.loaded.length > 0 && <> · loaded {m.loaded.map((x) => <span key={x} className="mono">{x}{m.warm.includes(x) ? " (warm)" : ""} </span>)}</>}
      </span>
      <span className="row" style={{ gap: 8 }}>
        {warnings.map((w) => <span key={w.label} className={`state small tone-${w.tone}`}>{w.label}</span>)}
        <span className="small quiet">{m.age_s == null ? "" : `data ${m.age_s}s old`}</span>
      </span>
    </div>
  );
}

function MachinesSection({ data, phone }: { data: Live; phone: boolean }) {
  return (
    <Section title="Machines and local models" aside={<Link to="/nodes">Nodes</Link>}>
      {data.machines.length === 0 && <p className="empty panel">No machines report metrics yet. Runners collect them every 15 s once updated.</p>}
      <div className={rowsClass(phone)}>
        {data.machines.map((m) => <MachineRow key={m.node} m={m} />)}
      </div>
    </Section>
  );
}

function SelfReportedSection({ data, now }: { data: Live; now: number }) {
  return (
    <Section title="Self-reported activity" aside="reported by the orchestrator; never enforced">
      {data.self_reported.length === 0 && (
        <p className="empty panel">Nothing self-reported in the last 24 hours. Work that can't run as a fleet task appears here
          when the orchestrator reports it with report_activity.</p>
      )}
      <div className="live-rows">
        {data.self_reported.map((a) => (
          <div key={a.id} className="live-row live-self" title={a.note ?? undefined}>
            <span className="state small tone-info">self-reported</span>
            <b className="live-title">{a.title}</b>
            <span className="small">{a.kind} · {a.account_label}{a.model ? ` · ${a.model}` : ""}</span>
            <span className={`state small ${a.status === "failed" ? "tone-bad" : a.status === "done" ? "tone-ok" : "tone-info"}`}>{a.status}</span>
            <span className="small quiet">{ago(a.ended_at ?? a.ts, now)}</span>
            {a.work_item && <Link className="small" to={`/tasks?task=${a.work_item}`}>{a.work_item}</Link>}
          </div>
        ))}
      </div>
    </Section>
  );
}
