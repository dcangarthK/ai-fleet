"""Part D: the Live page's data, self-reported activity, chains, the owner's limit reset, runner heartbeats, the
liveness sweep and failover (docs/SPEC-live-monitor.md, including §12 and §13).

Nothing here starts work, changes admission or reads prompts beyond titles. /api/live is view scope and built from
state the controller already holds (node polls, heartbeats, the database): it makes no runner calls.
"""
import asyncio
import collections
import hashlib
import hmac
import json
import re
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import budget
import handoff
import presence
import redaction
import tasks
from events import emit
from store import db, task_root

router = APIRouter()

STARTED_AT = time.time()  # the startup grace for heartbeat expiry runs from here (12.2)
SWEEP_SECONDS = 5
DAY = 86400

# ---------- in-memory state (reset_state() for tests) ----------

ACTIVITY_POSTS: dict[str, collections.deque] = collections.defaultdict(collections.deque)  # actor -> post times
HEARTBEAT_ACCEPTED: dict[str, float] = {}  # node -> when its last heartbeat was stored
HEARTBEAT_FAILURES: dict[str, collections.deque] = collections.defaultdict(collections.deque)  # source -> times
LIVENESS: dict[str, bool] = {}  # node -> offline, as the sweep last saw it
WARNINGS: dict[tuple[str, str], bool] = {}  # (node, event type) -> active


def reset_state() -> None:
    for store in (ACTIVITY_POSTS, HEARTBEAT_ACCEPTED, HEARTBEAT_FAILURES, LIVENESS, WARNINGS):
        store.clear()


def _rate(bucket: collections.deque, limit: int, window: float, now: float) -> bool:
    """True when another event fits in the window (and records it)."""
    while bucket and bucket[0] <= now - window:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True


# ---------- accounts and labels ----------

def all_slots() -> list[dict]:
    """Every paid slot on every approved node with a runner, usable or not."""
    flags = tasks.signed_out()
    return [s for n in tasks.node_rows() if n["status"] == "approved" and n.get("runner")
            for agent in sorted(budget.PAID_AGENTS) if ((n["runner"].get("agents") or {}).get(agent))
            for s in tasks.node_slots(n, agent, flags)]


def known_labels() -> set[str]:
    return {s["label"] for s in all_slots() if s["label"]}


def known_accounts() -> dict[str, dict]:
    """account key -> {account, agent, label}: from the runners' slots, plus accounts that ran fleet work."""
    out = {s["account"]: {"account": s["account"], "agent": s["agent"], "label": s["label"]} for s in all_slots()}
    with db() as c:
        for r in c.execute("SELECT DISTINCT account, agent, account_label FROM agent_usage WHERE account IS NOT NULL "
                           "AND agent IN ('claude','codex')"):
            out.setdefault(r["account"], {"account": r["account"], "agent": r["agent"], "label": r["account_label"]})
    return out


def note_presence(request: Request) -> None:
    """auth's presence hook: bridge headers from local or operate+ callers (2.4)."""
    if "x-fleet-session" in request.headers:
        presence.note(request.headers, getattr(request.state, "scope", "view"), known_labels)


# ---------- §2.3 self-reported activity ----------

ACTIVITY_KINDS = set(budget.KINDS) | {"orchestrator", "hints", "subagent"}
ACTIVITY_STATUSES = ("running", "done", "failed")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
TASK_ID_RE = re.compile(r"^t-[0-9a-f]{8}$")
TEXT_CAP = 300


class ActivityIn(BaseModel):
    id: str | None = None
    title: str | None = None
    kind: str | None = None
    account_label: str | None = None
    model: str | None = None
    work_item: str | None = None
    status: str | None = None
    tokens: dict | None = None
    note: str | None = None


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    return " ".join(redaction.redact(value, strict=True).split())[:TEXT_CAP] or None


def _tokens(value: dict | None) -> str | None:
    if value is None:
        return None
    out = {}
    for key in ("input", "output", "cached"):
        found = value.get(key)
        if found is None:
            continue
        if not isinstance(found, int) or isinstance(found, bool) or found < 0:
            raise HTTPException(400, f"tokens.{key} must be a whole number of tokens")
        out[key] = found
    return json.dumps(out)


def activity_view(row) -> dict:
    out = dict(row)
    out["tokens"] = json.loads(out["tokens"]) if out.get("tokens") else None
    out["self_reported"] = True
    return out


