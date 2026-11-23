"""Decision records (docs/research/FLEET-IMPROVEMENT-PLAN-2026-11-19.md 1.3): one row per placement, park or
handoff, so "why did it go there" is a query, not a code read. Observation only: a failure to write a row never
changes what the scheduler does."""
import json
import time

from fastapi import APIRouter

from store import db

router = APIRouter()


def _dump(value) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def record(task_id: str | None, kind: str, inputs: dict | None, candidates: list | None, rule: str,
           chosen, shadow_choice=None) -> int | None:
    """Write one row; returns its id, or None if the write failed."""
    try:
        with db() as c:
            return c.execute("INSERT INTO decisions (task_id, kind, inputs, candidates, rule, chosen, shadow_choice, ts)"
                             " VALUES (?,?,?,?,?,?,?,?)",
                             (task_id, kind, _dump(inputs), _dump(candidates), rule, _dump(chosen),
                              _dump(shadow_choice), time.time())).lastrowid
    except Exception as exc:  # observation must never stop placement
        print(f"decision record failed: {exc!r}")
        return None


def list_decisions(task_id: str | None = None, limit: int = 200) -> list[dict]:
    sql, args = "SELECT * FROM decisions", []
    if task_id:
        sql += " WHERE task_id=?"
        args.append(task_id)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 1000)))
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        for field in ("inputs", "candidates", "chosen", "shadow_choice"):
            d[field] = json.loads(d[field]) if d[field] else None
        out.append(d)
    return out


@router.get("/api/decisions")
def get_decisions(task_id: str | None = None, limit: int = 200) -> list[dict]:
    """View scope, read-only: newest first."""
    return list_decisions(task_id, limit)
