"""The one limit policy (plan 1.1), running in SHADOW only (plan 1.0): `plan_limit` says what a single owner of the
limit decision would do; the existing writers keep acting and `shadow_compare` logs where they differ.
Nothing here writes task state. Behaviour of the current writers: docs/SPEC-limit-policy.md."""
import time
from dataclasses import dataclass

import budget
import decisions
import handoff
import tasks
from events import emit


@dataclass(frozen=True)
class Park:
    until: float
    reason: str = ""


@dataclass(frozen=True)
class Handoff:
    slot: dict


@dataclass(frozen=True)
class Fail:
    reason: str


def plan_limit(task: dict, rules: dict | None = None, now: float | None = None):
    """Park(until) | Handoff(slot) | Fail(reason) for a task that stopped on a provider limit. One wait time: the
    task's own reset (no resume delay). Same eligibility order as handoff.handle_limit."""
    now = time.time() if now is None else now
    rules = budget.current_rules() if rules is None else rules
    if not handoff.limit_failure(task):
        return Fail("not a limit failure")
    resets = task.get("resets_at")
    if not resets:
        return Fail("no reset time known")
    if budget.on_task_limit(rules, task.get("kind")) != "handoff":
        return Park(resets, f"{task.get('kind') or 'this kind'} waits for the reset")
    if tasks._profile_is_pinned(task):
        return Park(resets, "profile is pinned")
    if handoff.handoffs_in_chain(task["id"]) >= int((rules.get("limits") or {}).get("max_handoffs", 2)):
        return Park(resets, "no handoffs left for this work item")
    if not task.get("worktree") or not task.get("repo"):
        return Park(resets, "no worktree to checkpoint")
    choices = handoff.candidate_slots(task, rules, now)
    if not choices:
        return Park(resets, "no eligible slot")
    return Handoff(choices[0])


def describe(plan) -> dict:
    if isinstance(plan, Park):
        return {"action": "park", "until": plan.until, "reason": plan.reason}
    if isinstance(plan, Handoff):
        return {"action": "handoff", "slot": {k: plan.slot.get(k) for k in ("node", "agent", "profile", "model")}}
    return {"action": "fail", "reason": plan.reason}


def mismatch(actual: dict, shadow: dict) -> bool:
    """Different action, or both park and the wait ends at a different time, or both hand off to known, different
    slots."""
    if actual.get("action") != shadow.get("action"):
        return True
    if actual["action"] == "park":
        return abs((actual.get("until") or 0) - (shadow.get("until") or 0)) > 1
    if actual["action"] == "handoff" and actual.get("slot") and shadow.get("slot"):
        return actual["slot"] != shadow["slot"]
    return False


def shadow_plan(task: dict, rules: dict | None = None, now: float | None = None) -> dict | None:
    """plan_limit as a dict, taken BEFORE the writer acts (afterwards the task's state has moved). Never raises."""
    try:
        return describe(plan_limit(task, rules, now))
    except Exception as exc:
        print(f"limit shadow failed: {exc!r}")
        return None


def shadow_compare(task: dict, actual: dict, rule: str, shadow: dict | None) -> bool | None:
    """Write one decision row (actual vs shadow, with the mismatch flag) and a `limit.shadow_mismatch` event when
    they differ. Never raises. `actual` is {"action": park|handoff|fail, ...} as describe() shapes it. Returns the
    mismatch flag, or None when there was no shadow decision."""
    try:
        if shadow is None:
            return None
        flag = mismatch(actual, shadow)
        decisions.record(task["id"], "limit." + actual["action"],
                         {"failure_reason": task.get("failure_reason"), "kind": task.get("kind"),
                          "resets_at": task.get("resets_at"), "mismatch": flag},
                         None, rule, actual, shadow)
        if flag:
            emit(task["id"], "limit.shadow_mismatch", {"title": task.get("title"), "rule": rule, "actual": actual,
                                                       "shadow": shadow})
        return flag
    except Exception as exc:
        print(f"limit shadow failed: {exc!r}")
        return None