@router.post("/api/activity")
def report_activity(body: ActivityIn, request: Request) -> dict:
    """Operate scope. A record of paid work that couldn't run as a fleet task: labelled "self-reported" everywhere,
    never used for enforcement, outcomes, pipelines or independence."""
    # The token's unique id (labels aren't unique): rate limits and record ownership key on it (Codex review).
    actor = getattr(request.state, "actor_id", None) or getattr(request.state, "actor", None) or "local"
    session = presence.session_of(request.headers)
    now = time.time()
    if not _rate(ACTIVITY_POSTS[actor], 60, 60, now):  # 60 a minute per actor, in total (Fable's note a)
        raise HTTPException(429, "too many activity reports: at most 60 a minute")
    if body.status is not None and body.status not in ACTIVITY_STATUSES:
        raise HTTPException(400, f"status must be one of {list(ACTIVITY_STATUSES)}")
    if body.kind is not None and body.kind not in ACTIVITY_KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(ACTIVITY_KINDS)}")
    if body.account_label is not None and body.account_label not in known_labels():
        raise HTTPException(400, f"unknown account label {body.account_label}")
    if body.model is not None and not MODEL_RE.fullmatch(body.model):
        raise HTTPException(400, "model must be a model name")
    if body.work_item is not None:
        if not TASK_ID_RE.fullmatch(body.work_item):
            raise HTTPException(400, "work_item must be a task id")
        with db() as c:
            if not c.execute("SELECT 1 FROM tasks WHERE id=?", (body.work_item,)).fetchone():
                raise HTTPException(400, f"unknown task {body.work_item}")
    fields = {"title": _text(body.title), "kind": body.kind, "account_label": body.account_label,
              "model": body.model, "work_item": body.work_item, "status": body.status, "tokens": _tokens(body.tokens),
              "note": _text(body.note)}
    fields = {k: v for k, v in fields.items() if v is not None}
    if body.status in ("done", "failed"):
        fields["ended_at"] = now
    if body.id:
        with db() as c:
            row = c.execute("SELECT * FROM activity WHERE id=?", (body.id,)).fetchone()
            if not row or row["actor"] != actor:
                raise HTTPException(404, f"unknown activity {body.id}")
            fields["updated_at"] = now
            c.execute(f"UPDATE activity SET {', '.join(k + '=?' for k in fields)} WHERE id=?",
                      (*fields.values(), body.id))
            row = c.execute("SELECT * FROM activity WHERE id=?", (body.id,)).fetchone()
    else:
        missing = [k for k in ("title", "kind", "account_label", "status") if not fields.get(k)]
        if missing:
            raise HTTPException(400, f"a new activity needs {', '.join(missing)}")
        record = {"id": "a-" + secrets.token_hex(4), "ts": now, "updated_at": now, "actor": actor,
                  "session": session} | fields
        with db() as c:
            c.execute(f"INSERT INTO activity ({', '.join(record)}) VALUES ({', '.join('?' * len(record))})",
                      tuple(record.values()))
            row = c.execute("SELECT * FROM activity WHERE id=?", (record["id"],)).fetchone()
    view = activity_view(row)
    # In the timeline of its work item (if any), clearly self-reported.
    emit(view.get("work_item"), "activity.self_reported", {"title": view["title"], "status": view["status"],
                                                          "kind": view["kind"], "label": view["account_label"],
                                                          "self_reported": True})
    return view


# ---------- §3 chains and stages ----------

class ChainIn(BaseModel):
    stages: list[dict] = Field(default_factory=list)


