"""Agent tasks: scheduling onto node runners, lifecycle, approvals, waiting, usage.

A task is one piece of delegated work for an agent (claude, codex) or a model (ollama) on some node.
The controller decides where it runs and what state it is in; the node's runner owns the process.
See docs/SPEC-agent-tasks.md for the lifecycle and permission modes.

State changes are compare-and-swap (they only apply if the task is still in the expected state) and
everything that also touches a runner holds the task's lock, so approvals, messages, cancels and the
poller can't overwrite each other.
"""
import asyncio
import json
import os
import secrets
import time
from collections import defaultdict

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from events import changed, emit, events_after, last_event_id, snapshot
from store import db

RUNNER_PORT = 11436
RUNNER_TOKEN = os.environ.get("RUNNER_TOKEN", "")
LOCAL_RUNNER = os.environ.get("FLEET_LOCAL_RUNNER", f"http://127.0.0.1:{RUNNER_PORT}").rstrip("/")
MASTER_ID = "master"
POLL_SECONDS = float(os.environ.get("FLEET_TASK_POLL_SECONDS", "3"))

MODES = ["read-only", "supervised", "auto-edit", "full"]
DEFAULT_MODE = "auto-edit"
NEEDS_APPROVAL = {"supervised", "full"}
AGENTS = {"claude", "codex", "ollama"}
ACTIVE = {"queued", "awaiting_approval", "starting", "working"}
SETTLED = {"review", "failed", "blocked", "interrupted", "done", "cancelled"}
RESUMABLE = {"review", "failed", "blocked", "interrupted"}

router = APIRouter()
_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def rank(mode: str) -> int:
    return MODES.index(mode)


# ---------- runner client ----------

def runner_url(node: dict) -> str:
    return LOCAL_RUNNER if node["id"] == MASTER_ID else f"http://{node['ip']}:{RUNNER_PORT}"


class RunnerError(HTTPException):
    pass


