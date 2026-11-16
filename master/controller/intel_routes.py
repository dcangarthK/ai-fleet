"""Part I routes: /api/intel/* (docs/SPEC-intelligent-controller.md R4, R7-R11, section 5).

Read-only except POST /api/intel/cleanup (merged branches only) and POST /api/intel/estimate (records a
`suggest` decision, no prompt text). Nothing here approves, unblocks or overrides budget."""
from __future__ import annotations

import asyncio
import subprocess
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import budget
import intel
from store import db

router = APIRouter()

REVIEW_KINDS = ("review", "re_review", "final_review", "security_review")


CONCRETE_MODEL = {"codex": "gpt-5.6-terra"}  # the model auto_pick assigns when a candidate names none


def admission_allowed(rules: dict):
    """allowed(key, kind) for intel.rank: judged on the concrete identity the task is spawned as, with the same
    budget.match_key / budget.kind_allowed that budget.admit uses."""
    def allowed(key: str, kind: str) -> bool:
        agent, model = intel.split_key(key)
        model = model or CONCRETE_MODEL.get(agent)
        rkey = budget.match_key(rules, agent, model)
        entry = budget.entry_for(rules, rkey, agent) if rkey else {}
        return budget.kind_allowed(entry, kind) if entry else True  # no rule: admit() has nothing to refuse on
    return allowed


def _ready(c):
    intel.ensure_schema(c)
    return c


def build_stats(c, now: float | None = None) -> dict:
    """{(kind, key): {done, bad}} over finished tasks of the last 30 days, keyed by intel.key_of (shared by all callers)."""
    since = (now or time.time()) - 30 * 86400
    stats: dict = {}
    for r in c.execute("SELECT kind, agent, model, state, COUNT(*) AS n FROM tasks WHERE kind IS NOT NULL "
                       "AND state IN ('done','failed','capped','cancelled') AND created_at>=? "
                       "GROUP BY kind, agent, model, state", (since,)):
        s = stats.setdefault((r["kind"], intel.key_of(r["agent"], r["model"])), {"done": 0, "bad": 0})
        s["done" if r["state"] == "done" else "bad"] += r["n"]
    return stats


def exclusion(c, kind: str, parent_id: str | None) -> tuple:
    """R6: review kinds never pick the parent builder's key."""
    if parent_id and kind in REVIEW_KINDS:
        p = c.execute("SELECT agent, model FROM tasks WHERE id=?", (parent_id,)).fetchone()
        if p:
            return (intel.key_of(p["agent"], p["model"]),)
    return ()


def used_5h() -> dict:
    """{agent: lowest reported 5h used_percent among its accounts, or None}."""
    out = {}
    accounts = account_view()
    for agent in ("claude", "codex"):
        vals = [a["used_5h"] for a in accounts.values() if a["agent"] == agent and a["used_5h"] is not None]
        out[agent] = min(vals) if vals else None
    return out


def context(c, kind: str, parent_id: str | None = None) -> dict:
    """Evidence for rank(): rules, history stats, headroom, running load, per-task history rows."""
    now = time.time()
    rules = budget.current_rules()
    stats = build_stats(c, now)
    load: dict = {}
    for r in c.execute("SELECT agent, model, COUNT(*) AS n FROM tasks WHERE state IN ('working','running') "
                       "GROUP BY agent, model"):
        k = intel.key_of(r["agent"], r["model"])
        load[k] = load.get(k, 0) + r["n"]
    headroom: dict = {}
    for r in c.execute("SELECT role FROM assignments WHERE state='active'"):
        headroom[f"local:{r['role']}"] = {"routed": True, "used_percent": 0}
    accounts = account_view()
    held: dict = {}
    try:
        held = budget.limit_snapshot(rules, now).get("accounts", {})
    except Exception:  # noqa: BLE001 - headroom is advisory; admission still enforces limits
        pass
    for agent in ("claude", "codex"):
        mine = [a for a in accounts.values() if a["agent"] == agent]
        used = min((a["used_5h"] for a in mine if a["used_5h"] is not None), default=None)
        until = max(held.get(agent, 0) or 0, min((a["rejected_until"] or 0 for a in mine), default=0) if mine else 0)
        entry = {"used_percent": used, "limited_until": until or None}
        for key in intel.CANDIDATES:
            if intel.split_key(key)[0] == agent:
                headroom[key] = dict(entry)
    return {"rules": rules, "allowed": admission_allowed(rules), "stats": stats, "headroom": headroom, "load": load,
            "exclude": exclusion(c, kind, parent_id), "history": intel.history(c, now - 30 * 86400)}


