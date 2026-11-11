"""Task events: stored, broadcast to waiters (long-poll/SSE), and handed to plugins."""
import asyncio
import json
import subprocess
import threading
import time
import fnmatch
import os
from pathlib import Path

from store import db

# FLEET_PLUGINS_DIR lets tests (and anyone) point plugins elsewhere; FLEET_PLUGINS=off disables them.
PLUGIN_DIR = Path(os.environ.get("FLEET_PLUGINS_DIR") or Path(__file__).resolve().parent.parent / "plugins")
_changed = asyncio.Event()


def emit(task_id: str | None, type_: str, data: dict | None = None) -> int:
    with db() as c:
        cur = c.execute("INSERT INTO events (ts, task_id, type, data) VALUES (?,?,?,?)",
                        (time.time(), task_id, type_, json.dumps(data or {})))
        event_id = cur.lastrowid
    event = {"id": event_id, "ts": time.time(), "task_id": task_id, "type": type_, "data": data or {}}
    _wake()
    run_plugins(event)
    return event_id


_loop: asyncio.AbstractEventLoop | None = None


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def _wake() -> None:
    # asyncio.Event isn't thread-safe; sync endpoints run in a threadpool, so hop onto the loop.
    if _loop is None:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _loop:
        _swap()
    else:
        _loop.call_soon_threadsafe(_swap)


def _swap() -> None:
    # Wake every waiter, then arm a fresh Event for the next round.
    global _changed
    old, _changed = _changed, asyncio.Event()
    old.set()


def snapshot() -> asyncio.Event:
    """Take this BEFORE reading state; anything emitted afterwards sets it, so no wakeup is lost."""
    return _changed


async def changed(since: asyncio.Event, timeout: float) -> None:
    try:
        await asyncio.wait_for(since.wait(), timeout)
    except asyncio.TimeoutError:
        pass


def events_after(after: int, limit: int = 200) -> list[dict]:
    with db() as c:
        rows = c.execute("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
    return [dict(r) | {"data": json.loads(r["data"])} for r in rows]


def last_event_id() -> int:
    with db() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]


# ---------- plugins ----------
# A plugin is master/plugins/<name>.json: {"events": ["task.blocked", ...], "command": ["sh", "x.sh"]}.
# The command runs from the plugins directory with the event JSON on stdin. Local files only.

def load_plugins() -> list[dict]:
    plugins = []
    for manifest in sorted(PLUGIN_DIR.glob("*.json")):
        try:
            spec = json.loads(manifest.read_text())
            if spec.get("enabled", True) and spec.get("command"):
                plugins.append({"name": manifest.stem, "events": set(spec.get("events") or []),
                                "command": [str(x) for x in spec["command"]]})
        except (OSError, ValueError) as e:
            print(f"plugin {manifest.name} ignored: {e}")
    return plugins


PLUGINS = load_plugins()


_recent: dict[tuple, float] = {}
DEDUPE_SECONDS = 60


def _task(task_id: str | None) -> dict:
    if not task_id:
        return {}
    try:
        with db() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            in_pipeline = c.execute(
                "SELECT 1 FROM pipelines WHERE build_task=? OR reviews LIKE ?", (task_id, f'%"{task_id}"%')).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    task = dict(row)
    task["in_pipeline"] = bool(in_pipeline)
    try:
        task["denials"] = json.loads(task.get("denials") or "[]")
    except ValueError:
        task["denials"] = []
    return task


def _agent(name: str | None) -> str:
    return {"claude": "Claude", "codex": "Codex", "ollama": "A local model"}.get(name or "", name or "An agent")


