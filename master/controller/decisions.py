"""Decision records (docs/research/FLEET-IMPROVEMENT-PLAN-2026-11-19.md 1.3): one row per placement, park or
handoff, so "why did it go there" is a query, not a code read. Observation only: a failure to write a row never
changes what the scheduler does."""
import json
import time

from fastapi import APIRouter

from store import db

router = APIRouter()


RETENTION_DAYS = 14   # rows older than this are deleted...
RETENTION_ROWS = 50_000  # ...and only the newest this many are kept
PRUNE_EVERY = 200     # inserts between prunes
_inserts = 0
_LAST: dict = {}      # last chosen per key, so a repeated identical choice is not written again


def _dump(value) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


def changed(key, chosen) -> bool:
    """True when `chosen` differs from the last choice recorded for `key` (and remembers it)."""
    if _LAST.get(key) == chosen:
        return False
    if len(_LAST) > 5000:
        _LAST.clear()
    _LAST[key] = chosen
    return True


def prune(now: float | None = None, days: float = RETENTION_DAYS, keep: int = RETENTION_ROWS) -> int:
    """Delete rows older than `days`, then everything but the newest `keep`. Returns rows deleted."""
    now = time.time() if now is None else now
    with db() as c:
        n = c.execute("DELETE FROM decisions WHERE ts < ?", (now - days * 86400,)).rowcount
        n += c.execute("DELETE FROM decisions WHERE id <= (SELECT MAX(id) FROM decisions) - ?", (keep,)).rowcount
    return n


def record(task_id: str | None, kind: str, inputs: dict | None, candidates: list | None, rule: str,
           chosen, shadow_choice=None) -> int | None:
    """Write one row; returns its id, or None if the write failed."""
    global _inserts
    try:
        with db() as c:
            row = c.execute("INSERT INTO decisions (task_id, kind, inputs, candidates, rule, chosen, shadow_choice, ts)"
                            " VALUES (?,?,?,?,?,?,?,?)",
                            (task_id, kind, _dump(inputs), _dump(candidates), rule, _dump(chosen),
                             _dump(shadow_choice), time.time())).lastrowid
        _inserts += 1
        if _inserts % PRUNE_EVERY == 0:
            prune()
        return row
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