@router.put("/api/work/{root_id}/chain")
def set_chain(root_id: str, body: ChainIn) -> dict:
    """Operate scope. A descriptive chain on a work item's root: it never starts anything or affects admission."""
    with db() as c:
        if not c.execute("SELECT 1 FROM tasks WHERE id=?", (root_id,)).fetchone():
            raise HTTPException(404, f"unknown task {root_id}")
        if task_root(c, root_id) != root_id:
            raise HTTPException(400, "a chain goes on the work item's root task")
    if len(body.stages) > 20:
        raise HTTPException(400, "at most 20 stages")
    chain = []
    for stage in body.stages:
        name, agent = stage.get("stage"), stage.get("agent")
        if not isinstance(name, str) or not tasks.STAGE_RE.fullmatch(name):
            raise HTTPException(400, "each stage needs a name of 1-24 lowercase letters, digits or dashes")
        if agent not in tasks.AGENTS:
            raise HTTPException(400, f"agent must be one of {sorted(tasks.AGENTS)}")
        entry = {"stage": name, "agent": agent}
        if stage.get("model") is not None:
            if not isinstance(stage["model"], str) or not MODEL_RE.fullmatch(stage["model"]):
                raise HTTPException(400, "model must be a model name")
            entry["model"] = stage["model"]
        if stage.get("profile") is not None:
            if not isinstance(stage["profile"], str) or not tasks.PROFILE_RE.fullmatch(stage["profile"]):
                raise HTTPException(400, "profile must be a runner profile id")
            entry["profile"] = stage["profile"]
        chain.append(entry)
    tasks.update(root_id, chain=json.dumps(chain))
    return {"root_id": root_id, "chain": chain}


def attempt_state(t: dict) -> str:
    if t.get("resolution"):
        return "superseded"
    if t["state"] in tasks.ACTIVE:
        return "current"
    if t["state"] in ("review", "done"):
        return "done"
    return "failed"


def chain_view(chain: list[dict], members: list[dict]) -> list[dict]:
    """§3.2: per chain stage, the latest attempt with that stage decides its state; a repeat shows its count."""
    out = []
    for stage in chain:
        tries = [m for m in members if m.get("stage") == stage["stage"]]
        latest = tries[-1] if tries else None
        out.append(stage | {"state": attempt_state(latest) if latest else "next", "count": len(tries),
                            "task_id": latest["id"] if latest else None})
    return out


def pipeline_strip(p: dict) -> list[dict]:
    """§3.1: a pipeline's stages from its own history (gate and fix rounds included)."""
    names: list[str] = []
    for entry in p.get("history") or []:
        state = entry.get("state")
        if state == "building":
            names += ["fix" if "build" in names else "build"] + (["gate"] if p.get("gate") else [])
        elif state in ("reviewing", "review_waiting"):
            if not names or names[-1] != "review":
                names.append("review")
        elif state in ("ready", "merged", "failed", "escalated", "cancelled"):
            names.append(state)
    terminal = p.get("state") in ("ready", "merged", "failed", "escalated", "cancelled")
    strip = [{"stage": n, "state": "done"} for n in names]
    if strip:
        if not terminal:
            strip[-1]["state"] = "current"
        elif p.get("state") in ("failed", "escalated", "cancelled"):
            strip[-1]["state"] = "failed"
    return strip


# ---------- §4.2 /api/live ----------

WORKER_FIELDS = ("id, title, agent, model, account_label, profile, kind, effort, stage, node_id, branch, started_at, "
                 "independence, state, created_at, budget_wait, error, account, budget_identity")