def describe(event: dict) -> dict | None:
    """What a person should read for an event: {title, subtitle, message}, or None for nothing worth showing."""
    kind, data = event.get("type", ""), event.get("data") or {}
    if kind == "notify.test":
        return {"title": "AI Fleet", "subtitle": "Test notification", "message": "Notifications from your fleet work."}
    if kind.startswith("task."):
        task = _task(event.get("task_id"))
        name = (data.get("title") or task.get("title") or "A task")[:80]
        agent = _agent(data.get("agent") or task.get("agent"))
        if kind == "task.awaiting_approval":
            mode = data.get("mode") or task.get("mode") or "a higher"
            return {"title": "Approval needed", "subtitle": name,
                    "message": f"{agent} wants to run in {mode} mode. Open AI Fleet to approve or reject."}
        if kind == "task.blocked":
            tools = sorted({d.get("tool_name") or d.get("tool") for d in task.get("denials", [])} - {None, ""})
            return {"title": "Task blocked", "subtitle": name,
                    "message": f"{agent} was denied {', '.join(tools) or 'a tool'} and stopped. Approve to let it continue."}
        if kind == "task.auto_approve_exhausted":
            return {"title": "Task needs you", "subtitle": name,
                    "message": f"{agent} kept hitting denied tools after {data.get('rounds', 3)} automatic approvals."}
        if task.get("in_pipeline"):
            return None  # the pipeline reports its own outcome once
        if kind == "task.review":
            return {"title": "Ready for review", "subtitle": name, "message": f"{agent} finished and its checks passed."}
        if kind in ("task.failed", "task.interrupted"):
            what = "stopped unexpectedly" if kind == "task.interrupted" else "failed"
            detail = (task.get("error") or "").strip()
            return {"title": "Task " + what, "subtitle": name, "message": (detail[:140] or f"{agent}'s run {what}.")}
        return None
    if kind.startswith("pipeline."):
        name = (data.get("title") or "A pipeline")[:80]
        state = kind.split(".", 1)[1]
        if state == "ready":
            return {"title": "Ready to merge", "subtitle": name, "message": "Built, tested and approved by its reviewers."}
        if state == "escalated":
            return {"title": "Pipeline needs you", "subtitle": name,
                    "message": (data.get("note") or "Its reviewers couldn't settle it.")[:160]}
        if state == "failed":
            return {"title": "Pipeline failed", "subtitle": name, "message": (data.get("note") or "The build failed.")[:160]}
        return None
    if kind == "model.state" and data.get("state") in ("blocked", "failed"):
        model, node = data.get("model") or "A model", data.get("node") or "a node"
        return {"title": f"Model install {data['state']}", "subtitle": f"{model} on {node}",
                "message": (data.get("reason") or "See Models in AI Fleet.")[:160]}
    if kind == "node.offline":
        return {"title": "Node offline", "subtitle": data.get("node") or data.get("name") or "A node",
                "message": "It stopped answering. Tasks won't be sent to it until it's back."}
    return None


def run_plugins(event: dict) -> None:
    if os.environ.get("FLEET_PLUGINS", "").lower() == "off":
        return
    wanted = [p for p in PLUGINS if _plugin_wants(p, event)]
    if not wanted:
        return
    note = describe(event)
    if not note:
        return
    key = (event.get("type"), event.get("task_id"), note["subtitle"])
    now = time.time()
    if now - _recent.get(key, 0) < DEDUPE_SECONDS:
        return
    _recent[key] = now
    if len(_recent) > 1000:
        for k in [k for k, t in _recent.items() if now - t > DEDUPE_SECONDS]:
            _recent.pop(k, None)
    event = dict(event, notification=note)
    for p in wanted:
        threading.Thread(target=_run_plugin, args=(p, event), daemon=True).start()


def _plugin_wants(plugin: dict, event: dict) -> bool:
    """Database rules override a manifest's legacy event list for that plugin."""
    try:
        with db() as c:
            rules = c.execute("SELECT event_glob,enabled FROM notify_rules WHERE plugin=?", (plugin["name"],)).fetchall()
    except Exception:  # startup migrations may not have run yet
        rules = []
    if not rules:
        return event["type"] in plugin["events"] or "*" in plugin["events"]
    for row in rules:
        if not row["enabled"]:
            continue
        pattern = row["event_glob"]
        if pattern in ("model.state", "model.state:blocked|failed"):
            if event["type"] == "model.state" and event.get("data", {}).get("state") in ("blocked", "failed"):
                return True
        elif fnmatch.fnmatchcase(event["type"], pattern):
            return True
    return False


def send_test(plugin_name: str, rule_id: str) -> bool:
    plugin = next((item for item in PLUGINS if item["name"] == plugin_name), None)
    if not plugin:
        return False
    event = {"id": 0, "ts": time.time(), "task_id": None, "type": "notify.test",
             "data": {"rule_id": rule_id}}
    event["notification"] = describe(event)
    threading.Thread(target=_run_plugin, args=(plugin, event), daemon=True).start()
    return True


def _run_plugin(plugin: dict, event: dict) -> None:
    try:
        subprocess.run(plugin["command"], input=json.dumps(event), text=True, cwd=PLUGIN_DIR,
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"plugin {plugin['name']} failed: {e}")
