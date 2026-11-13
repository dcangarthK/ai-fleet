"""Durable, short-session controller leadership (Part G requirement 11)."""
import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import budget
import handoff
import tasks
from events import emit
from store import db

router = APIRouter()
MAX_PROMPT_BYTES = 4 * 1024
ACTIVE_STATUSES = {"open"}
STATUSES = ACTIVE_STATUSES | {"blocked", "pending_review", "review_queued", "done"}
_lock = asyncio.Lock()


class PlanItemIn(BaseModel):
    work_item: str
    next_steps: list[str] = Field(default_factory=list)
    status: str = "open"
    revision: int | None = None


class PlanIn(BaseModel):
    items: list[PlanItemIn] = Field(default_factory=list)


class PlanItemUpdateIn(BaseModel):
    next_steps: list[str] = Field(default_factory=list)
    status: str
    revision: int


def _check_item(item: PlanItemIn | PlanItemUpdateIn, work_item: str) -> None:
    if not work_item or len(work_item) > 128:
        raise HTTPException(400, "each plan item needs a work_item")
    if item.status not in STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(STATUSES)}")
    if len(item.next_steps) > 50 or any(not isinstance(step, str) or len(step) > 1000 for step in item.next_steps):
        raise HTTPException(400, "next_steps must contain at most 50 short strings")


def _revision(c) -> int:
    row = c.execute("SELECT revision FROM plan_state WHERE id=1").fetchone()
    return row["revision"] if row else 0


def _bump(c) -> int:
    c.execute("INSERT OR IGNORE INTO plan_state (id,revision) VALUES (1,0)")
    c.execute("UPDATE plan_state SET revision=revision+1 WHERE id=1")
    return _revision(c)


def _items() -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT work_item,next_steps,status,updated_at,revision FROM plan_items ORDER BY rowid").fetchall()
    out = []
    for row in rows:
        try:
            steps = json.loads(row["next_steps"])
        except (TypeError, ValueError):
            steps = []
        out.append({"work_item": row["work_item"], "next_steps": steps if isinstance(steps, list) else [],
                    "status": row["status"], "updated_at": row["updated_at"], "revision": row["revision"]})
    return out


def record() -> dict | None:
    with db() as c:
        row = c.execute("SELECT account,model,since,session_task_id,not_before,plan_revision,halted FROM leader WHERE id=1").fetchone()
    return dict(row) if row else None


def plan_view() -> dict:
    with db() as c:
        revision = _revision(c)
    return {"revision": revision, "items": _items()}


@router.get("/api/plan")
def get_plan(_: Request) -> dict:
    return plan_view()


@router.put("/api/plan")
def put_plan(body: PlanIn, _: Request) -> dict:
    """Upsert supplied items. Existing items need their revision; this never deletes unseen work."""
    if len(body.items) > 100:
        raise HTTPException(400, "at most 100 plan items")
    seen = set()
    with db() as c:
        for item in body.items:
            work_item = item.work_item.strip()
            if work_item in seen:
                raise HTTPException(400, "each plan item needs a unique work_item")
            _check_item(item, work_item)
            seen.add(work_item)
            old = c.execute("SELECT revision FROM plan_items WHERE work_item=?", (work_item,)).fetchone()
            if old and item.revision != old["revision"]:
                raise HTTPException(409, "plan item changed; GET /api/plan and retry with its revision")
            revision = (old["revision"] + 1) if old else 1
            c.execute("INSERT INTO plan_items (work_item,next_steps,status,updated_at,revision) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(work_item) DO UPDATE SET next_steps=excluded.next_steps,status=excluded.status,"
                      "updated_at=excluded.updated_at,revision=excluded.revision",
                      (work_item, json.dumps(item.next_steps), item.status, time.time(), revision))
        if body.items:
            _bump(c)
    return plan_view()


@router.put("/api/plan/{work_item}")
def update_plan_item(work_item: str, body: PlanItemUpdateIn, _: Request) -> dict:
    work_item = work_item.strip()
    _check_item(body, work_item)
    with db() as c:
        changed = c.execute("UPDATE plan_items SET next_steps=?,status=?,updated_at=?,revision=revision+1 "
                            "WHERE work_item=? AND revision=?",
                            (json.dumps(body.next_steps), body.status, time.time(), work_item, body.revision)).rowcount
        if changed != 1:
            raise HTTPException(409, "plan item changed; GET /api/plan and retry with its revision")
        _bump(c)
    return plan_view()


@router.get("/api/leader")
def get_leader() -> dict:
    return record() or {"account": None, "model": None, "since": None, "session_task_id": None,
                        "not_before": None, "plan_revision": 0, "halted": False}


