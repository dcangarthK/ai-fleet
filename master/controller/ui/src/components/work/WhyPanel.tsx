import { whyAttempt, type DetailAttempt } from "../../lib/work";

/** "Why?": for each attempt, the model, account, kind and standing budget rule it ran under. */
export function WhyPanel({ attempts }: { attempts: DetailAttempt[] }) {
  if (attempts.length === 0) return <p className="muted">No attempts to explain yet.</p>;
  return (
    <ul style={{ margin: 0, paddingLeft: 18 }}>
      {attempts.map((a) => {
        const w = whyAttempt(a);
        return (
          <li key={a.id} style={{ overflowWrap: "anywhere" }}>
            <span className="mono">{a.id}</span>: {w.kind} on <b>{w.model}</b>, account {w.account}. <span className="muted">{w.rule}</span>
          </li>
        );
      })}
    </ul>
  );
}
