"""Part G (docs/SPEC-capacity-placement.md) requirements 2-4 and 8: placement that follows live capacity.

- `tick()` runs from the task poll loop. In shadow mode (FLEET_CAPACITY_MODE unset or "shadow") it only records what
  it would decide as events (`capacity.shadow_limit`, `placement.shadow_move`, `task.requeue_shadow`). In live mode
  it acts: queued tasks waiting on a limit or a busy slot move to a free eligible slot (`placement.moved`), and a
  task that failed on a limit goes back to `queued` with `not_before` = the limit's reset (`task.requeued`).
- A move is one compare-and-set on the task row (state='queued' AND node/profile/account/key = old) under the task
  lock, so `recover()` and a second tick can never start or move it twice (Part G 3a). Moves stay on the task's own
  node (its policy, instructions and repo paths were resolved for it) and its own agent.
- Pins: an explicit `profile` pins the account, an explicit `model` pins the model key, `pin: true` pins both. A pinned
  field is never changed.
"""
import json
import time

from fastapi import HTTPException

import budget
import handoff
import tasks
import usage
from events import emit
from store import db

MAX_RESTART_FAILURES = 3  # runner refusals of a re-queued task's resume, per run, before it stops retrying
RESUME_TEXT = "The usage limit has reset. Continue the task where you stopped."
_SEEN: set = set()  # shadow decisions already recorded (one event per distinct decision)
_LIMIT_SEEN: dict[str, str] = {}


def _once(key) -> bool:
    if key in _SEEN:
        return False
    if len(_SEEN) > 5000:
        _SEEN.clear()
    _SEEN.add(key)
    return True


# ---------- requirement 2: limits follow live usage (shadow comparison) ----------

def shadow_limits(now: float) -> None:
    """For every account with live usage: the limit state now vs. what live mode would hold. One event per change."""
    rules = budget.current_rules()
    for account in budget.usage_records():
        legacy = budget.limit_snapshot(rules, now, account, live=False)
        live = budget.limit_snapshot(rules, now, account, live=True)
        if legacy == live:
            _LIMIT_SEEN.pop(account, None)
            continue
        text = json.dumps([legacy, live], sort_keys=True, default=str)
        if _LIMIT_SEEN.get(account) != text:
            _LIMIT_SEEN[account] = text
            emit(None, "capacity.shadow_limit", {"account": account, "now": legacy["accounts"] | {"models": legacy["models"]},
                                                 "live": live["accounts"] | {"models": live["models"]},
                                                 "reason": "live usage would change this account's limit"})


# ---------- requirement 3: queued work migrates ----------

def pins(task: dict) -> set[str]:
    placed = task.get("placement") or {}
    out = set()
    if placed.get("pin"):
        out |= {"profile", "model"}
    if placed.get("profile"):
        out.add("profile")
    if placed.get("model_pinned"):
        out.add("model")
    return out


def _view(task: dict) -> dict:
    return {"account": budget.task_account(task), "profile": task.get("profile") or "fleet",
            "key": task.get("budget_key"), "model": task.get("model")}


def choose_move(task: dict, now: float, claimed: dict | None = None, nodes: list[dict] | None = None) -> dict | None:
    """The slot a queued task should move to, or None. Same node and agent; rank_slots supplies tier order, rule 0,
    reviewer independence and concurrency; the slot must be free, no dearer than the current one, allowed by the
    task's pins, and admit the task as it would be after the move."""
    rules = budget.current_rules()
    kind = task.get("kind") or "draft"
    held = pins(task)
    current = handoff.slot_identity(task)
    current_tier = budget.entry_for(rules, task.get("budget_key"), task["agent"]).get("tier")
    claimed = {} if claimed is None else claimed
    frozen = budget.held_accounts()  # the owner's hold (req 9): nothing moves onto or off a held account
    if budget.task_account(task) in frozen:
        return None
    for slot in handoff.rank_slots(kind, [current], {"id": task["id"]}, now=now, nodes=nodes):
        if slot["agent"] != task["agent"] or slot["node"] != task["node_id"] or slot["state"] != "free":
            continue
        if slot["max"] is not None and slot["running"] + claimed.get((slot["account"], slot["key"]), 0) >= slot["max"]:
            continue
        if slot["account"] in frozen:
            continue
        if "profile" in held and slot["account"] != budget.task_account(task):
            continue
        if "model" in held and slot["key"] != task.get("budget_key"):
            continue
        if handoff.tier_rank(kind, slot["tier"]) > handoff.tier_rank(kind, current_tier):
            continue
        moved = {**task, "account": slot["account"], "profile": slot["profile"], "account_label": slot["label"],
                 "budget_key": slot["key"], "model": slot["model"],
                 "budget_identity": budget.identity(task["agent"], slot["model"])}
        if budget.start_wait(moved, now) != (None, None):
            continue
        return slot
    return None