def _next_item(items: list[dict]) -> dict | None:
    return next((item for item in items if item["status"] in ACTIVE_STATUSES and item["next_steps"]), None)


def _lead_slots(now: float) -> list[dict]:
    rows = handoff.resources(now=now)
    candidates = [row for row in rows if row["state"] == "free" and row.get("concrete_model")
                  and "lead" in row.get("allowed_kinds", []) and budget.lead_allowed(row["agent"], row["concrete_model"])]
    def waits(row: dict) -> bool:  # a near-limit account would only queue the lead (budget.start_wait, as placement)
        probe = {"id": "", "created_at": now, "agent": row["agent"], "model": row["concrete_model"], "kind": "lead", "budget_key": row.get("model"),
                 "budget_identity": budget.identity(row["agent"], row["concrete_model"]), "account": row["account"]}
        return budget.start_wait(probe, now)[0] == "limit"
    ready = [row for row in candidates if not waits(row)]
    candidates = ready or candidates  # when every slot would wait, the lead queues on the best one
    return sorted(candidates, key=lambda row: (0 if row["agent"] == "codex" else 1 if "opus" in row["concrete_model"].lower() else 2,
                                                row["account"] or ""))


def _earliest_reset(now: float) -> float | None:
    ends = []
    for row in handoff.resources(now=now):
        if (budget.lead_allowed(row.get("agent"), row.get("concrete_model")) and
                row.get("state") in ("limited", "at_budget") and row.get("until") and row["until"] > now):
            ends.append(row["until"])
    return min(ends) if ends else None


def _brief(work_item: str) -> str:
    """Only the selected work item's bounded state; unrelated task results never enter a lead prompt."""
    with db() as c:
        rows = c.execute("SELECT id,title,state,result,error FROM tasks WHERE id=? OR parent_id=? "
                         "ORDER BY updated_at DESC LIMIT 8", (work_item, work_item)).fetchall()
    return "\n".join(f"{row['id']} {row['state']}: {(row['title'] or '')} "
                     f"{' '.join((row['result'] or row['error'] or '').split())[:240]}".strip() for row in rows)


def _lead_location(work_item: str) -> tuple[str | None, bool]:
    """A lead may edit only inside the repo already attached to its plan work item."""
    try:
        source = tasks.get_task(work_item)
    except HTTPException:
        return None, False
    repo = source.get("repo")
    return (repo, bool(repo)) if isinstance(repo, str) and repo else (None, False)


def prompt(item: dict, plan_revision: int) -> str:
    """One item and one next step fit below 4 KB without truncating JSON or omitting other plan records."""
    body = ("You are the controller lead for one capped session. Execute this one stored step, then update only this "
            "item with PUT /api/plan/{work_item} and its revision. Do not approve, unblock, push, or set budget "
            "overrides. If Codex builds work, add/update that build task's plan item as pending_review.\n\n"
            + json.dumps({"plan_revision": plan_revision, "work_item": item["work_item"], "revision": item["revision"],
                          "status": item["status"], "next_step": item["next_steps"][0]}, separators=(",", ":"))
            + "\n\nState brief:\n" + _brief(item["work_item"]))
    return body.encode()[:MAX_PROMPT_BYTES].decode("utf-8", "ignore")


def _mark_review_queued(item: dict) -> None:
    with db() as c:
        changed = c.execute("UPDATE plan_items SET status='review_queued',updated_at=?,revision=revision+1 "
                            "WHERE work_item=? AND revision=? AND status='pending_review'",
                            (time.time(), item["work_item"], item["revision"])).rowcount
        if changed:
            _bump(c)


def _unresolved_review(work_item: str) -> bool:
    """A pending-review item owns at most one non-terminal linked review task."""
    with db() as c:
        row = c.execute("SELECT 1 FROM tasks WHERE review_of=? AND state NOT IN ('done','cancelled','failed') LIMIT 1",
                        (work_item,)).fetchone()
    return row is not None


