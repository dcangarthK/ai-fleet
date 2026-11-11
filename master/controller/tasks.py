"""Agent tasks: scheduling onto node runners, lifecycle, approvals, waiting, usage.

A task is one piece of delegated work for an agent (claude, codex) or a model (ollama) on some node.
The controller decides where it runs and what state it is in; the node's runner owns the process.
See docs/SPEC-agent-tasks.md for the lifecycle and permission modes.

State changes are compare-and-swap (they only apply if the task is still in the expected state) and
everything that also touches a runner holds the task's lock, so approvals, messages, cancels and the
poller can't overwrite each other.
"""
import asyncio
import datetime
import json
import os
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from events import changed, emit, events_after, last_event_id, snapshot
import policy as fleet_policy
import skills as fleet_skills
from lineage import (  # noqa: F401  (re-exported: the lineage helpers are part of this module's surface)
    CHAIN_LINKS, FAILURE_STATES, LINKS, OUTCOME_KINDS, RESOLUTION_KINDS, clock, display_status, failure_reason,
    parse_reset, resolution_targets, tail, valid_commit,
)
from store import db, task_root

RUNNER_PORT = 11436
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
FLEET_DIR = os.environ.get("FLEET_DIR")
LOCAL_RUNNER = os.environ.get("FLEET_LOCAL_RUNNER", f"http://127.0.0.1:{RUNNER_PORT}").rstrip("/")
MASTER_ID = "master"
POLL_SECONDS = float(os.environ.get("FLEET_TASK_POLL_SECONDS", "3"))

MODES = ["read-only", "supervised", "auto-edit", "full"]
DEFAULT_MODE = "auto-edit"
NEEDS_APPROVAL = {"supervised", "full"}
AGENTS = {"claude", "codex", "ollama"}
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
ACTIVE = {"queued", "awaiting_approval", "starting", "working"}
SETTLED = {"review", "failed", "blocked", "interrupted", "done", "cancelled"}
RESUMABLE = {"review", "failed", "blocked", "interrupted"}

router = APIRouter()
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def rank(mode: str) -> int:
    return MODES.index(mode)


def local_minute() -> int:
    """Master-local minute of day; kept injectable for scheduler tests."""
    current = datetime.datetime.now().astimezone()
    return current.hour * 60 + current.minute


def effective_policy(node: str, role: str | None, agent: str, task_policy: dict | None = None,
                     skill_ids: list[str] | None = None) -> dict:
    """Load the newest applicable layers and compute the immutable task snapshot."""
    wanted = [("fleet", "*", "fleet"), ("node", node, f"node:{node}")]
    if role:
        wanted.append(("role", role, f"role:{role}"))
    wanted.append(("agent", agent, f"agent:{agent}"))
    layers = []
    with db() as c:
        for scope, scope_id, label in wanted:
            instruction = c.execute(
                "SELECT text FROM instructions WHERE scope=? AND scope_id=? ORDER BY version DESC LIMIT 1",
                (scope, scope_id)).fetchone()
            policy = c.execute(
                "SELECT body FROM policies WHERE scope=? AND scope_id=? ORDER BY version DESC LIMIT 1",
                (scope, scope_id)).fetchone()
            if instruction or policy:
                layers.append({"layer": label, "instructions": instruction["text"] if instruction else None,
                               "policy": json.loads(policy["body"]) if policy else {}})
    if task_policy is not None:
        fleet_policy.validate(task_policy)
        layers.append({"layer": "task", "instructions": None, "policy": task_policy})
    effective = fleet_policy.merge(layers)
    return fleet_skills.add_to_effective(effective, fleet_skills.snapshot(node, role, agent, skill_ids))


# ---------- runner client ----------

def runner_url(node: dict) -> str:
    return LOCAL_RUNNER if node["id"] == MASTER_ID else f"http://{node['ip']}:{RUNNER_PORT}"


class RunnerError(HTTPException):
    pass


def node_runner_token(node: dict | str) -> str:
    node_id = node if isinstance(node, str) else node["id"]
    with db() as c:
        row = c.execute("SELECT runner_token FROM nodes WHERE id=?", (node_id,)).fetchone()
    if row and row["runner_token"]:
        return row["runner_token"]
    if RUNNER_TOKEN:
        return RUNNER_TOKEN
    if node_id == MASTER_ID:
        token_path = Path(FLEET_DIR).expanduser() / "runner.token" if FLEET_DIR else Path.home() / ".ai-fleet/runner.token"
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


async def runner_call(node: dict, method: str, path: str, body: dict | None = None, timeout: float = 20) -> dict:
    token = node_runner_token(node)
    headers = {"X-Runner-Token": token} if token else {}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            r = await client.request(method, runner_url(node) + path, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise RunnerError(502, f"runner unreachable: {e.__class__.__name__}")
    if r.is_error:
        detail = r.json().get("detail", r.text) if r.content else r.reason_phrase
        raise RunnerError(502 if r.status_code >= 500 else r.status_code, f"runner: {detail}")
    return r.json()


async def runner_health(client: httpx.AsyncClient, base: str, node: dict | str = MASTER_ID) -> dict | None:
    token = node_runner_token(node)
    headers = {"X-Runner-Token": token} if token else {}
    try:
        r = await client.get(f"{base}/health", timeout=3, headers=headers)
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, dict) and body.get("ok") else None
    except (httpx.HTTPError, ValueError):
        return None


# ---------- scheduling ----------