def _waiting(now: float) -> list[dict]:
    with db() as c:
        ids = [r[0] for r in c.execute(
            "SELECT id FROM tasks WHERE state='queued' AND budget_wait IN ('limit','slot') AND COALESCE(runs,0)=0 "
            # A req-10 timed queue is still eligible to move before its reset if another account regains headroom.
            "AND (not_before IS NULL OR not_before>?) AND agent IN ('claude','codex') AND kind IS NOT NULL AND budget_key IS NOT NULL "
            "ORDER BY created_at, rowid", (now,))]
    return [tasks.get_task(i) for i in ids]


def timed_wait(task: dict, now: float, nodes: list[dict]) -> tuple[float, dict] | None:
    """The earliest fresh reset among this queued task's eligible, free placement targets.

    This is deliberately the req-3 ranking surface: its eligibility rules include capability, rule 0 and review
    independence.  A pinned field narrows it further, and the task never waits for a more expensive tier.
    """
    rules = budget.current_rules()
    kind = task.get("kind") or "draft"
    held = pins(task)
    current_tier = budget.entry_for(rules, task.get("budget_key"), task["agent"]).get("tier")
    candidates = [handoff.slot_identity(task), *handoff.rank_slots(kind, (), {"id": task["id"]}, now=now, nodes=nodes)]
    resets: list[tuple[float, dict]] = []
    seen = set()
    for slot in candidates:
        marker = (slot.get("account"), slot.get("key"), slot.get("model"))
        if marker in seen:
            continue
        seen.add(marker)
        if slot.get("agent", task["agent"]) != task["agent"] or slot.get("node", task["node_id"]) != task["node_id"]:
            continue
        if slot.get("account") in budget.held_accounts():
            continue
        if "profile" in held and slot.get("account") != budget.task_account(task):
            continue
        if "model" in held and slot.get("key") != task.get("budget_key"):
            continue
        if slot.get("tier") and handoff.tier_rank(kind, slot["tier"]) > handoff.tier_rank(kind, current_tier):
            continue
        key = slot.get("key") or task.get("budget_key")
        model = slot.get("model") or task.get("model")
        candidate = {**task, "account": slot.get("account"), "budget_key": key, "model": model,
                     "budget_identity": budget.identity(task["agent"], model)}
        if budget.start_wait(candidate, now)[0] != "limit":
            return None  # an eligible target has headroom or only waits for a slot
        reset = budget.near_limit_until(rules, slot.get("account"), task["agent"], now)
        if reset:
            resets.append((reset, {**slot, "key": key, "model": model}))
    return min(resets, default=None)


async def _apply_timed_wait(task: dict, reset: float, target: dict) -> bool:
    model = target.get("model") or budget.label(target.get("key"))
    reason = f"waiting for {model} at {budget.clock(reset)}"
    if not usage.is_live():
        if _once(("timed_wait", task["id"], reset, model)):
            emit(task["id"], "capacity.shadow_wait", {"title": task.get("title"), "not_before": reset,
                                                         "model": model, "reason": reason, "mode": "shadow"})
        return False
    async with tasks._locks[task["id"]]:
        current = tasks.get_task(task["id"])
        if current["state"] != "queued" or (current.get("runs") or 0) != 0:
            return False
        with db() as c:
            changed = c.execute(
                "UPDATE tasks SET node_id=?, profile=?, account=?, account_label=?, model=?, budget_key=?, "
                "budget_identity=?, not_before=?, budget_wait='limit', error=?, updated_at=? WHERE id=? "
                "AND state='queued' AND COALESCE(runs,0)=0 AND COALESCE(not_before,-1)=COALESCE(?,-1)",
                (target.get("node", task["node_id"]), target.get("profile", task.get("profile")),
                 target.get("account"), target.get("label", task.get("account_label")), target.get("model"),
                 target.get("key"), budget.identity(task["agent"], target.get("model")), reset, reason, time.time(),
                 task["id"], task.get("not_before"))).rowcount == 1
    if changed:
        emit(task["id"], "task.queued_for_usage", {"title": task.get("title"), "not_before": reset,
                                                      "model": model, "target": _view({**task, **target}), "reason": reason})
    return changed