async def _queue_pending_reviews(now: float) -> None:
    for item in _items():
        if item["status"] != "pending_review":
            continue
        try:
            source = tasks.get_task(item["work_item"])
            if source["state"] in tasks.ACTIVE:
                continue
            # A prior successful create can race with the plan CAS below.  Reuse it instead of
            # issuing another paid review when the item is still pending on the next tick.
            if _unresolved_review(source["id"]):
                _mark_review_queued(item)
                continue
            slot = next((row for row in handoff.resources(now=now) if row["state"] == "free" and row["agent"] == "claude"
                         and row["node"] == source["node_id"] and "review" in row.get("allowed_kinds", [])
                         and budget.lead_allowed("claude", row.get("concrete_model"))), None)
            if not slot:
                continue
            await tasks.create_task(tasks.TaskIn(agent="claude", model=slot["concrete_model"], profile=slot["profile"],
                                                  kind="review", mode="read-only", worktree=False, review_of=source["id"],
                                                  parent_id=source["id"], link="review", title="Claude review for Codex build",
                                                  prompt="Review the completed Codex work item for correctness and regressions."), scope="operate")
            _mark_review_queued(item)
        except Exception as exc:
            # One unavailable account or budget refusal must not stop leader continuity for other work.
            print(f"leader review queue skipped {item['work_item']}: {exc!r}")
            emit(item["work_item"], "leader.review_skipped", {"work_item": item["work_item"], "reason": str(exc)[:300]})


async def _cancel_old(old_task: dict) -> bool:
    if old_task["state"] not in tasks.ACTIVE:
        return True
    try:
        await tasks.cancel(old_task["id"])
    except HTTPException:
        return False
    return tasks.get_task(old_task["id"])["state"] not in tasks.ACTIVE


async def tick(now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    async with _lock:
        await _queue_pending_reviews(now)
        plan = plan_view()
        item = _next_item(plan["items"])
        if not item:  # blocked and pending-review items must not keep paid leaders alive
            return None
        old = record()
        old_task = None
        if old and old.get("session_task_id"):
            try:
                old_task = tasks.get_task(old["session_task_id"])
            except HTTPException:
                pass
        old_limited = bool(old and any(row.get("account") == old.get("account") and row.get("concrete_model") == old.get("model")
                                       and row.get("state") in ("limited", "at_budget") for row in handoff.resources(now=now)))
        if old_task and old_task["state"] == "queued" and not old_limited:
            return {"waiting": old_task.get("budget_wait") or old_task.get("error") or "lead task queued"}
        if old and old.get("halted") and old.get("plan_revision") == plan["revision"]:
            return {"halted": True}
        if old_task and old_task["state"] in tasks.ACTIVE and not old_limited:
            return None
        is_limit = bool(old_limited or (old_task and old_task.get("failure_reason") == "limit"))
        if old_task and not is_limit and old_task["state"] not in tasks.ACTIVE and old.get("plan_revision") == plan["revision"]:
            with db() as c:
                c.execute("UPDATE leader SET halted=1 WHERE id=1")
            emit(old_task["id"], "leader.escalated", {"reason": "lead settled without plan progress"})
            return {"halted": True}
        if old_task and old_task["state"] in tasks.ACTIVE and old_limited and not await _cancel_old(old_task):
            return {"waiting": "old leader could not be cancelled"}
        slots = _lead_slots(now)
        if not slots:
            reset = _earliest_reset(now)
            with db() as c:
                c.execute("INSERT INTO leader (id,not_before,plan_revision,halted) VALUES (1,?,?,0) "
                          "ON CONFLICT(id) DO UPDATE SET not_before=excluded.not_before", (reset, plan["revision"]))
            return {"waiting_until": reset}
        slot = slots[0]
        replace = old_task["id"] if old_task and is_limit else None
        repo, worktree = _lead_location(item["work_item"])
        task = await tasks.create_task(tasks.TaskIn(agent=slot["agent"], model=slot["concrete_model"], profile=slot["profile"],
                                                     node=slot["node"], kind="lead", effort="medium", mode="auto-edit",
                                                     repo=repo, worktree=worktree, title="Controller lead", prompt=prompt(item, plan["revision"]),
                                                     replaces=replace), scope="operate")
        current = {"account": task.get("account"), "model": task.get("model"), "since": now,
                   "session_task_id": task["id"], "not_before": None, "plan_revision": plan["revision"], "halted": False}
        with db() as c:
            c.execute("INSERT INTO leader (id,account,model,since,session_task_id,not_before,plan_revision,halted) "
                      "VALUES (1,?,?,?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET account=excluded.account,model=excluded.model,"
                      "since=excluded.since,session_task_id=excluded.session_task_id,not_before=NULL,"
                      "plan_revision=excluded.plan_revision,halted=0",
                      (current["account"], current["model"], now, current["session_task_id"], None, plan["revision"]))
        reason = "plan_started" if not old else "account_limited" if is_limit else ("capacity_available"
                 if not old.get("session_task_id") else "plan_progressed")
        emit(task["id"], "leader.assigned", {"from": old if old and old.get("session_task_id") else None,
                                                "to": current, "reason": reason})
        return current


async def loop() -> None:
    while True:
        try:
            await tick()
        except Exception as exc:
            print(f"leader tick failed: {exc!r}")
        await asyncio.sleep(3)