def node_rows() -> list[dict]:
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM nodes")]
        running = dict(c.execute(
            "SELECT node_id, COUNT(*) FROM tasks WHERE state IN ('starting','working') GROUP BY node_id").fetchall())
    for n in rows:
        n.pop("runner_token", None)
        n["runner"] = json.loads(n["runner"]) if n.get("runner") else None
        n["models"] = json.loads(n["models"] or "[]")
        n["inventory"] = json.loads(n["inventory"]) if n.get("inventory") else None
        n["running_tasks"] = running.get(n["id"], 0)
    return rows


def ceiling(node: dict) -> str:
    """Effective mode ceiling: the stricter of the dashboard setting and the node's own runner config."""
    runner_max = (node["runner"] or {}).get("max_mode", "read-only")
    mine = node.get("ceiling") or DEFAULT_MODE
    return min(mine, runner_max if runner_max in MODES else "read-only", key=rank)


def eligible(node: dict, agent: str, model: str | None, mode: str, gate: str | None = None,
             policy: dict | None = None, escalation: bool = False) -> str | None:
    """None if the node can take the task, else the reason it can't.

    ``escalation`` marks a grounded-ask escalation task. It only ever adds a requirement: the agent must still be
    in the merged (fleet, node, role, agent, task) ``escalation.agents`` allow-list. Default is none.
    """
    if node["status"] != "approved":
        return "not approved"
    if not node["runner"] or (node.get("runner_fails") or 0) >= 3:
        return "runner offline"
    if not (node["runner"].get("agents") or {}).get(agent):
        return f"no {agent} on this node"
    power = node["runner"].get("power") or {}
    if (agent in ("claude", "codex") and power.get("on_battery") is True and power.get("ok") is False):
        pct = power.get("battery_pct")
        return f"on battery ({pct}%)" if isinstance(pct, int) else "on battery"
    if agent == "ollama" and model not in node["models"]:
        return f"model {model} not on this node"
    runner = node["runner"]
    if (gate and str(runner.get("version")) == "2"
            and gate not in (runner.get("gate_profiles") or []) and not runner.get("allow_raw_gates")):
        return f"gate profile {gate} not on this node"
    if rank(mode) > rank(ceiling(node)):
        return f"mode {mode} is above this node's ceiling ({ceiling(node)})"
    policy_ceiling = (policy or {}).get("mode_ceiling")
    if policy_ceiling and rank(mode) > rank(policy_ceiling):
        return f"mode {mode} is above the effective policy ceiling ({policy_ceiling})"
    if escalation and agent not in ((policy or {}).get("escalation") or {}).get("agents", []):
        return f"node policy does not allow escalation to {agent}"
    return None


def ram(node: dict) -> int:
    return ((node["inventory"] or {}).get("hardware") or {}).get("ram_gb") or 0


def temporary_reason(node: dict, policy: dict) -> str | None:
    """Why a hard-eligible node cannot start this snapshot right now."""
    limits = policy.get("limits") or {}
    maximum = limits.get("max_concurrent")
    if maximum is not None and node["running_tasks"] >= maximum:
        return "max concurrent tasks reached"
    budget = limits.get("agent_minutes_per_day")
    if budget is not None:
        with db() as c:
            used = c.execute(
                "SELECT COALESCE(SUM(agent_seconds),0) FROM tasks WHERE node_id=? AND created_at>=?",
                (node["id"], time.time() - 86400)).fetchone()[0]
        if used >= budget * 60:
            return "daily agent budget used"
    if fleet_policy.in_quiet_hours(limits.get("quiet_hours") or [], local_minute()):
        return "quiet hours"
    return None


def stored_escalation_policy(node: str, role: str | None, agent: str, snapshot: dict, task_policy: dict | None) -> dict:
    """Policy used for the escalation check: a task's own layer may narrow escalation.agents but never grant it,
    so when the request carries a policy the check also needs the stored layers alone to opt in."""
    if task_policy is None:
        return snapshot["policy"]
    stored = effective_policy(node, role, agent, None)["policy"].get("escalation") or {}
    merged = snapshot["policy"].get("escalation") or {}
    allowed = [a for a in merged.get("agents", []) if a in stored.get("agents", [])]
    return snapshot["policy"] | {"escalation": {"agents": allowed}}


def _esc_view(node: dict, role, agent, snapshot, task_policy, escalation: bool) -> dict:
    return (stored_escalation_policy(node["id"], role, agent, snapshot, task_policy)
            if escalation else snapshot["policy"])


def pick_node(agent: str, model: str | None, mode: str, wanted: str | None, gate: str | None = None,
              role: str | None = None, task_policy: dict | None = None,
              skill_ids: list[str] | None = None, escalation: bool = False) -> tuple[dict, dict, str | None]:
    nodes = node_rows()
    if wanted:
        match = [n for n in nodes if n["id"] == wanted] or [n for n in nodes if n["name"] == wanted]
        if not match:
            raise HTTPException(404, f"unknown node {wanted}")
        if len(match) > 1:
            raise HTTPException(409, f"several nodes are named {wanted}; use an id: {[n['id'] for n in match]}")
        snapshot = effective_policy(match[0]["id"], role, agent, task_policy, skill_ids)
        why = eligible(match[0], agent, model, mode, gate,
                       _esc_view(match[0], role, agent, snapshot, task_policy, escalation), escalation)
        if why:
            raise HTTPException(409, f"{wanted}: {why}")
        return match[0], snapshot, temporary_reason(match[0], snapshot["policy"])
    candidates = [(n, effective_policy(n["id"], role, agent, task_policy, skill_ids)) for n in nodes]
    ok = [(n, snapshot) for n, snapshot in candidates
          if not eligible(n, agent, model, mode, gate, _esc_view(n, role, agent, snapshot, task_policy, escalation),
                          escalation)]
    if not ok:
        reasons = {n["name"]: eligible(n, agent, model, mode, gate,
                                       _esc_view(n, role, agent, snapshot, task_policy, escalation), escalation)
                   for n, snapshot in candidates}
        raise HTTPException(409, f"no node can run {agent} in {mode} mode: {reasons}")
    # Prefer a node that can start now, then least busy, then the bigger machine.
    node, snapshot = sorted(ok, key=lambda item: (
        temporary_reason(item[0], item[1]["policy"]) is not None,
        item[0]["running_tasks"], -ram(item[0])))[0]
    return node, snapshot, temporary_reason(node, snapshot["policy"])


