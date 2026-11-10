"""Agent tasks: scheduling onto node runners, lifecycle, approvals, waiting, usage.

A task is one piece of delegated work for an agent (claude, codex) or a model (ollama) on some node.
The controller decides where it runs and what state it is in; the node's runner owns the process.
See docs/SPEC-agent-tasks.md for the lifecycle and permission modes.
"""
import asyncio
import json
import os
import secrets
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from events import changed, emit, events_after, last_event_id
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
ACTIVE = {"queued", "awaiting_approval", "working"}
SETTLED = {"review", "failed", "blocked", "interrupted", "done", "cancelled"}

router = APIRouter()


def rank(mode: str) -> int:
    return MODES.index(mode)


# ---------- runner client ----------

def runner_url(node: dict) -> str:
    return LOCAL_RUNNER if node["id"] == MASTER_ID else f"http://{node['ip']}:{RUNNER_PORT}"


async def runner_call(node: dict, method: str, path: str, body: dict | None = None, timeout: float = 20) -> dict:
    headers = {"X-Runner-Token": RUNNER_TOKEN} if RUNNER_TOKEN else {}
    async with httpx.AsyncClient(trust_env=False, timeout=timeout) as client:
        r = await client.request(method, runner_url(node) + path, json=body, headers=headers)
    if r.is_error:
        detail = r.json().get("detail", r.text) if r.content else r.reason_phrase
        raise HTTPException(502 if r.status_code >= 500 else r.status_code, f"runner: {detail}")
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
        running = dict(c.execute("SELECT node_id, COUNT(*) FROM tasks WHERE state='working' GROUP BY node_id").fetchall())
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
    if agent not in (node["runner"].get("agents") or {}):
        return f"no {agent} on this node"
    if agent == "ollama" and model and node["models"] and model not in node["models"]:
        return f"model {model} not on this node"
    if rank(mode) > rank(ceiling(node)):
        return f"mode {mode} is above this node's ceiling ({ceiling(node)})"
    return None


def ram(node: dict) -> int:
    return ((node["inventory"] or {}).get("hardware") or {}).get("ram_gb") or 0


def pick_node(agent: str, model: str | None, mode: str, wanted: str | None) -> dict:
    nodes = node_rows()
    if wanted:
        match = [n for n in nodes if wanted in (n["id"], n["name"])]
        if not match:
            raise HTTPException(404, f"unknown node {wanted}")
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


def set_state(task: dict, state: str, **fields) -> None:
    if state in SETTLED and task["state"] in ACTIVE:
        fields.setdefault("ended_at", time.time())
    update(task["id"], state=state, **fields)
    if state != task["state"]:
        emit(task["id"], f"task.{state}", {"title": task["title"], "from": task["state"], "node": task["node_id"],
                                           "agent": task["agent"]})


async def start(task: dict) -> None:
    node = node_of(task)
    body = {"id": task["id"], "agent": task["agent"], "prompt": task["prompt"], "mode": task["mode"],
            "repo": task["repo"], "worktree": bool(task["worktree"]), "cwd": None, "model": task["model"],
            "gate": task["gate"], "allowed_tools": [], "resume_session": None}
    try:
        job = await runner_call(node, "POST", "/jobs", body)
    except (HTTPException, httpx.HTTPError) as e:
        set_state(task, "failed", error=f"could not start: {getattr(e, 'detail', e)}")
        return
    set_state(task, "working", started_at=time.time(), runs=1, branch=job.get("branch"), cwd=job.get("cwd"))


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
    gate = job.get("gate") or {}
    if gate.get("state") in ("pending", "running"):
        return "working"
    return "failed" if gate.get("state") == "failed" else "review"


async def poll_task(task: dict) -> None:
    node = node_of(task)
    try:
        job = await runner_call(node, "GET", f"/jobs/{task['id']}", timeout=5)
    except (HTTPException, httpx.HTTPError) as e:
        if not task["error"]:
            update(task["id"], error=f"runner unreachable: {getattr(e, 'detail', e)}")
            emit(task["id"], "task.unreachable", {"title": task["title"], "node": task["node_id"]})
        return
    fields = {"session_id": job.get("session_id"), "result": job.get("result"),
              "denials": json.dumps(job.get("denials") or []), "exit_code": job.get("exit_code"),
              "gate_result": json.dumps(job["gate"]) if job.get("gate") else None,
              "branch": job.get("branch"), "cwd": job.get("cwd"), "error": None}
    new = outcome(task, job)
    if new == "working":
        update(task["id"], **fields)
    else:
        set_state(task, new, **fields)


