import { Card, Empty, StatusPill } from "../ui";
import { clockTime } from "../../lib/budget";
import { ago } from "../../lib/format";
import { leaderLag, leaderState, type LeaderRecord, type PlanView } from "./model";
import { Link } from "react-router-dom";

/** Leader state from GET /api/leader: who leads, since when, whether it is halted or waiting on a limit. */
export function LeaderCard({ leader, plan, now }: { leader: LeaderRecord | undefined; plan: PlanView | undefined; now: number }) {
  const st = leaderState(leader, now);
  const lag = leaderLag(leader, plan);
  const none = !leader || (!leader.account && !leader.model && !leader.session_task_id);
  return (
    <Card title="Leader" actions={<StatusPill tone={st.tone}>{st.label}</StatusPill>}>
      {none ? (
        <Empty title="No leader session yet">The controller starts a short leader session when an open plan item has a next step.</Empty>
      ) : (
        <dl className="kv">
          <dt>Model</dt><dd className="mono">{leader!.model ?? "—"}</dd>
          <dt>Account</dt><dd className="mono">{leader!.account ?? "—"}</dd>
          <dt>Session</dt>
          <dd className="mono">{leader!.session_task_id ? <Link to={`/work/${encodeURIComponent(leader!.session_task_id)}`}>{leader!.session_task_id}</Link> : "—"}</dd>
          <dt>Since</dt><dd>{leader!.since ? ago(leader!.since) : "—"}</dd>
          {leader!.not_before && leader!.not_before > now && <><dt>Not before</dt><dd>{clockTime(leader!.not_before)}</dd></>}
          <dt>Plan seen</dt>
          <dd>r{leader!.plan_revision}{lag > 0 ? <span className="quiet"> ({lag} revision{lag === 1 ? "" : "s"} behind)</span> : null}</dd>
        </dl>
      )}
      {leader?.halted && <p className="banner error" role="alert">The controller records this leader as halted.</p>}
    </Card>
  );
}