# ---------- task helpers ----------

def get_task(task_id: str) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"unknown task {task_id}")
    return task_view(row)


def task_view(row) -> dict:
    t = dict(row)
    t["denials"] = json.loads(t["denials"] or "[]")
    t["approved_tools"] = json.loads(t.get("approved_tools") or "[]")
    t["gate_result"] = json.loads(t["gate_result"]) if t["gate_result"] else None
    t["policy"] = json.loads(t.get("policy") or "{}")
    t["skills"] = json.loads(t.get("skills") or "[]")
    t["resolution"] = json.loads(t["resolution"]) if t.get("resolution") else None
    t["outcome"] = json.loads(t["outcome"]) if t.get("outcome") else None
    t["hidden"] = bool(t.get("hidden"))
    derive_reason(t)
    t["display"] = display_status(t)
    return t


def derive_reason(t: dict) -> dict:
    """Read-path only: a failed task stored before lineage has no failure_reason; work it out from its text.
    The database is not rewritten."""
    if t.get("state") in FAILURE_STATES and not t.get("failure_reason"):
        t["failure_reason"] = failure_reason(t)
        if t["failure_reason"] == "limit" and t.get("resets_at") is None:
            ended = t.get("ended_at") or t.get("updated_at")
            if ended:
                t["resets_at"] = parse_reset(f"{tail(t.get('result'))}\n{tail(t.get('error'))}", ended)
    return t


def with_root(task: dict) -> dict:
    with db() as c:
        task["root_id"] = task_root(c, task["id"])
    return task


def approvable_tools(task: dict, tools) -> list[str]:
    """Return a stable tool list without anything the task's policy permanently denies."""
    denied = set((((task.get("policy") or {}).get("tools") or {}).get("claude_deny") or []))
    return sorted({tool for tool in tools if isinstance(tool, str) and tool and tool not in denied})


def denied_tool_names(task: dict) -> list[str]:
    return approvable_tools(task, (denial.get("tool_name") or denial.get("tool")
                                   for denial in task["denials"]))


def node_of(task: dict) -> dict:
    for n in node_rows():
        if n["id"] == task["node_id"]:
            return n
    raise HTTPException(409, f"task node {task['node_id']} is gone")


def update(task_id: str, **fields) -> None:
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), task_id))


def transition(task: dict, expected: set[str], state: str, **fields) -> bool:
    """Move the task to `state` only if it is still in one of `expected`. Returns whether it moved."""
    now = time.time()
    if state == "working":
        fields.setdefault("run_started_at", now)
    elif state in SETTLED:
        if task["state"] in ACTIVE:
            fields.setdefault("ended_at", now)
        if task.get("run_started_at") and "agent_seconds" not in fields:
            # Agent time counts only while a run is going, not while a human reviews.
            fields.update(close_run(task, now))
    if state in FAILURE_STATES:
        merged = {**task, **fields}
        reason = failure_reason(merged, None, state)
        fields["failure_reason"] = reason
        fields["resets_at"] = None
        if reason == "limit":
            text = f"{tail(merged.get('result'))}\n{tail(merged.get('error'))}"
            fields["resets_at"] = parse_reset(text, merged.get("ended_at") or now)
    else:
        fields.setdefault("failure_reason", None)
        fields.setdefault("resets_at", None)
    fields["updated_at"] = now
    cols = ", ".join(f"{k}=?" for k in fields)
    marks = ",".join("?" * len(expected))
    with db() as c:
        moved = c.execute(f"UPDATE tasks SET state=?, {cols} WHERE id=? AND state IN ({marks})",
                          (state, *fields.values(), task["id"], *expected)).rowcount == 1
    if moved and state != task["state"]:
        emit(task["id"], f"task.{state}", {"title": task["title"], "from": task["state"], "node": task["node_id"],
                                           "agent": task["agent"]})
    if moved and state in ("review", "done"):
        auto_resolve(task["id"])
    return moved


def lineage_rows(c, root_id: str) -> dict[str, dict]:
    """Every task in the work item rooted at `root_id` (the root and all its descendants), keyed by id."""
    items: dict[str, dict] = {}
    frontier = [root_id]
    while frontier:
        marks = ",".join("?" * len(frontier))
        rows = c.execute(f"SELECT id,parent_id,link,title,state,resolution FROM tasks WHERE id IN ({marks}) "
                         f"OR parent_id IN ({marks})", (*frontier, *frontier)).fetchall()
        frontier = []
        for row in rows:
            if row["id"] not in items:
                items[row["id"]] = dict(row)
                frontier.append(row["id"])
    return items


