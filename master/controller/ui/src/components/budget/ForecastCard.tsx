import { useMemo, useState } from "react";
import { Card, Empty, Field, Select } from "../ui";
import { clockTime, modelLabel, type BudgetStatus } from "../../lib/budget";
import { chartGeometry, forecastSentence, forecastTone, windowForecast, type Forecast } from "../../lib/forecast";

const W = 620, H = 230, L = 50, R = 590, T = 20, B = 180;
const px = (x: number) => L + x * (R - L);
const py = (y: number) => B - y * (B - T);

function dayTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString(undefined, { weekday: "short" })} ${clockTime(ts)}`;
}

function Chart({ f, now, title }: { f: Forecast; now: number; title: string }) {
  const g = chartGeometry(f, now);
  const desc = `Used ${Math.round(f.used)}% now. ${f.hitsAt != null ? `At the current burn it reaches 100% at ${dayTime(f.hitsAt)}.` : f.atReset != null ? `At the current burn it ends near ${Math.round(Math.min(f.atReset, 100))}% at reset.` : "Too early to project."} Resets ${dayTime(f.resetsAt)}.`;
  const tone = forecastTone(f);
  const projColour = tone === "bad" ? "var(--bad)" : tone === "warn" ? "var(--warn)" : "var(--accent)";
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby="fc-title fc-desc" style={{ width: "100%", height: "auto", maxWidth: 760, display: "block", fontSize: 11, fill: "var(--ink-3)" }}>
      <title id="fc-title">{title}</title>
      <desc id="fc-desc">{desc}</desc>
      {[0, 0.5, 1].map((y) => (
        <g key={y}>
          <line x1={L} x2={R} y1={py(y)} y2={py(y)} stroke="var(--line)" />
          <text x={L - 6} y={py(y) + 4} textAnchor="end">{Math.round(y * 100)}%</text>
        </g>
      ))}
      <line x1={L} x2={R} y1={B} y2={B} stroke="var(--line-strong, var(--ink-3))" />
      <line x1={L} x2={L} y1={T} y2={B} stroke="var(--line-strong, var(--ink-3))" />
      <line x1={L} x2={R} y1={py(1)} y2={py(1)} stroke="var(--bad)" strokeDasharray="4 3" />
      <text x={L} y={B + 16} textAnchor="start">{dayTime(f.windowStart)}</text>
      <text x={R} y={B + 16} textAnchor="end">{dayTime(f.resetsAt)}</text>
      <text x={(L + R) / 2} y={H - 4} textAnchor="middle">Window opens to reset (local time)</text>
      <polyline points={`${px(g.start.x)},${py(g.start.y)} ${px(g.now.x)},${py(g.now.y)}`} fill="none" stroke="var(--ink-2, var(--ink-3))" strokeWidth={2} />
      {g.end && (
        <polyline points={`${px(g.now.x)},${py(g.now.y)} ${px(g.end.x)},${py(g.end.y)}${g.hit && g.hit.x < 1 ? ` ${R},${py(1)}` : ""}`}
                  fill="none" stroke={projColour} strokeWidth={2} strokeDasharray="6 4" />
      )}
      <circle cx={px(g.now.x)} cy={py(g.now.y)} r={4} fill="var(--accent)" />
      <text x={px(g.now.x) + 8} y={py(g.now.y) - 8} style={{ fill: "var(--ink)" }}>Now {Math.round(f.used)}%</text>
      {g.hit && <circle cx={px(g.hit.x)} cy={py(g.hit.y)} r={4} fill="var(--bad)" />}
      {g.hit && <text x={Math.min(px(g.hit.x), R - 6)} y={py(1) - 6} textAnchor="end" style={{ fill: "var(--bad)" }}>100% {dayTime(f.hitsAt!)}</text>}
    </svg>
  );
}

/** Forecast for one account window: straight-line projection from the window's average burn (lib/forecast.ts). */
export function ForecastCard({ status, now }: { status?: BudgetStatus; now: number }) {
  const options = useMemo(() => {
    const out: { id: string; label: string; f: Forecast }[] = [];
    for (const m of status?.models ?? []) {
      for (const a of m.accounts ?? []) {
        for (const [name, w] of [["5-hour", a.rate_limits?.primary], ["Weekly", a.rate_limits?.secondary]] as const) {
          const f = windowForecast(w, now);
          if (f && w) out.push({ id: `${m.key}|${a.account}|${name}`, label: `${modelLabel(m.key)} · ${a.label ?? a.account} · ${name}`, f });
        }
      }
    }
    // The tightest windows first, so the default view is the one that matters.
    return out.sort((x, y) => (y.f.atReset ?? y.f.used) - (x.f.atReset ?? x.f.used));
  }, [status, now]);
  const [pick, setPick] = useState<string>("");
  const chosen = options.find((o) => o.id === pick) ?? options[0];

  return (
    <Card title="Rate-limit forecast">
      {!chosen ? (
        <Empty title="Nothing to forecast">Rate-limit windows appear once a Codex task reports them. Claude windows are not reported yet.</Empty>
      ) : (
        <div className="stack">
          <Field label="Window">
            {(p) => (
              <Select {...p} value={chosen.id} onChange={(e) => setPick(e.target.value)}>
                {options.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </Select>
            )}
          </Field>
          <Chart f={chosen.f} now={now} title={`${chosen.label} used over its window`} />
          <p data-testid="forecast-sentence">{forecastSentence(chosen.f, now, dayTime)}</p>
          <p className="small quiet">Straight-line projection from the average burn since the window opened. An estimate, not a guarantee; fleet tasks only.</p>
        </div>
      )}
    </Card>
  );
}