def _json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def workers(now: float, nodes: list[dict]) -> list[dict]:
    with db() as c:
        running = [dict(r) for r in c.execute(
            f"SELECT {WORKER_FIELDS} FROM tasks WHERE state IN ('starting','working') ORDER BY started_at, rowid")]
        roots = {r["id"]: task_root(c, r["id"]) for r in running}
        last_end = {(r["node_id"], r["agent"], r["profile"]): r["ended"] for r in c.execute(
            "SELECT node_id, agent, COALESCE(profile,'fleet') AS profile, MAX(ended_at) AS ended FROM tasks "
            "WHERE ended_at IS NOT NULL GROUP BY node_id, agent, COALESCE(profile,'fleet')")}
    rows = []
    for r in running:
        waiver = _json(r["independence"])
        rows.append({"task_id": r["id"], "title": r["title"], "agent": r["agent"], "model": r["model"],
                     "account_label": r["account_label"], "profile": r["profile"], "kind": r["kind"],
                     "effort": r["effort"], "stage": r["stage"], "work_item": roots[r["id"]], "node": r["node_id"],
                     "branch": r["branch"], "started_at": r["started_at"],
                     "elapsed_s": round(now - r["started_at"]) if r["started_at"] else None,
                     "independence": "waived" if (waiver or {}).get("waived") else None,
                     "waiver_reason": (waiver or {}).get("reason")})
    busy = {(r["node"], r["agent"], r["profile"] if r["agent"] in budget.PAID_AGENTS else None) for r in rows}
    # Idle rows: one per (node, agent, profile) with nothing running, sharing Part C's Resources helper.
    limited: dict[tuple, float] = {}
    for res in handoff.resources(nodes=nodes, now=now):
        if res["state"] in ("limited", "at_budget") and res["until"]:
            key = (res["node"], res["agent"], res["profile"])
            limited[key] = max(limited.get(key, 0), res["until"])
    flags = tasks.signed_out()
    codex_seen = False
    for node in nodes:
        if node["status"] != "approved" or not node.get("runner"):
            continue
        agents = node["runner"].get("agents") or {}
        for agent in ("claude", "codex", "ollama"):
            if not agents.get(agent):
                continue
            slots = tasks.node_slots(node, agent, flags) if agent in budget.PAID_AGENTS else [{"profile": None, "label": None}]
            for slot in slots:
                codex_seen = codex_seen or agent == "codex"
                key = (node["id"], agent, slot["profile"])
                if key in busy:
                    continue
                rows.append({"task_id": None, "agent": agent, "node": node["id"], "node_name": node.get("name"),
                             "profile": slot["profile"], "account_label": slot.get("label"),
                             "idle_since": last_end.get((node["id"], agent, slot["profile"] or "fleet")),
                             "limited_until": limited.get(key),
                             "signed_in": not slot.get("signed_out", False),
                             "offline": tasks.runner_offline(node, now)})
    if not codex_seen and not any(r["agent"] == "codex" for r in rows):
        rows.append({"task_id": None, "agent": "codex", "node": None, "profile": None, "account_label": None,
                     "idle_since": None, "limited_until": None, "signed_in": None, "offline": "no node has Codex"})
    return rows


def queue(now: float) -> list[dict]:
    with db() as c:
        rows = [dict(r) for r in c.execute(
            f"SELECT {WORKER_FIELDS} FROM tasks WHERE state IN ('queued','awaiting_approval') "
            "ORDER BY created_at, rowid")]
    rules = budget.current_rules()
    out = []
    for r in rows:
        until = None
        if r["state"] == "awaiting_approval":
            wait, reason = "approval", "needs your approval"
        elif r["budget_wait"] in ("limit", "slot"):
            wait, reason = r["budget_wait"], r["error"]
            if wait == "limit" and r["agent"] in budget.PAID_AGENTS:
                snap = budget.limit_snapshot(rules, now, budget.task_account(r))
                ident = r["budget_identity"] or budget.identity(r["agent"], r["model"])
                until = budget.limited_for(snap, r["agent"], ident)[0]
        elif r["error"]:
            wait, reason = "node", r["error"]
        else:
            wait, reason = "starting", None
        out.append({"task_id": r["id"], "title": r["title"], "agent": r["agent"], "model": r["model"],
                    "account_label": r["account_label"], "kind": r["kind"], "stage": r["stage"], "node": r["node_id"],
                    "wait": wait, "reason": (reason or "")[:200] or None, "until": until,
                    "queued_s": round(now - r["created_at"])})
    return out


def chains(now: float) -> list[dict]:
    out = []
    with db() as c:
        pipelines = [dict(r) for r in c.execute(
            "SELECT id, title, state, round, gate, history FROM pipelines WHERE state NOT IN "
            "('ready','failed','escalated','cancelled','merged') OR updated_at>? ORDER BY updated_at DESC LIMIT 20",
            (now - DAY,))]
        recent = [r[0] for r in c.execute("SELECT id FROM tasks WHERE updated_at>? ORDER BY updated_at DESC LIMIT 300",
                                          (now - DAY,))]  # newest activity first, bounded
        roots = list(dict.fromkeys(task_root(c, i) for i in recent))[:30]
        members_by_root: dict[str, list[dict]] = {}
        for root in roots:
            tree = [dict(r) for r in c.execute(
                "WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT t.id FROM tasks t JOIN tree ON t.parent_id=tree.id) "
                "SELECT id, title, stage, state, resolution, link, chain, created_at, hidden FROM tasks "
                "WHERE id IN (SELECT id FROM tree) ORDER BY created_at, rowid", (root,))]
            members_by_root[root] = tree
    for p in pipelines:
        p["history"] = _json(p["history"]) or []
        out.append({"pipeline_id": p["id"], "title": p["title"], "state": p["state"], "stages": pipeline_strip(p)})
    for root, members in members_by_root.items():
        if not members or members[0]["id"] != root or members[0]["hidden"]:
            continue
        for m in members:
            m["resolution"] = _json(m["resolution"])
        chain = _json(members[0]["chain"])
        if chain is None:
            # No declared chain: the attempts themselves, by stage (or link) in order.
            chain = [{"stage": s} for s in dict.fromkeys(m.get("stage") or m.get("link") or "task" for m in members)]
            for m in members:
                m["stage"] = m.get("stage") or m.get("link") or "task"
        out.append({"root_id": root, "title": members[0]["title"], "declared": members[0]["chain"] is not None,
                    "stages": chain_view(chain, members)})
    return out