def auto_resolve(task_id: str) -> None:
    """A task just ended well: mark the earlier failed attempts it supersedes as resolved by it."""
    with db() as c:
        items = lineage_rows(c, task_root(c, task_id))
        success = items.get(task_id)
        targets = resolution_targets(success, items) if success else []
        blob = json.dumps({"kind": "resolved_by", "task_id": task_id, "title": success["title"] if success else None,
                           "at": time.time()})
        targets = [t for t in targets
                   if c.execute("UPDATE tasks SET resolution=? WHERE id=? AND resolution IS NULL", (blob, t)).rowcount]
    for target in targets:
        emit(target, "task.resolved", {"title": items[target]["title"], "kind": "resolved_by", "by_task": task_id})


def close_run(task: dict, end: float) -> dict:
    """Fields that add the current run's duration to agent_seconds and mark the run closed."""
    start = task.get("run_started_at")
    return {"agent_seconds": (task.get("agent_seconds") or 0) + max(0, end - start), "run_started_at": None}


def iso_epoch(value) -> float | None:
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def job_fields(job: dict) -> dict:
    return {"session_id": job.get("session_id"), "result": job.get("result"),
            "denials": json.dumps(job.get("denials") or []), "exit_code": job.get("exit_code"),
            "gate_result": json.dumps(job["gate"]) if job.get("gate") else None,
            "branch": job.get("branch"), "cwd": job.get("cwd"), "error": None}


async def start(task: dict) -> None:
    """Start (or adopt) the runner job. Caller holds the task lock and has moved it to 'starting'."""
    node = node_of(task)
    policy = task["policy"]
    runner_policy = {name: policy[name] for name in ("filesystem", "tools", "network") if name in policy}
    if "task_minutes" in (policy.get("limits") or {}):
        runner_policy["limits"] = {"task_minutes": policy["limits"]["task_minutes"]}
    body = {"id": task["id"], "agent": task["agent"], "prompt": task["prompt"], "mode": task["mode"],
            "repo": task["repo"], "worktree": bool(task["worktree"]), "cwd": None, "model": task["model"],
            "gate": task["gate"], "allowed_tools": [], "resume_session": None,
            "instructions": task["instructions"], "policy": runner_policy, "policy_hash": task["policy_hash"]}
    try:
        job = await runner_call(node, "POST", "/jobs", body)
    except RunnerError as e:
        if e.status_code == 409:  # the runner already has it (e.g. we crashed mid-start): adopt it
            job = await runner_call(node, "GET", f"/jobs/{task['id']}")
        elif e.status_code == 502:
            update(task["id"], error=str(e.detail))  # stays 'starting'; recover() retries it
            return
        else:
            transition(task, {"starting"}, "failed", error=f"could not start: {e.detail}")
            return
    transition(task, {"starting"}, "working", started_at=time.time(), runs=max(task["runs"] or 0, 1),
               **job_fields(job))


async def try_start(task: dict) -> bool:
    """Start one queued task if its stored snapshot currently admits it."""
    if task["state"] != "queued":
        return False
    node = node_of(task)
    why = eligible(node, task["agent"], task["model"], task["mode"], task["gate"], task["policy"])
    why = why or temporary_reason(node, task["policy"])
    if why:
        if task.get("error") != why:
            update(task["id"], error=why)
        return False
    if not transition(task, {"queued"}, "starting", error=None):
        return False
    await start(get_task(task["id"]))
    return True


async def recover() -> None:
    """After a controller restart: finish starting tasks that were mid-start or never started."""
    with db() as c:
        pending = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state IN ('queued','starting')")]

    async def one(task_id: str) -> None:
        async with _locks[task_id]:
            task = get_task(task_id)
            if task["state"] == "queued":
                await try_start(task)
                return
            if get_task(task_id)["state"] == "starting":
                await start(get_task(task_id))

    await asyncio.gather(*(one(i) for i in pending), return_exceptions=True)


# ---------- polling runners ----------

def outcome(task: dict, job: dict) -> str:
    """Map a runner job onto a task state."""
    state = job.get("state")
    if state == "running":
        return "working"
    if state == "cancelled":
        return "cancelled"
    if state == "lost":
        return "interrupted"
    if job.get("denials") and task["mode"] != "read-only":
        return "blocked"
    if job.get("exit_code") != 0:
        return "failed"
    if not task["gate"]:
        return "review"
    gate = (job.get("gate") or {}).get("state")
    if gate in ("pending", "running"):
        return "working"
    return "review" if gate == "passed" else "failed"  # a requested gate must actually pass


async def poll_task(task_id: str) -> None:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] != "working":
            return
        try:
            job = await runner_call(node_of(task), "GET", f"/jobs/{task_id}", timeout=5)
        except HTTPException as e:
            if not task["error"]:
                update(task_id, error=str(e.detail))
                emit(task_id, "task.unreachable", {"title": task["title"], "node": task["node_id"]})
            return
        new = outcome(task, job)
        fields = job_fields(job)
        agent_end = iso_epoch(job.get("ended_at"))
        if agent_end and task.get("run_started_at"):
            fields.update(close_run(task, agent_end))  # the gate's run time isn't agent time
        if new == "working":
            update(task_id, **fields)
        elif transition(task, {"working"}, new, **fields) and new == "blocked":
            await auto_approve_denials(get_task(task_id))


AUTO_APPROVE_ROUNDS = 3