async def poll_loop() -> None:
    while True:
        try:
            with db() as c:
                active = [task_view(r) for r in c.execute("SELECT * FROM tasks WHERE state='working'")]
            await asyncio.gather(*(poll_task(t) for t in active), return_exceptions=True)
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
        tasks = [get_task(i) for i in wanted]
        settled = [t for t in tasks if t["state"] not in ACTIVE]
        if (mode == "any" and settled) or len(settled) == len(tasks) or time.monotonic() >= deadline:
            return {"timed_out": time.monotonic() >= deadline and not settled,
                    "tasks": [{k: t[k] for k in ("id", "title", "state", "agent", "node_id", "exit_code", "error")}
                              | {"result": (t["result"] or "")[:2000]} for t in tasks]}
        await changed(min(30, deadline - time.monotonic()))


@router.get("/api/tasks/{task_id}")
def read_task(task_id: str) -> dict:
    return get_task(task_id)


@router.get("/api/tasks/{task_id}/log")
async def task_log(task_id: str, offset: int = 0) -> dict:
    task = get_task(task_id)
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/log?offset={offset}")


@router.get("/api/tasks/{task_id}/diff")
async def task_diff(task_id: str) -> dict:
    task = get_task(task_id)
    return await runner_call(node_of(task), "GET", f"/jobs/{task_id}/diff")


@router.post("/api/tasks/{task_id}/message")
async def message(task_id: str, m: MessageIn) -> dict:
    task = get_task(task_id)
    if task["state"] not in ("review", "failed", "blocked", "interrupted"):
        raise HTTPException(409, f"can't message a task that is {task['state']}")
    await resume(task, m.text, [])
    return get_task(task_id)


async def resume(task: dict, text: str, allowed_tools: list[str]) -> None:
    await runner_call(node_of(task), "POST", f"/jobs/{task['id']}/message",
                      {"text": text, "allowed_tools": allowed_tools, "mode": None})
    set_state(task, "working", runs=(task["runs"] or 0) + 1, ended_at=None, denials="[]")


@router.post("/api/tasks/{task_id}/approve")
async def approve(task_id: str) -> dict:
    """User-only (dashboard): start an awaiting task, or grant a blocked task the tools it was denied."""
    task = get_task(task_id)
    if task["state"] == "awaiting_approval":
        emit(task_id, "task.approved", {"title": task["title"]})
        await start(task)
    elif task["state"] == "blocked":
        tools = sorted({d.get("tool_name") or d.get("tool") for d in task["denials"]} - {None})
        emit(task_id, "task.approved", {"title": task["title"], "tools": tools})
        await resume(task, "The user approved the tools you were denied: " + ", ".join(tools)
                     + ". Continue the task.", tools)
    else:
        raise HTTPException(409, f"nothing to approve: task is {task['state']}")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/reject")
async def reject(task_id: str) -> dict:
    task = get_task(task_id)
    if task["state"] not in ("awaiting_approval", "blocked"):
        raise HTTPException(409, f"nothing to reject: task is {task['state']}")
    set_state(task, "cancelled", error="rejected by user")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/cancel")
async def cancel(task_id: str) -> dict:
    task = get_task(task_id)
    if task["state"] in ("done", "cancelled"):
        return task
    if task["state"] == "working":
        try:
            await runner_call(node_of(task), "POST", f"/jobs/{task_id}/cancel")
        except HTTPException as e:
            if e.status_code != 404:
                raise
    set_state(task, "cancelled")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/complete")
def complete(task_id: str) -> dict:
    task = get_task(task_id)
    if task["state"] not in ("review", "failed", "interrupted", "blocked"):
        raise HTTPException(409, f"can't complete a task that is {task['state']}")
    set_state(task, "done")
    return get_task(task_id)


@router.post("/api/tasks/{task_id}/cleanup")
async def cleanup(task_id: str) -> dict:
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
    """Tasks and agent time per node and agent, to spot overloaded machines and heavy subscriptions."""
    since = time.time() - days * 86400
    with db() as c:
        rows = c.execute("""SELECT node_id, agent, COUNT(*) AS tasks,
                                   SUM(state='working') AS running,
                                   SUM(state IN ('failed','interrupted')) AS failed,
                                   CAST(SUM(COALESCE(ended_at, ?) - COALESCE(started_at, ?)) AS INTEGER) AS seconds
                            FROM tasks WHERE created_at >= ? GROUP BY node_id, agent ORDER BY tasks DESC""",
                         (time.time(), time.time(), since)).fetchall()
    return [dict(r) for r in rows]


@router.get("/api/events")
async def event_stream(request: Request, after: int | None = None) -> StreamingResponse:
    """Server-sent events: the dashboard listens here instead of polling."""
    cursor = last_event_id() if after is None else after

    async def gen():
        nonlocal cursor
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            for e in events_after(cursor):
                cursor = e["id"]
                yield f"id: {e['id']}\ndata: {json.dumps(e)}\n\n"
            await changed(15)
            yield ": keepalive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/api/events/list")
def event_list(after: int = 0, limit: int = 200) -> list[dict]:
    return events_after(after, min(limit, 1000))
