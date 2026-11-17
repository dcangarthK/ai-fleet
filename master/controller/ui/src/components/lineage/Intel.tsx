import { useMemo } from "react";
import { fmtClock, fmtDur, type Anomaly, type Bottleneck, type Cite, type LNode, type Narrative } from "../../lib/lineageGraph";
import { InsightCard, type Evidence } from "../ui";

/** Evidence links: task id and timestamp, each opening that attempt in its trace. */
function evidenceFor(cites: Cite[], rootOf: Map<string, string>): Evidence[] {
  const seen = new Set<string>();
  const out: Evidence[] = [];
  for (const c of cites) {
    const key = `${c.id}@${Math.round(c.ts)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const root = rootOf.get(c.id);
    out.push({ label: `${c.id} · ${fmtClock(c.ts, false, true)}`, to: root ? `/lineage/${root}?sel=${c.id}` : undefined });
  }
  return out.slice(0, 8);
}

const ANOMALY_TONE: Record<Anomaly["kind"], "warn" | "bad"> = { retries: "warn", gate_rounds: "bad", stall: "bad", cost: "warn", limit: "warn" };
const ANOMALY_LABEL: Record<Anomaly["kind"], string> = { retries: "Anomaly · retries", gate_rounds: "Anomaly · gate rounds", stall: "Anomaly · stall", cost: "Anomaly · cost", limit: "Anomaly · usage limit" };

/** Bottleneck callout, anomaly markers and the narrative. Advice only: nothing here runs anything. */
export function Intel({ nodes, bottleneck, anomalies, narrative }: {
  nodes: LNode[]; bottleneck: Bottleneck | null; anomalies: Anomaly[]; narrative?: Narrative | null;
}) {
  const rootOf = useMemo(() => new Map(nodes.map((n) => [n.id, n.root_id])), [nodes]);
  const first = anomalies.slice(0, 3);
  const rest = anomalies.slice(3);
  if (!bottleneck && !anomalies.length && !narrative) return null;
  return (
    <section className="lin-intel" aria-label="Intelligence">
      <div className="lin-intel-grid">
        {narrative && (
          <InsightCard
            kind="What happened" tone="ok" confidence={0.95}
            title={<span className="lin-narrative">{narrative.text}</span>}
            why="Written from the recorded events and attempts. No model was used."
            evidence={evidenceFor(narrative.evidence, rootOf)}
          />
        )}
        {bottleneck && (
          <InsightCard
            kind={bottleneck.kind === "queue" ? "Bottleneck · queue wait" : "Bottleneck · slowest stage"} tone="warn"
            title={`${fmtDur(bottleneck.seconds)} ${bottleneck.kind === "queue" ? "waiting in the queue" : "in one step"}`}
            why={bottleneck.why} confidence={bottleneck.kind === "queue" ? 0.8 : 0.7}
            evidence={evidenceFor(bottleneck.evidence, rootOf)}
          />
        )}
        {first.map((a, i) => (
          <InsightCard key={`${a.kind}-${a.ids[0]}-${i}`} kind={ANOMALY_LABEL[a.kind]} tone={ANOMALY_TONE[a.kind]} title={a.title} why={a.why} confidence={0.9} evidence={evidenceFor(a.evidence, rootOf)} />
        ))}
      </div>
      {rest.length > 0 && (
        <details className="lin-more">
          <summary>{rest.length} more anomal{rest.length === 1 ? "y" : "ies"}</summary>
          <div className="lin-intel-grid">
            {rest.map((a, i) => (
              <InsightCard key={`${a.kind}-${a.ids[0]}-${i}`} kind={ANOMALY_LABEL[a.kind]} tone={ANOMALY_TONE[a.kind]} title={a.title} why={a.why} confidence={0.9} evidence={evidenceFor(a.evidence, rootOf)} />
            ))}
          </div>
        </details>
      )}
    </section>
  );
}