async def auto_approve_denials(task: dict) -> None:
    """If the task's policy pre-approves denied tools for its agent, resume with them (a few rounds at most)."""
    auto = (task.get("policy") or {}).get("auto_approve") or {}
    if not (auto.get("denied_tools") and task["agent"] in (auto.get("agents") or [])):
        return
    tools = denied_tool_names(task)
    if not tools:
        return
    if (task.get("auto_approvals") or 0) >= AUTO_APPROVE_ROUNDS:
        emit(task["id"], "task.auto_approve_exhausted", {"title": task["title"], "rounds": AUTO_APPROVE_ROUNDS})
        return
    update(task["id"], auto_approvals=(task.get("auto_approvals") or 0) + 1)
    emit(task["id"], "task.auto_approved", {"title": task["title"], "tools": tools, "agent": task["agent"]})
    await resume(get_task(task["id"]), "Policy pre-approves the tools you were denied: " + ", ".join(tools)
                 + ". Continue the task.", tools)


async def poll_loop() -> None:
    recovering: asyncio.Task | None = None
    while True:
        try:
            # Recovery retries can wait on unreachable runners; run them beside polling, one round at a time.
            if recovering is None or recovering.done():
                recovering = asyncio.create_task(recover())
            with db() as c:
                ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state='working'")]
                finished = {r[0] for r in c.execute("SELECT id FROM tasks WHERE state IN ('done','cancelled')")}
            await asyncio.gather(*(poll_task(i) for i in ids), return_exceptions=True)
            for task_id in [i for i in list(_locks) if i in finished and not _locks[i].locked()]:
                _locks.pop(task_id, None)
        except Exception as e:  # keep polling whatever happens
            print(f"task poll failed: {e!r}")
        await asyncio.sleep(POLL_SECONDS)


# ---------- API ----------

class TaskIn(BaseModel):
    agent: str
    prompt: str
    title: str | None = None
    model: str | None = None
    mode: str = DEFAULT_MODE
    repo: str | None = None
    worktree: bool = True
    gate: str | None = None
    node: str | None = None
    parent_id: str | None = None
    link: str | None = None  # why this task exists relative to parent_id: retry, resume, review, fix, merge, check
    role: str | None = None
    policy: dict | None = None
    skills: list[str] = Field(default_factory=list)
    escalation: bool = False  # grounded-ask escalation: node selection also requires escalation.agents to allow it
    trusted_bypass: bool = False  # Claude/Codex: run in full mode without permission prompts

    @field_validator("prompt")
    @classmethod
    def prompt_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt is empty")
        return v


class MessageIn(BaseModel):
    text: str


class ModeIn(BaseModel):
    mode: str


@router.get("/api/tasks")
def list_tasks(state: str | None = None, parent_id: str | None = None, limit: int = 200) -> list[dict]:
    q, args = "SELECT * FROM tasks WHERE 1=1", []
    if state:
        q += f" AND state IN ({','.join('?' * len(state.split(',')))})"
        args += state.split(",")
    if parent_id:
        q += " AND parent_id=?"
        args.append(parent_id)
    with db() as c:
        views = [task_view(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*args, limit))]
        parents = {r["id"]: r["parent_id"] for r in c.execute("SELECT id,parent_id FROM tasks")}
    for view in views:
        view["root_id"] = root_in(parents, view["id"])
    return views


def root_in(parents: dict, task_id: str) -> str:
    seen = {task_id}
    while parents.get(task_id) in parents and parents[task_id] not in seen:
        task_id = parents[task_id]
        seen.add(task_id)
    return task_id