def account_view() -> dict:
    """{account: forecast input + agent + rejected_until} from the runners' reported capacity."""
    out = {}
    for name, rec in budget.usage_records().items():
        w5, wk = rec["windows"].get("5h") or {}, rec["windows"].get("weekly") or {}
        used = w5.get("used_percent")
        out[name] = {"agent": rec["agent"], "used_5h": used, "resets_5h": w5.get("resets_at"),
                     "used_week": wk.get("used_percent"), "resets_week": wk.get("resets_at"),
                     "rejected_until": rec.get("rejected_until"),
                     "samples": [(w5["observed_at"], used)] if used is not None and w5.get("observed_at") else []}
    return out


def _shape(x: dict | None) -> dict | None:
    if not x:
        return None
    return {"agent": "ollama" if x["agent"] == "local" else x["agent"], "model": x["model"]}


@router.get("/api/intel/decisions")
def get_decisions(task_id: str | None = None, limit: int = 50) -> list[dict]:
    with db() as c:
        return intel.decisions(_ready(c), task_id, limit)


@router.get("/api/intel/tasks/{task_id}")
def task_decisions(task_id: str) -> list[dict]:
    with db() as c:
        return intel.decisions(_ready(c), task_id, 200)


@router.get("/api/intel/template")
def template(kind: str = "build", security: int = 0) -> dict:
    with db() as c:
        _ready(c)
        top = lambda k, **kw: next((x for x in intel.rank(k, **(context(c, k) | kw)) if x["eligible"]), None)  # noqa: E731
        builder = top(kind)
        final_kind = "security_review" if security else "final_review"
        reviewer = top("review", exclude=(builder["key"],) if builder else ())
        final = top(final_kind)
    why = lambda x: x["reasons"] if x else ["no eligible candidate"]  # noqa: E731
    return {"builder": _shape(builder), "reviewers": [_shape(reviewer)] if reviewer else [], "final": _shape(final),
            "gate_bounces": 3, "reasons": {"builder": why(builder), "reviewer": why(reviewer), "final": why(final)}}


def _git_rev(repo: str, ref: str) -> str | None:
    r = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", ref], capture_output=True,
                       text=True, timeout=10)
    return r.stdout.strip() if r.returncode == 0 else None


def _is_merged(repo: str | None, branch: str) -> bool:
    """True only when the branch has commits and its tip is an ancestor of main (never for tip == main)."""
    if not repo or not branch:
        return False
    try:
        tip, main = _git_rev(repo, branch), _git_rev(repo, "main")
        if not tip or not main or tip == main:
            return False  # missing ref, or never committed (tip == main)
        return subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", branch, "main"],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001 - any error means "not merged"
        return False


def _cleanable(c) -> list[dict]:
    rows = [dict(r) for r in c.execute(
        "SELECT id, state, branch, repo, node_id, worktree, cwd FROM tasks WHERE state IN ('done','failed','cancelled') "
        "AND worktree IS NOT NULL ORDER BY created_at DESC LIMIT 300")]
    return intel.cleanup_candidates(rows, _is_merged)


