"""Durable controller leadership (Part G requirement 11).

The plan belongs to the controller, not to one chat session.  A leader only receives one
short, capped task at a time; when it settles or its account is limited this module starts
the next session on the first capable account.
"""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import handoff
import tasks
from events import emit
from store import db

router = APIRouter()
MAX_PROMPT_BYTES = 4 * 1024
OPEN_STATUSES = {"open", "blocked", "pending_review"}
STATUSES = OPEN_STATUSES | {"done"}
_lock = asyncio.Lock()


class PlanItemIn(BaseModel):
    work_item: str
    next_steps: list[str] = Field(default_factory=list)
    status: str = "open"


class PlanIn(BaseModel):
    items: list[PlanItemIn] = Field(default_factory=list)


def _items() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT work_item,next_steps,status,updated_at FROM plan_items ORDER BY rowid").fetchall()
    out = []
    for row in rows:
        try:
            steps = json.loads(row["next_steps"])
        except (TypeError, ValueError):
            steps = []
        out.append({"work_item": row["work_item"], "next_steps": steps if isinstance(steps, list) else [],
                    "status": row["status"], "updated_at": row["updated_at"]})
    return out


def record() -> dict | None:
    with db() as c:
        row = c.execute("SELECT account,model,since,session_task_id,not_before FROM leader WHERE id=1").fetchone()
    return dict(row) if row else None


def plan_view() -> dict:
    return {"items": _items()}


@router.get("/api/plan")
def get_plan(_: Request) -> dict:
    """The owner and operate-scoped orchestrators share the durable next-step plan."""
    return plan_view()


@router.put("/api/plan")
def put_plan(body: PlanIn, _: Request) -> dict:
    if len(body.items) > 100:
        raise HTTPException(400, "at most 100 plan items")
    seen = set()
    rows = []
    for item in body.items:
        work_item = item.work_item.strip()
        if not work_item or len(work_item) > 128 or work_item in seen:
            raise HTTPException(400, "each plan item needs a unique work_item")
        if item.status not in STATUSES:
            raise HTTPException(400, f"status must be one of {sorted(STATUSES)}")
        if len(item.next_steps) > 50 or any(not isinstance(step, str) or len(step) > 1000 for step in item.next_steps):
            raise HTTPException(400, "next_steps must contain at most 50 short strings")
        seen.add(work_item)
        rows.append((work_item, json.dumps(item.next_steps), item.status, time.time()))
    with db() as c:
        c.execute("DELETE FROM plan_items")
        c.executemany("INSERT INTO plan_items (work_item,next_steps,status,updated_at) VALUES (?,?,?,?)", rows)
    return plan_view()


@router.get("/api/leader")
def get_leader() -> dict:
    return record() or {"account": None, "model": None, "since": None, "session_task_id": None,
                        "not_before": None}


def _remaining(items: list[dict]) -> bool:
    return any(item["status"] in OPEN_STATUSES and item["next_steps"] for item in items)


def _lead_slots(now: float) -> list[dict]:
    """Only the owner-amended leader order; never Fable or a local model."""
    rows = handoff.resources(now=now)
    candidates = [row for row in rows if row["state"] == "free" and row.get("concrete_model")
                  and "lead" in row.get("allowed_kinds", [])]

    def order(row: dict) -> tuple:
        model = row["concrete_model"].lower()
        if row["agent"] == "codex" and model == "gpt-5.6-terra":
            return (0, 0, row["account"] or "")
        if row["agent"] == "claude" and "opus" in model:
            return (1, 0, row["account"] or "")
        if row["agent"] == "claude" and "sonnet" in model:
            return (2, 0, row["account"] or "")
        return (9, 0, row["account"] or "")

    return [row for row in sorted(candidates, key=order) if order(row)[0] < 9]