@router.post("/api/tasks")
async def create_task(t: TaskIn) -> dict:
    if t.agent not in AGENTS:
        raise HTTPException(400, f"agent must be one of {sorted(AGENTS)}")
    if t.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    if t.agent == "ollama" and not t.model:
        raise HTTPException(400, "ollama tasks need a model")
    if t.trusted_bypass:
        if t.agent not in ("claude", "codex"):
            raise HTTPException(400, "skipping permission prompts is only for Claude and Codex tasks")
        t.mode = "full"
    if t.gate and rank(t.mode) < rank("auto-edit"):
        raise HTTPException(400, "a gate runs shell commands, so it needs auto-edit or full mode")
    if t.link is not None and t.link not in LINKS:
        raise HTTPException(400, f"link must be one of {list(LINKS)}")
    if t.link and t.link != "check" and not t.parent_id:
        raise HTTPException(400, "a link needs a parent_id (only 'check' may stand alone)")
    if t.parent_id:
        with db() as c:
            if not c.execute("SELECT 1 FROM tasks WHERE id=?", (t.parent_id,)).fetchone():
                raise HTTPException(400, f"unknown parent task {t.parent_id}")
    if t.role is not None and not ROLE_RE.fullmatch(t.role):
        raise HTTPException(400, "role must be lowercase letters, digits, dashes")
    if t.policy is not None and "auto_approve" in t.policy:
        # Approval authority comes only from the stored (admin-only) policy layers, never from the request.
        raise HTTPException(403, "a task can't grant itself auto-approval; set it on the Policies page")
    try:
        if t.policy is not None:
            fleet_policy.validate(t.policy)
        node, effective, queued_reason = pick_node(t.agent, t.model, t.mode, t.node, t.gate, t.role, t.policy,
                                                   t.skills, t.escalation)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    task_id = "t-" + secrets.token_hex(4)
    now = time.time()
    title = (t.title or t.prompt.strip().splitlines()[0])[:80]
    auto = (effective["policy"].get("auto_approve") or {})
    pre_approved = t.agent in (auto.get("agents") or []) and bool(auto.get("full_mode"))
    if t.trusted_bypass and not pre_approved:
        raise HTTPException(403, f"policy doesn't let {t.agent} skip permission prompts here; turn on auto-approval "
                                 "for it on the Policies page (and the node must allow full mode)")
    state = "awaiting_approval" if t.mode in NEEDS_APPROVAL and not pre_approved else "queued"
    with db() as c:
        c.execute("""INSERT INTO tasks (id, parent_id, title, agent, model, prompt, mode, repo, worktree, gate,
                     node_id, state, created_at, updated_at, instructions, policy, policy_hash, role, error, skills,
                     trusted_bypass, link, hidden)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, t.parent_id, title, t.agent, t.model, t.prompt, t.mode, t.repo, int(t.worktree),
                   t.gate, node["id"], state, now, now, effective["instructions"], json.dumps(effective["policy"]),
                   effective["hash"], t.role, queued_reason if state == "queued" else None,
                   json.dumps(effective["skills"]), int(t.trusted_bypass), t.link, int(t.link == "check")))
        if t.model:
            c.execute("UPDATE node_models SET state='ready',reason=NULL,updated_at=? WHERE node=? AND catalog_id IN "
                      "(SELECT id FROM model_catalog WHERE reference=?)",
                      (now, node["id"], t.model))
        for skill_id in dict.fromkeys(t.skills):
            c.execute("INSERT OR IGNORE INTO skill_bindings VALUES (?, 'task', ?)", (skill_id, task_id))
    emit(task_id, f"task.{state}", {"title": title, "node": node["id"], "agent": t.agent, "mode": t.mode})
    if state == "queued":
        async with _locks[task_id]:
            await try_start(get_task(task_id))
    return with_root(get_task(task_id))


@router.get("/api/tasks/wait")
async def wait(ids: str, timeout: float = 300, mode: str = "all") -> dict:
    """Long-poll until all (or any) of the tasks leave the active states, or the timeout passes."""
    wanted = [i for i in ids.split(",") if i]
    for i in wanted:
        get_task(i)
    deadline = time.monotonic() + min(max(timeout, 1), 1800)
    while True:
        since = snapshot()  # before reading state, so an event in between still wakes us
        tasks = [get_task(i) for i in wanted]
        n_settled = sum(t["state"] not in ACTIVE for t in tasks)
        done = n_settled > 0 if mode == "any" else n_settled == len(tasks)
        if done or time.monotonic() >= deadline:
            return {"timed_out": not done,
                    "tasks": [{k: t[k] for k in ("id", "title", "state", "agent", "node_id", "exit_code", "error")}
                              | {"result": (t["result"] or "")[:2000]} for t in tasks]}
        await changed(since, min(30, deadline - time.monotonic()))


@router.get("/api/tasks/{task_id}")
def read_task(task_id: str) -> dict:
    return with_root(get_task(task_id))


@router.get("/api/tasks/{task_id}/log")
async def task_log(task_id: str, offset: int = 0) -> dict:
    task = get_task(task_id)
    if task["state"] in ("queued", "awaiting_approval", "starting"):
        return {"offset": 0, "text": ""}
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/log?offset={offset}")


@router.get("/api/tasks/{task_id}/diff")
async def task_diff(task_id: str) -> dict:
    task = get_task(task_id)
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/diff")


async def resume(task: dict, text: str, allowed_tools: list[str], *, reset_auto_approvals: bool = False) -> None:
    """Caller holds the task lock."""
    tools = approvable_tools(task, [*(task.get("approved_tools") or []), *allowed_tools])
    if tools != task.get("approved_tools"):
        update(task["id"], approved_tools=json.dumps(tools))
    await runner_call(node_of(task), "POST", f"/jobs/{task['id']}/message",
                      {"text": text, "allowed_tools": tools, "mode": None})
    fields = {"runs": (task["runs"] or 0) + 1, "ended_at": None, "denials": "[]", "error": None,
              "failure_reason": None, "resets_at": None, "resolution": None}
    if reset_auto_approvals:
        fields["auto_approvals"] = 0
    if transition(task, RESUMABLE, "working", **fields):
        # The earlier run's story lives in the event log: how it ended, shown without any later resolution.
        emit(task["id"], "task.resumed", {
            "title": task["title"], "run": fields["runs"], "from": task["state"], "ended_at": task.get("ended_at"),
            "failure_reason": task.get("failure_reason"), "display": display_status({**task, "resolution": None})})


@router.post("/api/tasks/{task_id}/message")
async def message(task_id: str, m: MessageIn) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] not in RESUMABLE:
            raise HTTPException(409, f"can't message a task that is {task['state']}")
        await resume(task, m.text, [], reset_auto_approvals=True)
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/approve")
async def approve(task_id: str) -> dict:
    """User-only (dashboard): start an awaiting task, or grant a blocked task the tools it was denied."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] == "awaiting_approval":
            if transition(task, {"awaiting_approval"}, "queued", ended_at=None, auto_approvals=0):
                emit(task_id, "task.approved", {"title": task["title"]})
                await try_start(get_task(task_id))
        elif task["state"] == "blocked":
            tools = denied_tool_names(task)
            emit(task_id, "task.approved", {"title": task["title"], "tools": tools})
            await resume(task, "The user approved the tools you were denied: " + ", ".join(tools)
                         + ". Continue the task.", tools, reset_auto_approvals=True)
        else:
            raise HTTPException(409, f"nothing to approve: task is {task['state']}")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/reject")
async def reject(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if not transition(task, {"awaiting_approval", "blocked"}, "cancelled", error="rejected by user"):
            raise HTTPException(409, f"nothing to reject: task is {task['state']}")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/cancel")
async def cancel(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] in ("done", "cancelled"):
            return task
        if task["state"] in ("starting", "working"):
            try:
                await runner_call(node_of(task), "POST", f"/jobs/{task_id}/cancel")
            except HTTPException as e:
                if e.status_code != 404:
                    raise
        transition(task, ACTIVE | RESUMABLE, "cancelled")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/complete")
async def complete(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if not transition(task, RESUMABLE, "done"):
            raise HTTPException(409, f"can't complete a task that is {task['state']}")
    return get_task(task_id)


RESOLVABLE = {"failed", "interrupted", "blocked", "cancelled", "review"}


class ResolveIn(BaseModel):
    kind: str
    commit: str | None = None
    by: str | None = None
    note: str | None = None


class OutcomeIn(BaseModel):
    kind: str
    commit: str | None = None


@router.post("/api/tasks/{task_id}/resolve")
async def resolve(task_id: str, body: ResolveIn, request: Request) -> dict:
    """Admin only: record that a settled task no longer needs attention. Never changes the task's state,
    so a blocked task stays blocked (it only stops being listed as needing attention)."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if body.kind not in ("dismissed", "done_elsewhere"):
            raise HTTPException(400, "kind must be dismissed or done_elsewhere")
        by = (body.by or "").strip()[:120]
        commit = (body.commit or "").strip()
        if commit and not valid_commit(commit):
            raise HTTPException(400, "commit must be 7 to 40 hex characters")
        if body.kind == "done_elsewhere" and not by:
            raise HTTPException(400, "done_elsewhere needs 'by' (who finished it)")
        if task["state"] not in RESOLVABLE:
            raise HTTPException(409, f"can't resolve a task that is {task['state']}")
        resolution = {"kind": body.kind, "at": time.time()}
        if by:
            resolution["by"] = by
        if commit:
            resolution["commit"] = commit
        if body.note and body.note.strip():
            resolution["note"] = body.note.strip()[:500]
        update(task_id, resolution=json.dumps(resolution))
    emit(task_id, "task.resolved", {"title": task["title"], "kind": body.kind, "by": by or None,
                                    "commit": commit or None, "actor": getattr(request.state, "actor", None)})
    return with_root(get_task(task_id))


@router.post("/api/tasks/{task_id}/outcome")
async def set_outcome(task_id: str, body: OutcomeIn, request: Request) -> dict:
    """Admin only: record how a work item ended. Only on a root task. Marks a settled root done for merged/done,
    but never moves a blocked or awaiting-approval task."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["parent_id"]:
            raise HTTPException(400, "an outcome belongs on the root task of the work item")
        if body.kind not in OUTCOME_KINDS:
            raise HTTPException(400, f"kind must be one of {list(OUTCOME_KINDS)}")
        commit = (body.commit or "").strip()
        if (body.kind == "merged" or commit) and not valid_commit(commit):
            raise HTTPException(400, "commit must be 7 to 40 hex characters" + (" (required for merged)" if body.kind == "merged" else ""))
        outcome_value = {"kind": body.kind, "at": time.time()}
        if commit:
            outcome_value["commit"] = commit
        update(task_id, outcome=json.dumps(outcome_value))
        if body.kind in ("merged", "done"):
            transition(task, {"review", "failed", "interrupted"}, "done")
    emit(task_id, "task.outcome", {"title": task["title"], "kind": body.kind, "commit": commit or None,
                                   "actor": getattr(request.state, "actor", None)})
    return with_root(get_task(task_id))


WORK_COLUMNS = ("id,parent_id,link,title,agent,model,state,failure_reason,resets_at,resolution,outcome,hidden,"
                "created_at,updated_at,started_at,ended_at,runs,gate,gate_result,exit_code,error,"
                # The end of the output is where a usage-limit message sits (same slice derive_reason uses).
                "CASE WHEN failure_reason IS NULL AND state IN ('failed','blocked','interrupted','cancelled') "
                "THEN substr(result,-4000) END AS result")


def outcome_display(value: dict) -> dict:
    kind = value.get("kind")
    if kind == "merged":
        return {"tone": "ok", "label": f"Merged {(value.get('commit') or '')[:7]}".strip()}
    if kind == "done":
        return {"tone": "ok", "label": "Done"}
    return {"tone": "muted", "label": "Abandoned"}


def attempt_entries(row: dict, resumed: list[dict]) -> list[dict]:
    """One entry per run of a task: earlier runs come from task.resumed events, the last from the row."""
    def entry(**kw) -> dict:
        return {"id": row["id"], "title": row["title"], "agent": row["agent"], "model": row["model"],
                "link": row["link"], "created_at": row["created_at"]} | kw

    at = row["created_at"]
    out, started = [], row["started_at"] or at
    for number, event in enumerate(resumed, start=1):
        data = event["data"]
        out.append(entry(state=data.get("from"), display=data.get("display") or {"tone": "muted", "label": str(data.get("from"))},
                         failure_reason=data.get("failure_reason"), started_at=started, ended_at=data.get("ended_at"),
                         run=number, at=at))
        at = started = event["ts"]
    out.append(entry(state=row["state"], display=display_status(row), failure_reason=row["failure_reason"],
                     started_at=started if resumed else row["started_at"], ended_at=row["ended_at"],
                     run=len(resumed) + 1, at=at))
    return out


@router.get("/api/work")
def work_items(limit: int = 50, include_hidden: int = 0) -> list[dict]:
    """One entry per root task (a piece of work), newest activity first, with every attempt in order."""
    limit = max(1, min(limit, 500))
    # Every task with the root of its work item. An orphan (parent missing) is its own root.
    tree = ("WITH RECURSIVE tree(id, root) AS ("
            "SELECT id, id FROM tasks WHERE parent_id IS NULL OR parent_id NOT IN (SELECT id FROM tasks) "
            "UNION ALL SELECT t.id, tree.root FROM tasks t JOIN tree ON t.parent_id = tree.id)")
    with db() as c:
        # Newest `limit` work items by latest activity across the whole item (an outcome bumps updated_at too).
        hidden = "" if include_hidden else "WHERE r.hidden = 0 "
        roots = [r[0] for r in c.execute(
            f"{tree} SELECT tree.root FROM tree JOIN tasks t ON t.id = tree.id JOIN tasks r ON r.id = tree.root "
            f"{hidden}GROUP BY tree.root ORDER BY MAX(t.updated_at) DESC, tree.root LIMIT ?", (limit,))]
        if not roots:
            return []
        marks = ",".join("?" * len(roots))
        scoped = (f"WITH RECURSIVE tree(id, root) AS (SELECT id, id FROM tasks WHERE id IN ({marks}) "
                  "UNION ALL SELECT t.id, tree.root FROM tasks t JOIN tree ON t.parent_id = tree.id)")
        rows = [dict(r) for r in c.execute(
            f"{scoped} SELECT {WORK_COLUMNS} FROM tasks WHERE id IN (SELECT id FROM tree) ORDER BY created_at, rowid",
            roots)]
        resumed: dict[str, list[dict]] = defaultdict(list)
        for r in c.execute(f"{scoped} SELECT task_id, ts, data FROM events WHERE type='task.resumed' "
                           "AND task_id IN (SELECT id FROM tree) ORDER BY id", roots):
            resumed[r["task_id"]].append({"ts": r["ts"], "data": json.loads(r["data"] or "{}")})
    by_id = {r["id"]: r for r in rows}
    parents = {r["id"]: r["parent_id"] for r in rows}
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        r["resolution"] = json.loads(r["resolution"]) if r["resolution"] else None
        derive_reason(r)
        groups[root_in(parents, r["id"])].append(r)
    items = []
    for root_id, members in groups.items():
        root = by_id[root_id]
        if root["hidden"] and not include_hidden:
            continue
        attempts = []
        for index, member in enumerate(members):
            if member["hidden"] and not include_hidden:
                continue
            for part in attempt_entries(member, resumed.get(member["id"], [])):
                attempts.append((part["at"], index, part["run"], part))
        attempts.sort(key=lambda item: item[:3])
        shown = [{k: v for k, v in part.items() if k not in ("at", "created_at")} for *_, part in attempts]
        outcome_value = json.loads(root["outcome"]) if root["outcome"] else None
        display = outcome_display(outcome_value) if outcome_value else (shown[-1]["display"] if shown else display_status(root))
        activity = max([m["updated_at"] or 0 for m in members] + [(outcome_value or {}).get("at") or 0])
        items.append({"root_id": root_id, "title": root["title"], "outcome": outcome_value, "display": display,
                      "updated_at": activity, "attempts": shown})
    items.sort(key=lambda item: item["updated_at"], reverse=True)
    return items


@router.post("/api/tasks/{task_id}/cleanup")
async def cleanup(task_id: str) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] in ACTIVE:
            raise HTTPException(409, "task is still active")
        out = await runner_call(node_of(task), "POST", f"/jobs/{task_id}/cleanup")
    emit(task_id, "task.cleaned_up", {"title": task["title"]})
    return out


@router.get("/api/inbox")
def inbox() -> list[dict]:
    """Everything waiting on a human: approvals to start, and blocked tasks with denied permissions."""
    return list_tasks(state="awaiting_approval,blocked")


@router.post("/api/nodes/{node}/ceiling")
def set_ceiling(node: str, m: ModeIn) -> dict:
    if m.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    with db() as c:
        if c.execute("UPDATE nodes SET ceiling=? WHERE id=? OR name=?", (m.mode, node, node)).rowcount != 1:
            raise HTTPException(404, f"unknown or ambiguous node {node}")
    emit(None, "node.ceiling", {"node": node, "mode": m.mode})
    return {"node": node, "ceiling": m.mode}


@router.get("/api/usage")
def usage(days: int = 1) -> list[dict]:
    """Tasks and agent run time per node and agent (time waiting on humans isn't counted)."""
    now = time.time()
    with db() as c:
        rows = c.execute("""SELECT node_id, agent, COUNT(*) AS tasks,
                                   SUM(state='working') AS running,
                                   SUM(state IN ('failed','interrupted')) AS failed,
                                   CAST(SUM(COALESCE(agent_seconds, 0)
                                            + CASE WHEN state='working' THEN ? - COALESCE(run_started_at, ?) ELSE 0 END)
                                        AS INTEGER) AS seconds
                            FROM tasks WHERE created_at >= ? GROUP BY node_id, agent ORDER BY tasks DESC""",
                         (now, now, now - days * 86400)).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/events")
async def event_stream(request: Request, after: int | None = None) -> StreamingResponse:
    """Server-sent events: the dashboard listens here instead of polling."""
    cursor = last_event_id() if after is None else after

    async def gen():
        nonlocal cursor
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            since = snapshot()
            for e in events_after(cursor):
                cursor = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
            await changed(since, 15)
            yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/events/list")
def event_list(after: int = 0, limit: int = 200) -> list[dict]:
    return events_after(after, min(limit, 1000))