async def migrate_tick(now: float | None = None) -> list[str]:
    """Re-rank every queued task that waits on a limit or a busy slot. Returns the ids moved (live mode)."""
    now = usage.now() if now is None else now
    claimed: dict = {}
    moved_ids = []
    nodes = tasks.node_rows()
    for task in _waiting(now):
        slot = choose_move(task, now, claimed, nodes)
        if slot is None:
            plan = timed_wait(task, now, nodes)
            if plan:
                await _apply_timed_wait(task, *plan)
            continue
        claimed[(slot["account"], slot["key"])] = claimed.get((slot["account"], slot["key"]), 0) + 1
        reason = task.get("error") or "waiting on a limit or a busy slot"
        if not usage.is_live():
            if _once(("move", task["id"], slot["account"], slot["key"])):
                emit(task["id"], "placement.shadow_move", {"title": task.get("title"), "from": _view(task),
                                                           "to": {"account": slot["account"], "profile": slot["profile"],
                                                                  "key": slot["key"], "model": slot["model"]},
                                                           "reason": reason, "mode": "shadow"})
            continue
        if await _apply_move(task["id"], slot, reason, task):
            moved_ids.append(task["id"])
    return moved_ids


async def _apply_move(task_id: str, slot: dict, reason: str, ranked: dict) -> bool:
    """Move `ranked` (the task as it was when `slot` was chosen) to `slot`, only if it still matches that row."""
    async with tasks._locks[task_id]:
        task = tasks.get_task(task_id)
        if task["state"] != "queued":
            return False
        before = _view(ranked)
        budget.held_accounts()  # reports a corrupt hold (an event can't be written inside the transaction below)
        with db() as c:  # the compare-and-set: only if it is still queued on exactly the slot we ranked from
            c.execute("BEGIN IMMEDIATE")  # the hold is read in the same transaction as the update (a set_hold waits)
            if {budget.task_account(task), slot["account"]} & budget.held_accounts(c, report=False):
                return False  # the owner froze an account between ranking and now
            moved = c.execute(
                "UPDATE tasks SET node_id=?, profile=?, account=?, account_label=?, model=?, budget_key=?, "
                "budget_identity=?, budget_wait=NULL, not_before=NULL, error=NULL, updated_at=? WHERE id=? AND state='queued' "
                "AND node_id=? AND COALESCE(profile,'fleet')=? AND COALESCE(account,'')=COALESCE(?,'') "
                "AND COALESCE(budget_key,'')=COALESCE(?,'') AND COALESCE(runs,0)=0 "
                "AND COALESCE(not_before,-1)=COALESCE(?,-1)",
                (slot["node"], slot["profile"], slot["account"], slot["label"], slot["model"], slot["key"],
                 budget.identity(task["agent"], slot["model"]), time.time(), task_id, ranked["node_id"],
                 before["profile"], ranked.get("account"), ranked.get("budget_key"), ranked.get("not_before"))).rowcount == 1
        if not moved:
            return False
        emit(task_id, "placement.moved", {"title": task.get("title"), "from": before,
                                          "to": {"account": slot["account"], "profile": slot["profile"],
                                                 "key": slot["key"], "model": slot["model"]},
                                          "reason": reason})
        await tasks.try_start(tasks.get_task(task_id))
        return True


# ---------- requirement 4: limit failures re-queue ----------