def _earliest_reset(now: float) -> float | None:
    ends = []
    for row in handoff.resources(now=now):
        model = (row.get("concrete_model") or "").lower()
        capable = ((row.get("agent") == "codex" and model == "gpt-5.6-terra") or
                   (row.get("agent") == "claude" and ("opus" in model or "sonnet" in model)))
        if capable and row.get("state") in ("limited", "at_budget") and row.get("until") and row["until"] > now:
            ends.append(row["until"])
    return min(ends) if ends else None


def _brief() -> str:
    """A deliberately small, transcript-free state brief for the fresh session."""
    with db() as c:
        rows = c.execute("SELECT id,title,state,result,error FROM tasks ORDER BY updated_at DESC LIMIT 8").fetchall()
    lines = []
    for row in rows:
        result = " ".join((row["result"] or row["error"] or "").split())[:240]
        lines.append(f"{row['id']} {row['state']}: {row['title'] or ''} {result}".strip())
    return "\n".join(lines)


def prompt(items: list[dict]) -> str:
    body = ("You are the controller lead for one capped session. Execute only the next stored plan step, "
            "then update the durable plan through PUT /api/plan before stopping. Do not approve, unblock, push, "
            "or set budget overrides: those actions remain owner-only. If you are Codex and build work, mark its "
            "plan item pending_review so a Claude review can be queued when an account returns.\n\n"
            "Stored plan:\n" + json.dumps(items, separators=(",", ":")) + "\n\nState brief:\n" + _brief())
    encoded = body.encode()
    if len(encoded) <= MAX_PROMPT_BYTES:
        return body
    return encoded[:MAX_PROMPT_BYTES].decode("utf-8", "ignore")


async def tick(now: float | None = None) -> dict | None:
    """Assign a fresh short lead session when an existing one is limited or settled."""
    now = time.time() if now is None else now
    async with _lock:
        items = _items()
        if not _remaining(items):
            return None
        old = record()
        if old and old.get("not_before") and old["not_before"] > now:
            return {"waiting_until": old["not_before"]}
        old_task = None
        if old and old.get("session_task_id"):
            try:
                old_task = tasks.get_task(old["session_task_id"])
            except HTTPException:
                pass
        old_limited = bool(old and any(row.get("account") == old.get("account") and
                                       row.get("concrete_model") == old.get("model") and
                                       row.get("state") in ("limited", "at_budget")
                                       for row in handoff.resources(now=now)))
        if old_task and old_task["state"] in tasks.ACTIVE and not old_limited:
            # The normal task lifecycle tells us about a live account limit; keep a still-running lead alone.
            return None
        reason = "plan_started" if not old else ("account_limited" if old_limited or (old_task and
                                                   old_task.get("failure_reason") == "limit") else "session_settled")
        slots = _lead_slots(now)
        if not slots:
            reset = _earliest_reset(now)
            if reset:
                with db() as c:
                    c.execute("INSERT INTO leader (id,not_before) VALUES (1,?) ON CONFLICT(id) DO UPDATE SET not_before=excluded.not_before",
                              (reset,))
            return {"waiting_until": reset}
        slot = slots[0]
        task = await tasks.create_task(tasks.TaskIn(
            agent=slot["agent"], model=slot["concrete_model"], profile=slot["profile"], node=slot["node"],
            kind="lead", effort="medium", mode="auto-edit", worktree=False, repo=None,
            title="Controller lead", prompt=prompt(items),
        ), scope="operate")
        current = {"account": slot["account"], "model": slot["concrete_model"], "since": now,
                   "session_task_id": task["id"], "not_before": None}
        with db() as c:
            c.execute("INSERT INTO leader (id,account,model,since,session_task_id,not_before) VALUES (1,?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET account=excluded.account,model=excluded.model,since=excluded.since,"
                      "session_task_id=excluded.session_task_id,not_before=NULL",
                      (current["account"], current["model"], now, current["session_task_id"], None))
        emit(task["id"], "leader.assigned", {"from": old, "to": current, "reason": reason})
        return current


async def loop() -> None:
    while True:
        try:
            await tick()
        except Exception as exc:  # a bad placement must not stop task polling or the controller
            print(f"leader tick failed: {exc!r}")
        await asyncio.sleep(3)