async def runner_call(node: dict, method: str, path: str, body: dict | None = None, timeout: float = 20) -> dict:
    headers = {"X-Runner-Token": RUNNER_TOKEN} if RUNNER_TOKEN else {}
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
            r = await client.request(method, runner_url(node) + path, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise RunnerError(502, f"runner unreachable: {e.__class__.__name__}")
    if r.is_error:
        detail = r.json().get("detail", r.text) if r.content else r.reason_phrase
        raise RunnerError(502 if r.status_code >= 500 else r.status_code, f"runner: {detail}")
    return r.json()


async def runner_health(client: httpx.AsyncClient, base: str) -> dict | None:
    headers = {"X-Runner-Token": RUNNER_TOKEN} if RUNNER_TOKEN else {}
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


def eligible(node: dict, agent: str, model: str | None, mode: str) -> str | None:
    """None if the node can take the task, else the reason it can't."""
    if node["status"] != "approved":
        return "not approved"
    if not node["runner"] or (node.get("runner_fails") or 0) >= 3:
        return "runner offline"
    if not (node["runner"].get("agents") or {}).get(agent):
        return f"no {agent} on this node"
    if agent == "ollama" and model not in node["models"]:
        return f"model {model} not on this node"
    if rank(mode) > rank(ceiling(node)):
        return f"mode {mode} is above this node's ceiling ({ceiling(node)})"
    return None


def ram(node: dict) -> int:
    return ((node["inventory"] or {}).get("hardware") or {}).get("ram_gb") or 0


def pick_node(agent: str, model: str | None, mode: str, wanted: str | None) -> dict:
    nodes = node_rows()
    if wanted:
        match = [n for n in nodes if n["id"] == wanted] or [n for n in nodes if n["name"] == wanted]
        if not match:
            raise HTTPException(404, f"unknown node {wanted}")
        if len(match) > 1:
            raise HTTPException(409, f"several nodes are named {wanted}; use an id: {[n['id'] for n in match]}")
        why = eligible(match[0], agent, model, mode)
        if why:
            raise HTTPException(409, f"{wanted}: {why}")
        return match[0]
    ok = [n for n in nodes if not eligible(n, agent, model, mode)]
    if not ok:
        reasons = {n["name"]: eligible(n, agent, model, mode) for n in nodes}
        raise HTTPException(409, f"no node can run {agent} in {mode} mode: {reasons}")
    # Least busy first, then the bigger machine.
    return sorted(ok, key=lambda n: (n["running_tasks"], -ram(n)))[0]


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
    t["gate_result"] = json.loads(t["gate_result"]) if t["gate_result"] else None
    return t


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
    elif task["state"] == "working" and state in SETTLED:
        # Agent time counts only while a run is going, not while a human reviews.
        fields["agent_seconds"] = (task.get("agent_seconds") or 0) + max(0, now - (task.get("run_started_at") or now))
        fields.setdefault("ended_at", now)
    fields["updated_at"] = now
    cols = ", ".join(f"{k}=?" for k in fields)
    marks = ",".join("?" * len(expected))
    with db() as c:
        moved = c.execute(f"UPDATE tasks SET state=?, {cols} WHERE id=? AND state IN ({marks})",
                          (state, *fields.values(), task["id"], *expected)).rowcount == 1
    if moved and state != task["state"]:
        emit(task["id"], f"task.{state}", {"title": task["title"], "from": task["state"], "node": task["node_id"],
                                           "agent": task["agent"]})
    return moved


def job_fields(job: dict) -> dict:
    return {"session_id": job.get("session_id"), "result": job.get("result"),
            "denials": json.dumps(job.get("denials") or []), "exit_code": job.get("exit_code"),
            "gate_result": json.dumps(job["gate"]) if job.get("gate") else None,
            "branch": job.get("branch"), "cwd": job.get("cwd"), "error": None}


async def start(task: dict) -> None:
    """Start (or adopt) the runner job. Caller holds the task lock and has moved it to 'starting'."""
    node = node_of(task)
    body = {"id": task["id"], "agent": task["agent"], "prompt": task["prompt"], "mode": task["mode"],
            "repo": task["repo"], "worktree": bool(task["worktree"]), "cwd": None, "model": task["model"],
            "gate": task["gate"], "allowed_tools": [], "resume_session": None}
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


async def recover() -> None:
    """After a controller restart: finish starting tasks that were mid-start or never started."""
    with db() as c:
        pending = [task_view(r) for r in c.execute("SELECT * FROM tasks WHERE state IN ('queued','starting')")]
    for task in pending:
        async with _locks[task["id"]]:
            task = get_task(task["id"])
            if task["state"] == "queued" and not transition(task, {"queued"}, "starting"):
                continue
            await start(get_task(task["id"]))


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
        if new == "working":
            update(task_id, **job_fields(job))
        else:
            transition(task, {"working"}, new, **job_fields(job))


async def poll_loop() -> None:
    await recover()
    while True:
        try:
            with db() as c:
                ids = [r[0] for r in c.execute("SELECT id FROM tasks WHERE state='working'")]
            await asyncio.gather(*(poll_task(i) for i in ids), return_exceptions=True)
            with db() as c:
                if c.execute("SELECT 1 FROM tasks WHERE state='starting' LIMIT 1").fetchone():
                    await recover()
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
        return [task_view(r) for r in c.execute(q + " ORDER BY created_at DESC LIMIT ?", (*args, limit))]


@router.post("/api/tasks")
async def create_task(t: TaskIn) -> dict:
    if t.agent not in AGENTS:
        raise HTTPException(400, f"agent must be one of {sorted(AGENTS)}")
    if t.mode not in MODES:
        raise HTTPException(400, f"mode must be one of {MODES}")
    if t.agent == "ollama" and not t.model:
        raise HTTPException(400, "ollama tasks need a model")
    if t.gate and rank(t.mode) < rank("auto-edit"):
        raise HTTPException(400, "a gate runs shell commands, so it needs auto-edit or full mode")
    if t.parent_id:
        get_task(t.parent_id)
    node = pick_node(t.agent, t.model, t.mode, t.node)
    task_id = "t-" + secrets.token_hex(4)
    now = time.time()
    title = (t.title or t.prompt.strip().splitlines()[0])[:80]
    state = "awaiting_approval" if t.mode in NEEDS_APPROVAL else "queued"
    with db() as c:
        c.execute("""INSERT INTO tasks (id, parent_id, title, agent, model, prompt, mode, repo, worktree, gate,
                     node_id, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (task_id, t.parent_id, title, t.agent, t.model, t.prompt, t.mode, t.repo, int(t.worktree),
                   t.gate, node["id"], state, now, now))
    emit(task_id, f"task.{state}", {"title": title, "node": node["id"], "agent": t.agent, "mode": t.mode})
    if state == "queued":
        async with _locks[task_id]:
            task = get_task(task_id)
            if transition(task, {"queued"}, "starting"):
                await start(get_task(task_id))
    return get_task(task_id)


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
    return get_task(task_id)


@router.get("/api/tasks/{task_id}/log")
async def task_log(task_id: str, offset: int = 0) -> dict:
    task = get_task(task_id)
    if task["state"] in ("queued", "awaiting_approval"):
        return {"offset": 0, "text": ""}
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/log?offset={offset}")


@router.get("/api/tasks/{task_id}/diff")
async def task_diff(task_id: str) -> dict:
    task = get_task(task_id)
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/diff")


async def resume(task: dict, text: str, allowed_tools: list[str]) -> None:
    """Caller holds the task lock."""
    await runner_call(node_of(task), "POST", f"/jobs/{task['id']}/message",
                      {"text": text, "allowed_tools": allowed_tools, "mode": None})
    transition(task, RESUMABLE, "working", runs=(task["runs"] or 0) + 1, ended_at=None, denials="[]", error=None)


@router.post("/api/tasks/{task_id}/message")
async def message(task_id: str, m: MessageIn) -> dict:
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] not in RESUMABLE:
            raise HTTPException(409, f"can't message a task that is {task['state']}")
        await resume(task, m.text, [])
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/approve")
async def approve(task_id: str) -> dict:
    """User-only (dashboard): start an awaiting task, or grant a blocked task the tools it was denied."""
    async with _locks[task_id]:
        task = get_task(task_id)
        if task["state"] == "awaiting_approval":
            if transition(task, {"awaiting_approval"}, "starting"):
                emit(task_id, "task.approved", {"title": task["title"]})
                await start(get_task(task_id))
        elif task["state"] == "blocked":
            tools = sorted({d.get("tool_name") or d.get("tool") for d in task["denials"]} - {None, ""})
            emit(task_id, "task.approved", {"title": task["title"], "tools": tools})
            await resume(task, "The user approved the tools you were denied: " + ", ".join(tools)
                         + ". Continue the task.", tools)
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