def accounts(now: float) -> list[dict]:
    rules = budget.current_rules()
    out = []
    with db() as c:
        for acct in sorted(known_accounts().values(), key=lambda a: a["account"]):
            account = acct["account"]
            last = c.execute("SELECT ts, resets_at FROM agent_usage WHERE account=? AND outcome='limit' "
                             "ORDER BY ts DESC LIMIT 1", (account,)).fetchone()
            reset = c.execute("SELECT MAX(at) FROM limit_resets WHERE account=?", (account,)).fetchone()[0]
            since = last["ts"] if last else now - 5 * 3600
            fleet = c.execute("SELECT SUM(input), SUM(output), SUM(cached) FROM agent_usage WHERE account=? AND ts>?",
                              (account, since)).fetchone()
            mine = {"input": 0, "output": 0, "cached": 0}
            if acct["label"]:
                for r in c.execute("SELECT tokens FROM activity WHERE account_label=? AND ts>? AND tokens IS NOT NULL",
                                   (acct["label"], since)):
                    for key, value in (_json(r[0]) or {}).items():
                        if key in mine and isinstance(value, int):
                            mine[key] += value
            running = c.execute(f"SELECT COUNT(*) FROM tasks WHERE state IN ('starting','working') AND "
                                f"{budget.ACCOUNT_SQL}=?", (account,)).fetchone()[0]
            snap = budget.limit_snapshot(rules, now, account)
            until, _ = budget.limited_for(snap, acct["agent"], acct["agent"])
            if until:
                status = f"limited until {budget.clock(until)}"
            elif reset and (not last or reset >= last["ts"]):
                status = f"reset by owner at {budget.clock(reset)}"
            elif last and now - last["ts"] < 5 * 3600:
                status = "limit hit less than 5 h ago (reset passed)"
            else:
                status = "no recent limit"
            out.append(acct | {"last_limit": {"at": last["ts"], "resets_at": last["resets_at"]} if last else None,
                               "limited_until": until, "reset_at": reset, "since": since,
                               "tokens_since": {"input": fleet[0], "output": fleet[1], "cached": fleet[2]},
                               "self_reported_tokens": mine, "running": running, "status": status})
    return out


SWAP_GB, SWAP_SHARE, DISK_GB, LOAD_FACTOR, METRICS_STALE = 4.0, 0.25, 10.0, 2.0, 60.0
METRIC_KEYS = ("mem_total_gb", "mem_used_gb", "swap_used_gb", "disk_free_gb", "load5", "cores")


def machine_row(node: dict, now: float) -> dict:
    runner = node.get("runner") or {}
    metrics = runner.get("metrics") if isinstance(runner.get("metrics"), dict) else {}
    seen = max([t for t in (node.get("heartbeat_at"), node.get("polled_at")) if t] or [0])
    runner_age = node.get("runner_age_s")
    if runner_age is None:
        runner_age = metrics.get("age_s") if isinstance(metrics.get("age_s"), (int, float)) else 0
    age = (now - seen + (runner_age or 0)) if seen else None
    unknown = not metrics or age is None or age > METRICS_STALE
    values = {k: metrics.get(k) if isinstance(metrics.get(k), (int, float)) else None for k in METRIC_KEYS}
    warnings = []
    if not unknown:
        swap, total = values["swap_used_gb"], values["mem_total_gb"]
        if swap is not None and (swap > SWAP_GB or (total and swap > SWAP_SHARE * total)):
            warnings.append("swap")
        if values["disk_free_gb"] is not None and values["disk_free_gb"] < DISK_GB:
            warnings.append("disk")
        if values["load5"] is not None and values["cores"] and values["load5"] > LOAD_FACTOR * values["cores"]:
            warnings.append("load")
    ollama = metrics.get("ollama") if isinstance(metrics.get("ollama"), dict) else {}
    beat = node.get("heartbeat_at")
    heartbeat = None
    if beat:
        heartbeat = "ok" if now - beat <= tasks.HEARTBEAT_TTL else "lost, polling"
    return {"node": node["id"], "name": node.get("name"), "age_s": round(age) if age is not None else None,
            "unknown": unknown, **values, "ollama_up": ollama.get("up"),
            "loaded": [m for m in ollama.get("loaded") or [] if isinstance(m, str)],
            "warm": sorted(tasks.warm_models(node, now)), "warnings": warnings,
            "offline": tasks.runner_offline(node, now), "heartbeat": heartbeat}