def _recheck(task_id: str) -> str | None:
    """Fresh check right before a removal. None = safe; else the reason to skip. Failed/cancelled tasks pass only
    when the branch has commits beyond main and is an ancestor of main (_is_merged); a worktree that exists on this
    machine must have a clean `git status`; a non-local one is only removable when the task is done."""
    import os
    with db() as c:
        r = c.execute("SELECT state, branch, repo, cwd FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not r:
        return "task gone"
    if r["state"] not in intel.TERMINAL:
        return f"state {r['state']}"
    if not _is_merged(r["repo"], r["branch"]):
        return "unmerged"
    cwd = r["cwd"]
    if cwd and os.path.isdir(cwd):
        try:
            st = subprocess.run(["git", "-C", cwd, "status", "--porcelain"], capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001 - cannot prove it is clean
            return "dirty"
        if st.returncode != 0 or st.stdout.strip():
            return "dirty"
    elif r["state"] != "done":
        return f"worktree not local and state is {r['state']}"
    return None


@router.get("/api/intel/cleanup")
def cleanup_list() -> list[dict]:  # sync def: FastAPI runs it in a threadpool, git calls stay off the loop
    with db() as c:
        return _cleanable(_ready(c))


@router.post("/api/intel/cleanup")
async def cleanup_run() -> dict:
    import tasks  # late: tasks owns the runner call

    def find():
        with db() as c:
            return _cleanable(_ready(c))
    found = await asyncio.to_thread(find)
    done, failed, skipped = [], [], []
    for t in found:
        why = await asyncio.to_thread(_recheck, t["id"])
        if why:
            skipped.append({"id": t["id"], "reason": why})
            with db() as c:
                intel.record(_ready(c), task_id=t["id"], kind="cleanup", action="cleanup_skip", chosen=t.get("branch"),
                             candidates=[], reasons=[why])
            continue
        try:
            await tasks.cleanup(t["id"])
            with db() as c:
                intel.record(_ready(c), task_id=t["id"], kind="cleanup", action="cleanup", chosen=t["branch"],
                             candidates=[], reasons=["merged into main"])
            done.append(t["id"])
        except HTTPException as e:
            failed.append({"id": t["id"], "error": e.detail})
        except Exception as e:  # noqa: BLE001 - one bad item never stops the rest
            failed.append({"id": t["id"], "error": f"{e.__class__.__name__}: {e}"})
    return {"cleaned": done, "failed": failed, "skipped": skipped}


@router.get("/api/intel/savings")
def get_savings(since: float | None = None, baseline_since: float | None = None,
                baseline_until: float | None = None) -> dict:
    since = intel.INTEL_EPOCH if since is None else since
    until = since if baseline_until is None else baseline_until
    base = until - 48 * 3600 if baseline_since is None else baseline_since
    with db() as c:
        return intel.savings(_ready(c), since, base, until)


@router.get("/api/intel/forecast")
def get_forecast() -> dict:
    accounts = account_view()
    with db() as c:
        ctx = context(_ready(c), "build")
    cands = intel.rank("build", **ctx)
    return {"accounts": intel.forecast(accounts, time.time()),
            "build_goes_to": _shape(next((x for x in cands if x["eligible"]), None))}


class EstimateIn(BaseModel):
    kind: str
    agent: str | None = None
    model: str | None = None
    prompt_bytes: int = 0
    parent_id: str | None = None


@router.post("/api/intel/estimate")
def post_estimate(body: EstimateIn) -> dict:
    with db() as c:
        _ready(c)
        ctx = context(c, body.kind, body.parent_id)
        cands = intel.rank(body.kind, prompt_bytes=body.prompt_bytes, **ctx)
        named = intel.key_of(body.agent, body.model) if body.agent and body.agent != "auto" else None
        chosen = next((x for x in cands if x["key"] == named), None) if named else next(
            (x for x in cands if x["eligible"]), cands[0])
        key = chosen["key"] if chosen else named
        est = intel.estimate(body.kind, key or "", ctx["history"], body.prompt_bytes)
        agent = intel.split_key(key)[0] if key else None
        fc = intel.forecast({n: a for n, a in account_view().items() if a["agent"] == agent}, time.time())
        intel.record(c, task_id=body.parent_id, kind=body.kind, action="suggest", chosen=key, candidates=cands[:5],
                     reasons=chosen["reasons"] if chosen else [], est_tokens=est["tokens"], est_usd=est["usd"])
    return {"candidates": cands, "chosen_estimate": est, "forecast": fc}