async def requeue_on_limit(task_id: str, now: float | None = None) -> bool:
    """A task that just failed on a provider limit goes back to `queued` with not_before = resets_at, unless a
    handoff to another slot can run it sooner (the existing handoff path then runs). Live mode only: it replaces
    the Part C scheduled resume, so in shadow mode it records the decision and leaves that path alone."""
    now = usage.now() if now is None else now
    task = tasks.get_task(task_id)
    if task.get("agent") not in budget.PAID_AGENTS or not handoff.limit_failure(task) or usage.mode() == "off":
        return False
    resets = task["resets_at"]
    if resets <= now:
        return False
    rules = budget.current_rules()
    if (budget.on_task_limit(rules, task.get("kind")) == "handoff" and task.get("worktree") and task.get("repo")
            and handoff.candidate_slots(task, rules, now)):
        return False  # a free slot elsewhere is sooner: the handoff moves it
    if not usage.is_live():
        if _once(("requeue", task_id, task.get("runs"))):
            emit(task_id, "task.requeue_shadow", {"title": task.get("title"), "not_before": resets, "mode": "shadow",
                                                  "reason": "usage limit; would wait for its reset"})
        return False
    async with tasks._locks[task_id]:
        task = tasks.get_task(task_id)
        why = f"usage limit; waiting for the reset at {budget.clock(resets)}"
        if not tasks.transition(task, {"failed"}, "queued", not_before=resets, ended_at=None, error=why,
                                budget_wait="limit", resolution=None):
            return False
    emit(task_id, "task.requeued", {"title": task.get("title"), "not_before": resets, "reason": "limit",
                                    "resets_at": resets})
    return True


async def restart_requeued(task: dict) -> bool:
    """Called by try_start (task lock held, limit cleared, not_before passed) for a re-queued task: continue its
    session, as a scheduled resume does. It passes the same continuation admission; it stays queued if refused."""
    verdict = tasks.continuation(task, now=usage.now())
    if verdict["outcome"] != "admit":
        tasks.update(task["id"], error=verdict["reason"])
        return False
    node = tasks.node_of(task)
    account = budget.task_account(task)
    budget.RESERVED[task["id"]] = (task["budget_key"], account, task.get("node_id"))
    try:
        await tasks.runner_call(node, "POST", f"/jobs/{task['id']}/message",
                                {"text": RESUME_TEXT, "allowed_tools": list(task.get("approved_tools") or []),
                                 "mode": None})
    except HTTPException as exc:
        tasks.update(task["id"], error=str(exc.detail))
        _restart_failed(task, str(exc.detail))
        return False
    finally:
        budget.RESERVED.pop(task["id"], None)
    runs = (task.get("runs") or 0) + 1
    if tasks.transition(task, {"queued"}, "working", where_node=task.get("node_id"), runs=runs, not_before=None,
                        ended_at=None, error=None, budget_wait=None, denials="[]", usage=None, num_turns=None,
                        run_prompt_bytes=len(RESUME_TEXT)):
        emit(task["id"], "task.resumed", {"title": task.get("title"), "run": runs, "from": "queued",
                                          "reason": "limit reset"})
    return True


def _restart_failed(task: dict, why: str) -> None:
    """Record one failed resume. The third for the same run records `task.restart_gave_up` and fails the task, so
    try_start stops retrying it every poll. (The task lock is held by the caller.)"""
    run = task.get("runs") or 0
    emit(task["id"], "task.restart_failed", {"title": task.get("title"), "run": run, "reason": why})
    with db() as c:
        rows = c.execute("SELECT data FROM events WHERE type='task.restart_failed' AND task_id=?",
                         (task["id"],)).fetchall()
    fails = sum(1 for r in rows if json.loads(r["data"] or "{}").get("run") == run)
    if fails < MAX_RESTART_FAILURES:
        return
    message = f"could not resume after {fails} tries: {why}"
    if tasks.transition(task, {"queued"}, "failed", ended_at=time.time(), error=message, budget_wait=None):
        emit(task["id"], "task.restart_gave_up", {"title": task.get("title"), "run": run, "tries": fails,
                                                  "reason": why})


# ---------- the tick ----------

async def tick(now: float | None = None) -> None:
    if usage.mode() == "off":
        return
    now = usage.now() if now is None else now
    try:
        shadow_limits(now)
    except Exception as exc:  # observation must never stop placement
        print(f"capacity shadow failed: {exc!r}")
    await migrate_tick(now)