ALERT_TYPES = ("task.awaiting_approval", "task.blocked", "task.failed", "budget.limit", "budget.rules_invalid",
               "budget.limit_unrecognised", "node.swap_high", "node.disk_low", "node.runner_offline")


def alerts() -> list[dict]:
    marks = ",".join("?" * len(ALERT_TYPES))
    with db() as c:
        rows = c.execute(f"SELECT id, ts, task_id, type, data FROM events WHERE type IN ({marks}) "
                         "ORDER BY id DESC LIMIT 10", ALERT_TYPES).fetchall()
    out = []
    for r in rows:
        data = _json(r["data"]) or {}
        # Titles and node ids only: never result, log or limit-message text.
        out.append({"id": r["id"], "ts": r["ts"], "task_id": r["task_id"], "type": r["type"],
                    "title": data.get("title") if isinstance(data.get("title"), str) else None,
                    "node": data.get("node") if isinstance(data.get("node"), str) else None,
                    "state": data.get("state") if isinstance(data.get("state"), str) else None})
    return out


def self_reported(now: float) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM activity WHERE status='running' OR ended_at>? ORDER BY ts DESC LIMIT 50",
                         (now - DAY,)).fetchall()
    return [{k: v for k, v in activity_view(r).items() if k != "actor"} for r in rows]


@router.get("/api/live")
def live_view() -> dict:
    """View scope: who is doing what, where, on which account, right now (docs/SPEC-live-monitor.md 4.2)."""
    now = time.time()
    nodes = tasks.node_rows()
    return {"generated_at": now, "orchestrator": presence.sessions(now), "workers": workers(now, nodes),
            "queue": queue(now), "chains": chains(now), "accounts": accounts(now),
            "self_reported": self_reported(now),
            "machines": [machine_row(n, now) for n in nodes if n["status"] == "approved" and n.get("runner")],
            "alerts": alerts()}


# ---------- §13 (b) the owner's limit reset ----------

class ResetIn(BaseModel):
    account: str


@router.post("/api/budget/accounts/reset")
def reset_limit(body: ResetIn, request: Request) -> dict:
    """Admin only (auth.ADMIN_PATHS): the owner reset this account's limits by hand. Limits on it up to now stop
    counting for enforcement and display; a newer limit counts again."""
    if body.account not in known_accounts():
        raise HTTPException(404, f"unknown account {body.account}")
    now = time.time()
    actor = getattr(request.state, "actor", None) or "local"
    with db() as c:
        c.execute("INSERT INTO limit_resets (account, at, actor) VALUES (?,?,?)", (body.account, now, actor))
    emit(None, "budget.limit_reset", {"account": body.account, "by": actor})
    return {"account": body.account, "at": now}


# ---------- §12.1 heartbeats ----------

HEARTBEAT_MAX_BYTES = 64 * 1024
HEARTBEAT_SKEW = 60
HEARTBEAT_MIN_GAP = 2.0
HEARTBEAT_FAIL_LIMIT, HEARTBEAT_FAIL_WINDOW = 30, 60.0
REFUSED = {"detail": "heartbeat refused"}


def _refuse(source: str, now: float) -> JSONResponse:
    if not _rate(HEARTBEAT_FAILURES[source], HEARTBEAT_FAIL_LIMIT, HEARTBEAT_FAIL_WINDOW, now):
        return JSONResponse(REFUSED, status_code=429)
    return JSONResponse(REFUSED, status_code=401)


