"""batch_report: per-task model, account, commit, gate, verdict, tokens/spend and limit hits, composed by one SQL
query (docs/research/FLEET-IMPROVEMENT-PLAN-2026-11-19.md 2.3: the "record per task" and spend report)."""
import json
import re

from store import db

VERDICT_RE = re.compile(r"^\W*verdict\b.*$", re.M | re.I)

SQL = """
SELECT t.id, t.title, t.kind, t.state, t.agent, t.model, t.account_label, t.branch, t.outcome, t.resolution,
       t.gate, t.gate_result, t.result,
       COUNT(u.id) AS usage_rows, COALESCE(SUM(u.input), 0) AS tokens_in, COALESCE(SUM(u.output), 0) AS tokens_out,
       COALESCE(SUM(u.cached), 0) AS tokens_cached, COALESCE(SUM(u.cost_usd), 0) AS spend_usd,
       COALESCE(SUM(u.outcome = 'limit'), 0) AS limit_hits,
       GROUP_CONCAT(DISTINCT u.account) AS accounts, GROUP_CONCAT(DISTINCT u.cost_source) AS cost_sources
FROM tasks t LEFT JOIN agent_usage u ON u.task_id = t.id
WHERE t.id IN ({marks})
GROUP BY t.id
"""


def _json(text):
    try:
        return json.loads(text) if text else None
    except (TypeError, ValueError):
        return None


def _commit(row) -> str | None:
    for field in ("outcome", "resolution"):
        value = _json(row[field])
        if isinstance(value, dict) and value.get("commit"):
            return value["commit"]
    return None


def report(ids: list[str]) -> list[dict]:
    """One row per requested id, in the order asked; an unknown id comes back as {"id", "error": "not found"}."""
    if not ids:
        return []
    with db() as c:
        rows = {r["id"]: r for r in c.execute(SQL.format(marks=",".join("?" * len(ids))), ids)}
    out = []
    for task_id in ids:
        r = rows.get(task_id)
        if r is None:
            out.append({"id": task_id, "error": "not found"})
            continue
        gate = _json(r["gate_result"])
        state = gate.get("state") if isinstance(gate, dict) else None
        verdict = VERDICT_RE.search(r["result"] or "")
        out.append({
            "id": r["id"], "title": r["title"], "kind": r["kind"], "state": r["state"], "agent": r["agent"],
            "model": r["model"], "account": r["accounts"] or r["account_label"],
            "commit": _commit(r), "branch": r["branch"],
            "gate": state or ("no_gate" if not r["gate"] else "not_run"),
            "verdict": verdict.group(0).strip() if verdict else "",
            "tokens": {"input": r["tokens_in"], "output": r["tokens_out"], "cached": r["tokens_cached"]},
            "spend_usd": round(r["spend_usd"], 4),
            "cost_sources": r["cost_sources"].split(",") if r["cost_sources"] else [],
            "runs": r["usage_rows"], "limit_hits": r["limit_hits"],
        })
    return out
