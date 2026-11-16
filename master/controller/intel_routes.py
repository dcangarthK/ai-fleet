"""Part I routes: /api/intel/* (docs/SPEC-intelligent-controller.md R4, R7-R11, section 5).

Read-only except POST /api/intel/cleanup (merged branches only) and POST /api/intel/estimate (records a
`suggest` decision, no prompt text). Nothing here approves, unblocks or overrides budget."""
from __future__ import annotations

import subprocess
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import budget
import intel
from store import db

router = APIRouter()

REVIEW_KINDS = ("review", "re_review", "final_review", "security_review")


def _ready(c):
    intel.ensure_schema(c)
    return c


def context(c, kind: str, parent_id: str | None = None) -> dict:
    """Evidence for rank(): rules, history stats, headroom, running load, per-task history rows."""
    now = time.time()
    rules = budget.current_rules()
    stats: dict = {}
    for r in c.execute("SELECT kind, agent, model, state, COUNT(*) AS n FROM tasks WHERE kind IS NOT NULL "
                       "AND state IN ('done','failed','capped') GROUP BY kind, agent, model, state"):
        s = stats.setdefault((r["kind"], intel.key_of(r["agent"], r["model"])), {"done": 0, "bad": 0})
        s["done" if r["state"] == "done" else "bad"] += r["n"]
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
    exclude = ()
    if parent_id and kind in REVIEW_KINDS:
        p = c.execute("SELECT agent, model FROM tasks WHERE id=?", (parent_id,)).fetchone()
        exclude = (intel.key_of(p["agent"], p["model"]),) if p else ()
    return {"rules": rules, "stats": stats, "headroom": headroom, "load": load, "exclude": exclude,
            "history": intel.history(c, now - 30 * 86400)}


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


def auto_pick(kind: str, *, parent_id: str | None = None, task_id: str | None = None, prompt_bytes: int = 0) -> dict:
    """R6: the top eligible non-local candidate for `kind`, recorded as one `auto_pick`. Raises LookupError if none."""
    with db() as c:
        _ready(c)
        ctx = context(c, kind, parent_id)
        cands = intel.rank(kind, prompt_bytes=prompt_bytes, **ctx)
        top = [x for x in cands if x["eligible"]]
        pick = next((x for x in top if x["agent"] != "local"), None)
        if pick is None:
            raise LookupError(f"no eligible agent for {kind}")
        reasons = list(pick["reasons"])
        if top and top[0]["agent"] == "local":
            reasons.insert(0, f"{top[0]['key']} ranked first but local picks are not taken for agent tasks")
        intel.record(c, task_id=task_id, kind=kind, action="auto_pick", chosen=pick["key"], candidates=cands[:5],
                     reasons=reasons, est_tokens=pick["est_tokens"], est_usd=pick["est_usd"])
    return pick


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


def _is_merged(repo: str | None, branch: str) -> bool:
    try:
        return subprocess.run(["git", "-C", repo or ".", "merge-base", "--is-ancestor", branch, "main"],
                              capture_output=True, timeout=10).returncode == 0
    except Exception:  # noqa: BLE001 - any error means "not merged"
        return False


def _cleanable(c) -> list[dict]:
    rows = [dict(r) for r in c.execute(
        "SELECT id, state, branch, repo, node_id, worktree FROM tasks WHERE state IN ('done','failed','cancelled') "
        "AND worktree IS NOT NULL ORDER BY created_at DESC LIMIT 300")]
    return intel.cleanup_candidates(rows, _is_merged)


@router.get("/api/intel/cleanup")
def cleanup_list() -> list[dict]:
    with db() as c:
        return _cleanable(_ready(c))


@router.post("/api/intel/cleanup")
async def cleanup_run() -> dict:
    import tasks  # late: tasks owns the runner call
    with db() as c:
        found = _cleanable(_ready(c))
    done, failed = [], []
    for t in found:
        try:
            await tasks.cleanup(t["id"])
            done.append(t["id"])
        except HTTPException as e:
            failed.append({"id": t["id"], "error": e.detail})
            continue
        with db() as c:
            intel.record(_ready(c), task_id=t["id"], kind="cleanup", action="cleanup", chosen=t["branch"],
                         candidates=[], reasons=["merged into main"])
    return {"cleaned": done, "failed": failed}


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