@router.post("/api/nodes/heartbeat")
async def heartbeat(request: Request):
    """A runner's signed push of its cached /health (12.1). The signature (HMAC of the node's runner token) is the
    only auth: no user token, no API scope. Every refusal is the same 401."""
    now = time.time()
    source = request.client.host if request.client else "?"
    # Header shape first (no body, no DB): a source over its failure limit sending junk is cut off right here.
    node_id = request.headers.get("x-fleet-node") or ""
    sig = (request.headers.get("x-fleet-sig") or "").lower()
    try:
        ts = int(request.headers.get("x-fleet-ts") or "")
    except ValueError:
        return _refuse(source, now)
    if not tasks.NODE_ID_RE.fullmatch(node_id) or not re.fullmatch(r"[0-9a-f]{64}", sig):
        return _refuse(source, now)
    try:
        length = int(request.headers.get("content-length") or 0)
    except ValueError:
        length = HEARTBEAT_MAX_BYTES + 1
    if length > HEARTBEAT_MAX_BYTES:  # an early refusal only: the stream below enforces the cap
        return JSONResponse({"detail": "heartbeat too large"}, status_code=413)
    buffer = bytearray()
    async for part in request.stream():  # never buffer more than the cap, whatever Content-Length said (chunked too)
        if len(part) > HEARTBEAT_MAX_BYTES - len(buffer):  # checked before extending: a huge chunk is never held
            return JSONResponse({"detail": "heartbeat too large"}, status_code=413)
        buffer += part
    raw = bytes(buffer)
    with db() as c:
        row = c.execute("SELECT status, heartbeat_ts, runner FROM nodes WHERE id=?", (node_id,)).fetchone()
    # An unknown or unapproved node is refused before any key is looked up (no env-token fallback for strangers).
    if not row or row["status"] != "approved":
        return _refuse(source, now)
    key = tasks.node_runner_token(node_id)
    if not key:
        return _refuse(source, now)
    message = f"{node_id}\n{ts}\n{hashlib.sha256(raw).hexdigest()}".encode()
    expected = hmac.new(key.encode(), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return _refuse(source, now)
    if abs(now - ts) > HEARTBEAT_SKEW or (row["heartbeat_ts"] is not None and ts <= row["heartbeat_ts"]):
        return _refuse(source, now)
    with db() as c:  # a validly signed post always advances the replay mark, even when rate-limited
        moved = c.execute("UPDATE nodes SET heartbeat_ts=? WHERE id=? AND (heartbeat_ts IS NULL OR heartbeat_ts<?)",
                          (ts, node_id, ts)).rowcount
    if not moved:
        return _refuse(source, now)
    if now - HEARTBEAT_ACCEPTED.get(node_id, 0) < HEARTBEAT_MIN_GAP:
        return JSONResponse({"detail": "too many heartbeats"}, status_code=429)
    try:
        body = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"detail": "heartbeat body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"detail": "heartbeat body must be an object"}, status_code=400)
    HEARTBEAT_ACCEPTED[node_id] = now
    stored = _json(row["runner"]) or {}
    health = {**stored, **tasks.clean_health(body)}  # what a poll could report, nothing more
    with db() as c:
        # Never resets runner_fails: a push proves runner-to-controller only (12.1).
        c.execute("UPDATE nodes SET runner=?, heartbeat_at=?, runner_age_s=? WHERE id=?",
                  (json.dumps(health), now, tasks.health_age(body), node_id))
    return {"ok": True}


# ---------- §12.2 the sweep: liveness transitions, machine warnings, failover ----------

def _transition(key, active: bool, seed: bool) -> bool:
    """True when this is a change worth an event (never on the first observation of a liveness state)."""
    prev = LIVENESS.get(key) if isinstance(key, str) else WARNINGS.get(key)
    if isinstance(key, str):
        LIVENESS[key] = active
    else:
        WARNINGS[key] = active
    if seed:
        return False
    if prev is None:
        return active if not isinstance(key, str) else False
    return prev != active


async def sweep_once(now: float | None = None) -> None:
    now = time.time() if now is None else now
    grace = tasks.in_startup_grace(now)
    for source in [s for s, bucket in HEARTBEAT_FAILURES.items()
                   if not bucket or bucket[-1] <= now - HEARTBEAT_FAIL_WINDOW]:
        del HEARTBEAT_FAILURES[source]  # an idle source's bucket is dropped, so the map can't grow without bound
    nodes = tasks.node_rows()
    for node in nodes:
        if node["status"] != "approved":
            continue
        reason = tasks.runner_offline(node, now, grace)
        if _transition(node["id"], bool(reason), grace):
            kind = "node.runner_offline" if reason else "node.runner_online"
            emit(None, kind, {"node": node["id"], "name": node.get("name"), "reason": reason})
        machine = machine_row(node, now)
        for warning, kind in (("swap", "node.swap_high"), ("disk", "node.disk_low")):
            if machine["unknown"]:
                continue  # stale or missing metrics say nothing: no transition either way
            active = warning in machine["warnings"]
            if _transition((node["id"], kind), active, False):
                emit(None, kind, {"node": node["id"], "name": node.get("name"),
                                  "state": "started" if active else "cleared"})
    if grace:
        return
    # Level-triggered failover (12.6): every pass, each placed queued task on a node that is OFFLINE now.
    offline = {n["id"] for n in nodes if n["status"] == "approved" and tasks.runner_offline(n, now, False)}
    if not offline:
        return
    marks = ",".join("?" * len(offline))
    with db() as c:
        queued = [r[0] for r in c.execute(f"SELECT id FROM tasks WHERE state='queued' AND node_id IN ({marks})",
                                          tuple(offline))]
    for task_id in queued:
        try:
            replace(tasks.get_task(task_id), now)
        except Exception as exc:  # one task must not stop the others
            print(f"re-placing {task_id} failed: {exc!r}")


def pinned(task: dict) -> bool:
    placement = task.get("placement")
    if not isinstance(placement, dict):
        return True  # queued before placements were recorded: waits, never moved
    extra = task.get("launch_extra") or {}
    return bool(placement.get("pinned") or task.get("review_of") or task.get("cwd")
                or extra.get("base_branch") or extra.get("source_job"))


def replace(task: dict, now: float) -> bool:
    """Move one placed, queued task off an OFFLINE node: pick_node again with its stored inputs and the same account,
    then one conditional update (12.6). False when it must wait where it is."""
    if task["state"] != "queued" or pinned(task):
        return False
    placement = task["placement"]
    allowed = None
    if task["agent"] in budget.PAID_AGENTS and task.get("account"):
        slots = [s for s in tasks.placement(task["agent"], None) if s["account"] == task["account"]]
        if not task.get("model"):  # no model named: only the fleet profile runs the fleet default
            slots = [s for s in slots if s["profile"] == "fleet"]
        allowed = {s["node"] for s in slots} - {task["node_id"]}
        if not allowed:
            return False
    try:
        node, effective, _ = tasks.pick_node(task["agent"], task.get("model"), task["mode"], None, task.get("gate"),
                                             placement.get("role"), placement.get("policy"),
                                             placement.get("skills") or [], bool(placement.get("escalation")),
                                             allowed=allowed)
    except HTTPException:
        return False
    if node["id"] == task["node_id"] or tasks.runner_offline(node, now, False):
        return False
    profile, label, account = task.get("profile"), task.get("account_label"), task.get("account")
    if task["agent"] in budget.PAID_AGENTS:
        slot = next((s for s in tasks.node_slots(node, task["agent"]) if tasks.usable(s)
                     and (account is None or s["account"] == account)
                     and (task.get("model") or s["profile"] == "fleet")), None)
        if slot is None:
            return False
        profile, label, account = slot["profile"], slot["label"], slot["account"]
    with db() as c:
        moved = c.execute(
            "UPDATE tasks SET node_id=?, policy=?, policy_hash=?, instructions=?, profile=?, account_label=?, "
            "account=?, updated_at=? WHERE id=? AND state='queued' AND node_id=?",
            (node["id"], json.dumps(effective["policy"]), effective["hash"], effective["instructions"], profile, label,
             account, now, task["id"], task["node_id"])).rowcount == 1
    if moved:
        emit(task["id"], "task.replaced", {"title": task["title"], "from": task["node_id"], "to": node["id"]})
    return moved


async def sweep_loop() -> None:
    while True:
        try:
            await sweep_once()
        except Exception as exc:  # keep sweeping whatever happens
            print(f"live sweep failed: {exc!r}")
        await asyncio.sleep(SWEEP_SECONDS)
